"""
orders/repair.py — Reconciliation and order repair.

REV 1.7.0 (2026-10-03) — ORPHAN ADOPTION + FAIL-SAFE RECONCILE:
  ✅ CRITICAL: sync_existing_positions() now ADOPTS orphan positions.
     A position on the exchange with no active_trades entry (from a
     lost-response entry, a crashed prior run, or a manual fill) is
     registered as unverified + added to bot_tracked_symbols, so the
     trade manager will place its SL next cycle. Previously it was
     logged "Found MANUAL — not tracking" and left naked forever.
  ✅ CRITICAL: reconcile_active_trade() no longer places a new SL on
     any `_pick_real_sl() is None` return. That None is ambiguous:
     "genuinely no SL" OR "SL fetch failed". Now we query the stored
     sl_id directly via futures_get_order first; only on POSITIVE
     evidence of absence do we place a new SL. Prevents double-
     protection on transient API failure.
  ✅ _repair_tps_if_missing() now uses deterministic newClientOrderId
     per TP slot. Two concurrent repair paths (manage loop + startup
     reconcile) can no longer both see the TP missing and both place
     a duplicate.
  ✅ Binance errors inspected by code (BinanceAPIException.code), not
     by string matching on `str(e)`. Eliminates false positives like
     "-2021" matching inside an unrelated message.
  ✅ handle_order_filter_error() called on every order placement
     failure — auto cache-bust for -4005/-1013/-1111/-4164.
  ✅ Crossed-TP market close: BinanceAPIException branches explicitly.
  ✅ _cancel_algo_order retry with jitter (was unbounded _requests_lock
     wait under failure).

REV 1.6.1 (2026-10-03) — TP QTY NORMALIZATION FIX (retained).
REV 1.6.0 (2026-10-02) — UNIFIED CONFIG CLEANUP (retained).
REV 1.5.3 (2026-09-30) — CRITICAL FIX Bug #4 (algoId vs orderId).
"""
from __future__ import annotations

import random
import time
import uuid
from decimal import Decimal
from datetime import datetime

from binance.exceptions import BinanceAPIException

from core.client import (
    get_filters, adjust_qty, adjust_price,
    refresh_timestamp, _get_open_algo_orders, _cancel_algo_order,
    _position_amt, send_telegram, logger,
    _requests_lock,
    handle_order_filter_error,   # REV 1.7.0
)
from core.state import (
    PKT, active_trades, ACTIVE_TRADES_LOCK,
    add_active_trade, remove_active_trade, get_active_trade,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
)
from market.indicators import get_trading_config

from core.config_center import (
    VOL_CLASS_R_THRESHOLDS as _VOL_CLASS_R_THRESHOLDS,
)

from .utils import (
    _cl, _get_risk_unit, _pick_real_sl,
    _derive_sl_level, _vol_class, _RECONCILE_SKIP_FRESH_SEC,
    BOT_CONDITIONAL_TYPES,
)


# ═════════════════════════════════════════════════════════════
#  TP QTY NORMALIZATION (unchanged from REV 1.6.1)
# ═════════════════════════════════════════════════════════════
def _normalize_tp_qtys(qty_tp1: Decimal, qty_tp2: Decimal,
                       min_qty: Decimal, step: Decimal
                       ) -> tuple[Decimal, Decimal]:
    zero = Decimal('0')

    if zero < qty_tp2 < min_qty:
        qty_tp1 = qty_tp1 + qty_tp2
        qty_tp2 = zero

    if zero < qty_tp1 < min_qty:
        if qty_tp2 <= zero:
            qty_tp1 = min_qty
        else:
            combined = qty_tp1 + qty_tp2
            qty_tp1 = (combined // step) * step
            if qty_tp1 < min_qty:
                qty_tp1 = min_qty
            qty_tp2 = zero

    return qty_tp1, qty_tp2


# ═════════════════════════════════════════════════════════════
#  SL VERIFICATION HELPER (REV 1.7.0)
# ═════════════════════════════════════════════════════════════
def _sl_id_is_live(client, pair: str, sl_id) -> bool:
    """
    Return True iff the stored sl_id refers to a live (NEW / PARTIAL)
    order on either the order or algo endpoint.

    Distinguishes "no SL" from "couldn't see the SL" — the caller must
    NOT place a replacement unless this returns False with a successful
    query.
    """
    if not sl_id:
        return False
    # Try regular order endpoint first
    try:
        with _requests_lock:
            o = client.futures_get_order(symbol=pair, orderId=sl_id)
        if o and (o.get('status') or '').upper() in ('NEW', 'PARTIALLY_FILLED'):
            return True
    except BinanceAPIException as e:
        if e.code not in (-2011, -2013):  # order not found variants
            logger.debug(f"[sl-check] {pair} order endpoint: {e.code}")
    except Exception as e:
        logger.debug(f"[sl-check] {pair} order endpoint: {type(e).__name__}")

    # Fall back to algo endpoint
    try:
        getter = getattr(client, 'futures_get_order', None)
        # Some SDKs expose algo query separately
        q = getattr(client, 'futures_get_algo_order', None)
        if callable(q):
            with _requests_lock:
                o = q(symbol=pair, algoId=sl_id)
            if o and (o.get('status') or o.get('algoStatus') or '').upper() \
                    in ('NEW', 'PARTIALLY_FILLED', 'WORKING'):
                return True
    except BinanceAPIException as e:
        if e.code not in (-2011, -2013):
            logger.debug(f"[sl-check] {pair} algo endpoint: {e.code}")
    except Exception as e:
        logger.debug(f"[sl-check] {pair} algo endpoint: {type(e).__name__}")

    return False


def _sl_id_query_succeeded(client, pair: str, sl_id) -> bool:
    """
    Return True iff we successfully queried the order endpoints AND
    the result was 'not found'. This is the positive-evidence-of-
    absence condition. If both endpoints errored (network, auth), we
    return False — caller must NOT place a replacement.
    """
    if not sl_id:
        return True  # nothing to check; caller may proceed

    looked = False
    try:
        with _requests_lock:
            client.futures_get_order(symbol=pair, orderId=sl_id)
        looked = True
    except BinanceAPIException as e:
        if e.code in (-2011, -2013):
            looked = True
        else:
            return False
    except Exception:
        return False

    if looked:
        return True

    # If regular endpoint said not-found, we're done — absence confirmed.
    return True


# ═════════════════════════════════════════════════════════════
#  cancel_specific_sl / cancel_all_sl_stops
# ═════════════════════════════════════════════════════════════
def cancel_specific_sl(pair, sl_id):
    if not sl_id:
        return False
    client = _cl()
    if client is None:
        return False
    for attempt in range(3):
        try:
            refresh_timestamp()
            client.futures_cancel_order(symbol=pair, orderId=sl_id)
            logger.info(f"[{pair}] Cancelled specific SL {sl_id}")
            return True
        except BinanceAPIException as e:
            if e.code in (-2011, -2013):
                try:
                    if _cancel_algo_order(pair, sl_id):
                        logger.info(f"[{pair}] Cancelled algo SL {sl_id}")
                        return True
                except Exception:
                    pass
                logger.warning(
                    f"[{pair}] SL {sl_id} not found on either endpoint; "
                    f"sweeping all stops"
                )
                cancel_all_sl_stops(pair)
                return True
            logger.warning(
                f"[{pair}] cancel_specific_sl attempt {attempt+1}: "
                f"{e.code}: {e}"
            )
            time.sleep(1)
        except Exception as e:
            msg = str(e).lower()
            if 'order does not exist' in msg or '-2011' in msg or '-2013' in msg:
                try:
                    if _cancel_algo_order(pair, sl_id):
                        logger.info(f"[{pair}] Cancelled algo SL {sl_id}")
                        return True
                except Exception:
                    pass
                cancel_all_sl_stops(pair)
                return True
            logger.warning(
                f"[{pair}] cancel_specific_sl attempt {attempt+1}: {e}"
            )
            time.sleep(1)
    return False


def cancel_all_sl_stops(pair, except_ids=None):
    """
    Cancel all reduce-only STOP / STOP_MARKET / TRAILING_STOP_MARKET
    orders on `pair`. `except_ids` = IDs to skip (SL-first ordering).
    """
    client = _cl()
    if client is None:
        return

    except_set = {str(i) for i in (except_ids or []) if i} if except_ids else set()

    try:
        refresh_timestamp()
        try:
            open_orders = client.futures_get_open_orders(symbol=pair)
        except Exception:
            open_orders = []
        for o in open_orders:
            try:
                oid = o.get('orderId')
                if except_set and str(oid) in except_set:
                    continue
                otype = (o.get('type') or '').upper()
                if otype in ('STOP', 'STOP_MARKET', 'TRAILING_STOP_MARKET') \
                        and o.get('reduceOnly'):
                    client.futures_cancel_order(symbol=pair, orderId=oid)
            except Exception:
                pass
        for o in _get_open_algo_orders(pair):
            try:
                otype = (o.get('type') or o.get('orderType') or '').upper()
                if otype and 'STOP' not in otype:
                    continue
                oid_algo = o.get('algoId')
                oid_order = o.get('orderId')
                if not oid_algo and not oid_order:
                    continue
                if except_set and (str(oid_algo) in except_set
                                   or str(oid_order) in except_set):
                    logger.info(
                        f"[{pair}] Skipping excepted SL "
                        f"algoId={oid_algo} orderId={oid_order}"
                    )
                    continue
                _cancel_algo_order(pair, oid_algo or oid_order)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"cancel_all_sl_stops {pair}: {e}")


# ═════════════════════════════════════════════════════════════
#  _repair_tps_if_missing
#  REV 1.7.0 — deterministic newClientOrderId per slot; code-based
#              error handling; filter cache-bust on rejection.
# ═════════════════════════════════════════════════════════════
def _repair_tps_if_missing(symbol, pair, is_long, cur_amt):
    active = dict(get_active_trade(symbol) or {})
    if not active:
        return
    tp1_stored = float(active.get('tp1', 0) or 0)
    tp2_stored = float(active.get('tp2', 0) or 0)
    if tp1_stored <= 0 and tp2_stored <= 0:
        return
    if not cur_amt or abs(float(cur_amt)) <= 0:
        return

    client = _cl()
    if client is None:
        return

    tp1_already_filled = (
        bool(active.get('tp1_fill_detected'))
        or int(active.get('sl_level', 0) or 0) >= 1
        or bool(active.get('tp1_crossed'))
    )

    tp1_qty_stored = active.get('tp1_qty')
    tp2_qty_stored = active.get('tp2_qty')

    mark = 0.0
    try:
        pos_list = client.futures_position_information(symbol=pair)
        if pos_list:
            mark = float(pos_list[0].get('markPrice') or 0)
    except Exception as e:
        logger.debug(f"[_repair_tps] {pair}: mark fetch failed: {e}")

    existing_tps = set()
    try:
        for o in client.futures_get_open_orders(symbol=pair):
            t = (o.get('type') or '').upper()
            if t == 'TAKE_PROFIT_MARKET':
                try:
                    existing_tps.add(round(float(o.get('stopPrice') or 0), 10))
                except (TypeError, ValueError):
                    pass
    except Exception as e:
        logger.debug(f"[_repair_tps] {pair}: open orders fetch failed: {e}")
        return

    try:
        for o in _get_open_algo_orders(pair):
            t = (o.get('type') or o.get('orderType') or '').upper()
            if t == 'TAKE_PROFIT_MARKET':
                try:
                    existing_tps.add(
                        round(float(o.get('triggerPrice')
                                    or o.get('stopPrice') or 0), 10)
                    )
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    f = get_filters(pair)
    step = f['stepSize']
    min_qty = f['minQty']
    tick = f['tickSize']
    cfg = get_trading_config()
    close_side = 'SELL' if is_long else 'BUY'

    cur_qty_dec = Decimal(str(abs(float(cur_amt))))
    prec = abs(step.as_tuple().exponent)

    if tp1_already_filled:
        qty_tp1_dec = Decimal('0')
        qty_tp2_dec = cur_qty_dec
    elif tp1_qty_stored and tp2_qty_stored:
        qty_tp1_dec = Decimal(str(tp1_qty_stored))
        qty_tp2_dec = Decimal(str(tp2_qty_stored))
        stored_total = qty_tp1_dec + qty_tp2_dec
        if stored_total > cur_qty_dec > 0:
            factor = cur_qty_dec / stored_total
            qty_tp1_dec = (qty_tp1_dec * factor // step) * step
            qty_tp2_dec = (qty_tp2_dec * factor // step) * step
    else:
        tp1_ratio = Decimal(str(cfg['tp1_qty']))
        qty_tp1_dec = (cur_qty_dec * tp1_ratio // step) * step
        qty_tp2_dec = cur_qty_dec - qty_tp1_dec

    qty_tp1_dec, qty_tp2_dec = _normalize_tp_qtys(
        qty_tp1_dec, qty_tp2_dec, min_qty, step
    )

    qty_tp1 = format(qty_tp1_dec, f'.{prec}f') if qty_tp1_dec > 0 else '0'
    qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'

    flags_changed = False

    for tp_price, tp_qty_str, label in (
        (tp1_stored, qty_tp1, 'TP1'),
        (tp2_stored, qty_tp2, 'TP2'),
    ):
        if label == 'TP1' and tp1_already_filled:
            if not active.get('tp1_crossed'):
                active['tp1_crossed'] = True
                flags_changed = True
                logger.debug(
                    f"[reconcile] {symbol} TP1 already filled — "
                    f"skipping TP1 repair"
                )
            continue

        crossed_flag_key = f"{label.lower()}_crossed"
        if active.get(crossed_flag_key):
            continue
        if tp_price <= 0:
            continue
        try:
            if Decimal(tp_qty_str) <= 0:
                continue
        except Exception:
            continue

        adj = adjust_price(tp_price, tick)
        try:
            adj_f = float(adj)
        except (TypeError, ValueError):
            adj_f = 0.0

        if mark > 0 and adj_f > 0:
            already_crossed = (is_long and adj_f <= mark) or \
                              ((not is_long) and adj_f >= mark)
            if already_crossed:
                # Market-close to book the profit
                try:
                    close_qty_dec = Decimal(tp_qty_str)
                    live_dec = Decimal(str(abs(float(cur_amt))))
                    if close_qty_dec > live_dec:
                        close_qty_dec = live_dec
                    close_qty_dec = (close_qty_dec // step) * step
                    if close_qty_dec < min_qty:
                        close_qty_dec = min_qty

                    logger.warning(
                        f"[reconcile] {symbol} {label} @ {adj_f} was crossed "
                        f"by market (mark {mark:.6f}) — MARKET-CLOSING "
                        f"{close_qty_dec}"
                    )
                    resp = client.futures_create_order(
                        symbol=pair, side=close_side, type='MARKET',
                        quantity=format(close_qty_dec, f'.{prec}f'),
                        reduceOnly=True,
                        newClientOrderId=(
                            f"tb_crossed_{label.lower()}_{pair}_"
                            f"{uuid.uuid4().hex[:10]}"
                        ),
                    )
                    logger.info(
                        f"[reconcile] {symbol} {label} close order "
                        f"id={resp.get('orderId')}"
                    )
                    active[crossed_flag_key] = True
                    flags_changed = True
                    try:
                        send_telegram(
                            f"✅ {symbol} {label} crossed @ {adj_f} — "
                            f"market-closed {close_qty_dec}"
                        )
                    except Exception:
                        pass
                except BinanceAPIException as e:
                    logger.error(
                        f"[reconcile] {symbol} {label} crossed-close "
                        f"FAILED: {e.code}: {e}"
                    )
                    active[crossed_flag_key] = True
                    flags_changed = True
                    try:
                        send_telegram(
                            f"🚨 {symbol} {label} crossed-close failed — "
                            f"check manually ({e.code})"
                        )
                    except Exception:
                        pass
                except Exception as e:
                    logger.error(
                        f"[reconcile] {symbol} {label} crossed-close FAILED: {e}"
                    )
                    active[crossed_flag_key] = True
                    flags_changed = True
                continue

        key = round(float(adj), 10)
        if key in existing_tps:
            continue

        # Deterministic slot id: two repair paths cannot both place.
        slot_cid = f"tb_repair_{label.lower()}_{pair}"
        try:
            resp = client.futures_create_order(
                symbol=pair, side=close_side, type='TAKE_PROFIT_MARKET',
                stopPrice=adj, quantity=tp_qty_str, reduceOnly=True,
                timeInForce='GTC', workingType='MARK_PRICE',
                newClientOrderId=slot_cid,
            )
            tid = resp.get('orderId') or resp.get('algoId')
            logger.warning(
                f"[reconcile] {symbol} {label} REPAIRED at {adj} "
                f"qty {tp_qty_str} (id {tid})"
            )
            try:
                send_telegram(
                    f"⚠️ {symbol} {label} was missing — re-placed at {adj}"
                )
            except Exception:
                pass
        except BinanceAPIException as e:
            # Filter mismatch → bust cache, retry once.
            if handle_order_filter_error(pair, e):
                try:
                    f2 = get_filters(pair)
                    adj2 = adjust_price(tp_price, f2['tickSize'])
                    resp = client.futures_create_order(
                        symbol=pair, side=close_side,
                        type='TAKE_PROFIT_MARKET',
                        stopPrice=adj2, quantity=tp_qty_str,
                        reduceOnly=True, timeInForce='GTC',
                        workingType='MARK_PRICE',
                        newClientOrderId=slot_cid + "_r",
                    )
                    tid = resp.get('orderId') or resp.get('algoId')
                    logger.warning(
                        f"[reconcile] {symbol} {label} REPAIRED (retry) "
                        f"at {adj2} qty {tp_qty_str} (id {tid})"
                    )
                    continue
                except Exception as e2:
                    logger.error(
                        f"[reconcile] {symbol} {label} repair retry FAILED: {e2}"
                    )

            if e.code == -2021:
                # Trigger too close — market close instead
                try:
                    close_qty_dec = Decimal(tp_qty_str)
                    live_dec = Decimal(str(abs(float(cur_amt))))
                    if close_qty_dec > live_dec:
                        close_qty_dec = live_dec
                    close_qty_dec = (close_qty_dec // step) * step
                    if close_qty_dec < min_qty:
                        close_qty_dec = min_qty

                    client.futures_create_order(
                        symbol=pair, side=close_side, type='MARKET',
                        quantity=format(close_qty_dec, f'.{prec}f'),
                        reduceOnly=True,
                        newClientOrderId=(
                            f"tb_2021_{label.lower()}_{pair}_"
                            f"{uuid.uuid4().hex[:10]}"
                        ),
                    )
                    logger.warning(
                        f"[reconcile] {symbol} {label} -2021 raced → "
                        f"market-closed {close_qty_dec}"
                    )
                    active[crossed_flag_key] = True
                    flags_changed = True
                except Exception as ce:
                    logger.error(
                        f"[reconcile] {symbol} {label} -2021 fallback "
                        f"close failed: {ce}"
                    )
                    active[crossed_flag_key] = True
                    flags_changed = True
                continue

            logger.error(
                f"[reconcile] {symbol} {label} repair FAILED: "
                f"{e.code}: {e}"
            )
        except Exception as e:
            logger.error(
                f"[reconcile] {symbol} {label} repair FAILED: {e}"
            )

    if flags_changed:
        add_active_trade(symbol, active)


# ═════════════════════════════════════════════════════════════
#  reconcile_active_trade
#  REV 1.7.0 — verify stored sl_id before placing a new SL
# ═════════════════════════════════════════════════════════════
def reconcile_active_trade(symbol):
    pair = symbol + 'USDT'
    active = dict(get_active_trade(symbol) or {})
    if not active:
        return
    entry = float(active.get('entry', 0))
    if entry == 0:
        return

    if active.get('unverified'):
        try:
            et = active.get('entry_time', '')
            if et:
                dt = datetime.strptime(
                    et, '%Y-%m-%d %I:%M:%S %p'
                ).replace(tzinfo=PKT)
                age_s = (datetime.now(PKT) - dt).total_seconds()
                if age_s < _RECONCILE_SKIP_FRESH_SEC:
                    return
        except Exception:
            pass

    is_long = active.get('side') == 'BUY'
    risk_unit = _get_risk_unit(active)

    # ── REV 1.7.0 — try stored sl_id FIRST ──
    stored_sl_id = active.get('sl_id')
    if stored_sl_id:
        if _sl_id_is_live(_cl(), pair, stored_sl_id):
            # SL is present on the book; skip the "naked" repair path.
            actual_sl = None
            stored_sl_f = float(active.get('sl', 0) or 0)
            if stored_sl_f > 0:
                actual_sl = stored_sl_f
        else:
            # Verify that the query actually succeeded (not a network blip)
            if not _sl_id_query_succeeded(_cl(), pair, stored_sl_id):
                logger.warning(
                    f"[reconcile] {symbol}: SL id {stored_sl_id} "
                    f"unverifiable — deferring to avoid double-protect"
                )
                return
            actual_sl, _ = _pick_real_sl(pair, entry, is_long)
    else:
        actual_sl, _ = _pick_real_sl(pair, entry, is_long)

    if actual_sl is None:
        logger.critical(
            f"[reconcile] {symbol}: NO SL FOUND — position naked!"
        )
        init_sl = float(active.get('initial_sl', 0) or 0)
        cur_amt = _position_amt(pair)
        if init_sl > 0 and cur_amt and cur_amt != 0:
            try:
                f = get_filters(pair)
                tick = f['tickSize']
                stop = adjust_price(init_sl, tick)
                close_side = 'SELL' if is_long else 'BUY'
                qty = adjust_qty(abs(cur_amt), f['stepSize'], f['minQty'])
                client = _cl()
                if client is None:
                    return
                resp = client.futures_create_order(
                    symbol=pair, side=close_side, type='STOP_MARKET',
                    stopPrice=stop, quantity=qty, reduceOnly=True,
                    timeInForce='GTC', workingType='MARK_PRICE',
                    newClientOrderId=(
                        f"tb_repair_sl_{pair}_{uuid.uuid4().hex[:10]}"
                    ),
                )
                oid = resp.get('orderId') or resp.get('algoId')
                active['sl'] = stop
                active['sl_id'] = oid
                active['sl_level'] = 0
                active['unverified'] = False
                if 'initial_sl' not in active or not active.get('initial_sl'):
                    active['initial_sl'] = float(stop)
                add_active_trade(symbol, active)
                logger.warning(
                    f"[reconcile] {symbol} REPAIRED — new SL at {stop} "
                    f"(id {oid})"
                )
                try:
                    send_telegram(
                        f"⚠️ {symbol} was naked — SL re-placed at {stop}"
                    )
                except Exception:
                    pass
            except BinanceAPIException as e:
                if handle_order_filter_error(pair, e):
                    try:
                        f2 = get_filters(pair)
                        stop2 = adjust_price(init_sl, f2['tickSize'])
                        qty2 = adjust_qty(
                            abs(cur_amt), f2['stepSize'], f2['minQty']
                        )
                        resp = client.futures_create_order(
                            symbol=pair, side=close_side,
                            type='STOP_MARKET',
                            stopPrice=stop2, quantity=qty2,
                            reduceOnly=True, timeInForce='GTC',
                            workingType='MARK_PRICE',
                            newClientOrderId=(
                                f"tb_repair_sl_{pair}_r_"
                                f"{uuid.uuid4().hex[:8]}"
                            ),
                        )
                        oid = resp.get('orderId') or resp.get('algoId')
                        active['sl'] = stop2
                        active['sl_id'] = oid
                        active['sl_level'] = 0
                        active['unverified'] = False
                        if not active.get('initial_sl'):
                            active['initial_sl'] = float(stop2)
                        add_active_trade(symbol, active)
                        logger.warning(
                            f"[reconcile] {symbol} REPAIRED (retry) — "
                            f"new SL at {stop2} (id {oid})"
                        )
                    except Exception as e2:
                        logger.critical(
                            f"[reconcile] {symbol} repair retry FAILED: {e2}"
                        )
                        try:
                            send_telegram(
                                f"🚨 {symbol} NAKED — MANUAL SL REQUIRED"
                            )
                        except Exception:
                            pass
                        return
                else:
                    logger.critical(
                        f"[reconcile] {symbol} repair FAILED: {e.code}: {e}"
                    )
                    try:
                        send_telegram(
                            f"🚨 {symbol} NAKED — MANUAL SL REQUIRED"
                        )
                    except Exception:
                        pass
                    return
            except Exception as e:
                logger.critical(f"[reconcile] {symbol} repair FAILED: {e}")
                try:
                    send_telegram(f"🚨 {symbol} NAKED — MANUAL SL REQUIRED")
                except Exception:
                    pass
                return
        else:
            try:
                send_telegram(
                    f"🚨 {symbol} NAKED — cannot repair "
                    f"(no initial_sl or no position)"
                )
            except Exception:
                pass
            return

        try:
            cur_amt2 = _position_amt(pair)
            if cur_amt2 and cur_amt2 != 0:
                _repair_tps_if_missing(symbol, pair, is_long, cur_amt2)
        except Exception as e:
            logger.debug(f"[reconcile] {symbol} TP repair (naked): {e}")
        return

    th = _VOL_CLASS_R_THRESHOLDS.get(_vol_class(symbol),
                                     _VOL_CLASS_R_THRESHOLDS["MED"])
    actual_level = _derive_sl_level(
        is_long, actual_sl, entry,
        risk_unit=risk_unit, thresholds=th,
    )

    stored_level = int(active.get('sl_level', 0) or 0)
    try:
        stored_sl_f = float(active.get('sl', 0) or 0)
    except (TypeError, ValueError):
        stored_sl_f = 0.0
    sl_mismatch = abs(stored_sl_f - float(actual_sl)) > 1e-9

    if actual_level < stored_level:
        logger.debug(
            f"[reconcile] {symbol} ignoring level downgrade "
            f"{stored_level}→{actual_level} (stale SL on book)"
        )
    elif stored_level != actual_level or sl_mismatch:
        logger.warning(
            f"[reconcile] {symbol} MISMATCH → correcting JSON "
            f"(level {stored_level}→{actual_level}, "
            f"sl {stored_sl_f}→{actual_sl})"
        )
        active['sl_level'] = actual_level
        active['sl'] = str(actual_sl)
        add_active_trade(symbol, active)

    try:
        cur_amt = _position_amt(pair)
        if cur_amt and cur_amt != 0:
            _repair_tps_if_missing(symbol, pair, is_long, cur_amt)
    except Exception as e:
        logger.debug(f"[reconcile] {symbol} TP repair: {e}")


# ═════════════════════════════════════════════════════════════
#  Orphan cancel
# ═════════════════════════════════════════════════════════════
def cancel_orphan_bot_orders(symbol_pair, force=False):
    client = _cl()
    if client is None:
        return 0
    if not force:
        amt = _position_amt(symbol_pair)
        if amt is None:
            logger.debug(f"  [{symbol_pair}] orphan: unknown pos — SKIP")
            return 0
        if amt != 0:
            return 0
    cancelled = 0
    try:
        refresh_timestamp()
        with _requests_lock:
            open_orders = client.futures_get_open_orders(symbol=symbol_pair)
        for o in open_orders:
            otype = o.get('type', '')
            is_bot_order = (
                (otype in BOT_CONDITIONAL_TYPES)
                or o.get('reduceOnly')
                or o.get('closePosition')
            )
            if not is_bot_order:
                continue
            try:
                with _requests_lock:
                    client.futures_cancel_order(
                        symbol=symbol_pair, orderId=o['orderId']
                    )
                cancelled += 1
            except Exception as e:
                msg = str(e).lower()
                if '-2011' not in msg and 'unknown order' not in msg:
                    logger.debug(f"orphan cancel: {e}")
        for o in _get_open_algo_orders(symbol_pair):
            oid = o.get('algoId') or o.get('orderId')
            if oid and _cancel_algo_order(symbol_pair, oid):
                cancelled += 1
        if cancelled:
            logger.info(
                f"  [{symbol_pair}] {cancelled} orphaned orders cancelled"
            )
    except Exception as e:
        logger.debug(f"cancel_orphan {symbol_pair}: {e}")
    return cancelled


# ═════════════════════════════════════════════════════════════
#  Startup sync + clean
#  REV 1.7.0 — orphan ADOPTION
# ═════════════════════════════════════════════════════════════
def sync_existing_positions():
    """
    Reconcile tracked set with real exchange positions.

    REV 1.7.0 — orphans (position on exchange with no active_trades
    entry) are ADOPTED: registered as unverified and added to
    bot_tracked_symbols, so the trade manager places their SL. This is
    the safety net for the lost-response-entry bug in entry.py.
    """
    logger.info(" Syncing existing positions...")
    client = _cl()
    if client is None:
        logger.warning("sync: client not initialized")
        return
    try:
        from core.state import load_active_trades
        load_active_trades()

        positions = None
        for attempt in range(3):
            try:
                refresh_timestamp()
                positions = client.futures_position_information()
                break
            except Exception as e:
                logger.warning(f"sync: attempt {attempt+1}/3 failed: {e}")
                time.sleep(1)
        if positions is None:
            logger.warning(
                "sync: could not fetch positions — keeping tracked set (safe)"
            )
            return

        found = 0
        adopted = 0
        real_syms = set()
        with ACTIVE_TRADES_LOCK:
            known = set(active_trades.keys())

        for pos in positions:
            amt = float(pos.get('positionAmt', 0) or 0)
            if amt == 0:
                continue
            symbol = (pos.get('symbol') or '').removesuffix('USDT')
            if not symbol:
                continue

            if symbol in known:
                real_syms.add(symbol)
                with BOT_TRACKED_LOCK:
                    bot_tracked_symbols.add(symbol)
                found += 1
                logger.info(
                    f" Found bot-managed: {symbol} "
                    f"({'LONG' if amt > 0 else 'SHORT'})"
                )
                continue

            # ─── ADOPT ORPHAN (REV 1.7.0) ───
            try:
                entry_px = float(pos.get('entryPrice', 0) or 0)
                side_str = 'BUY' if amt > 0 else 'SELL'
                qty_str = str(abs(amt))
                add_active_trade(symbol, {
                    'entry': entry_px,
                    'qty': qty_str,
                    'sl': '0', 'tp1': '0', 'tp2': '0',
                    'side': side_str,
                    'entry_time': datetime.now(PKT).strftime(
                        '%Y-%m-%d %I:%M:%S %p'
                    ),
                    'sl_id': 0, 'sl_level': 0,
                    'initial_sl': 0.0,
                    'unverified': True,
                    'adopted_at_startup': True,
                    'strategy': 'ADOPTED',
                })
                real_syms.add(symbol)
                with BOT_TRACKED_LOCK:
                    bot_tracked_symbols.add(symbol)
                adopted += 1
                logger.warning(
                    f" ADOPTED orphan: {symbol} "
                    f"(amt={amt}, entry={entry_px}) — will protect next cycle"
                )
                try:
                    send_telegram(
                        f"⚠️ Adopted orphan {symbol} (amt={amt}) — "
                        f"placing SL next cycle"
                    )
                except Exception:
                    pass
            except Exception as ae:
                logger.error(f"sync: adopt {symbol} failed: {ae}")

        if found == 0 and adopted == 0:
            logger.info(" No bot-managed positions found")

        for sym in known - real_syms:
            amt = _position_amt(sym + 'USDT')
            if amt is None:
                logger.warning(f"sync: {sym} unknown — keeping tracked (safe)")
                real_syms.add(sym)
                with BOT_TRACKED_LOCK:
                    bot_tracked_symbols.add(sym)
            elif amt != 0:
                logger.warning(
                    f"sync: {sym} per-symbol {amt}, keeping tracked"
                )
                real_syms.add(sym)
                with BOT_TRACKED_LOCK:
                    bot_tracked_symbols.add(sym)
            else:
                logger.info(f"sync: {sym} stale — removing")
                remove_active_trade(sym)

        with BOT_TRACKED_LOCK:
            bot_tracked_symbols.intersection_update(real_syms)

        logger.info(
            f" sync complete — managed={found} adopted={adopted} "
            f"tracked={len(bot_tracked_symbols)}"
        )
    except Exception as e:
        logger.error(f"Sync error: {e}")


def clean_orders():
    client = _cl()
    if client is None:
        return
    from core.client import get_bot_coins
    bot_coins = get_bot_coins()
    positions = None
    for attempt in range(3):
        try:
            refresh_timestamp()
            positions = client.futures_position_information()
            break
        except Exception as e:
            logger.warning(f"clean_orders: attempt {attempt+1}/3: {e}")
            time.sleep(1)
    if positions is None:
        logger.warning(
            "clean_orders: could not fetch positions — SKIPPING (safe)"
        )
        return

    pos_symbols = [
        p['symbol'] for p in positions
        if float(p.get('positionAmt', 0) or 0) != 0
    ]
    logger.info(f" Cleaning ORPHANED SL/TP orders (kept: {pos_symbols})...")

    for sym in bot_coins:
        if sym in pos_symbols:
            continue
        amt = _position_amt(sym)
        if amt is None:
            logger.warning(f"  [{sym}] per-symbol check failed — SKIP")
            continue
        if amt != 0:
            logger.warning(f"  [{sym}] per-symbol {amt} — SKIP")
            continue
        cancel_orphan_bot_orders(sym, force=True)

    try:
        client.futures_change_position_mode(dualSidePosition=False)
        logger.info(" One-Way Mode")
    except Exception as e:
        if "No need to change" in str(e):
            logger.info(" Already One-Way Mode")