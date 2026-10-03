"""
orders/manage.py — Trade management loop.

REV 1.10.0 (2026-10-03) — RACE-SAFE SL LIFECYCLE:
  ✅ CRITICAL: Per-symbol manage lock. `manage_single_trade` and
     `reconcile_active_trade` are now serialised per symbol by
     `_get_manage_lock(symbol)`. Previously both could place or
     cancel the same SL concurrently: `update_sl` would place a new
     SL then cancel the old, while `reconcile_active_trade` (from
     repair.py) simultaneously queried and saw an intermediate
     state — the two paths could both "fix" the SL, leaving a
     duplicate stop or an SL pointing at a cancelled order.
  ✅ CRITICAL: stale sl_id_stored bug fixed. After
     `_detect_tp1_fill_and_move_be` replaced the SL, the local
     `sl_id_stored` variable was not refreshed. `update_sl` would
     then cancel an already-dead order (no-op) instead of the
     current one, leaving the freshly-placed BE stop to be swept
     by cancel_all_sl_stops on the next iteration. Now `sl_id_stored`
     is re-read from state after every write.
  ✅ update_sl: BinanceAPIException.code driven, not string matched.
       • -2021 → skip (stop already crossed; will retry next cycle
                 when mark moves)
       • -4005/-1013/-1111/-4164 → filter cache-bust + retry once
       • other codes → log + retry up to 3 attempts
     Also guards against placing a new SL that is already on the
     wrong side of mark (which would immediately -2021).
  ✅ _detect_tp1_fill_and_move_be: -2021 on BE SL placement now
     market-closes the position (the BE premise is invalid — market
     has already crossed back through entry). Previously the code
     just logged and skipped, leaving the position on its original
     stop while the strategy assumed BE.
  ✅ All new orders carry newClientOrderId — idempotency-safe.
  ✅ BinanceAPIException caught before generic Exception at every
     order placement site (code visibility in logs).

REV 1.9.1 (2026-10-03) — DEFENSIVE HARDENING (retained).
REV 1.9.0 (2026-10-02) — RUNTIME TOGGLE AWARENESS (retained).
REV 1.8.0 (2026-10-02) — TIME_EXIT TOGGLE GATE (retained).
REV 1.7.1 (2026-10-02) — UNIFIED CONFIG CLEANUP (retained).
REV 1.7.0 (2026-10-02) — CONFIG CENTER INTEGRATION (retained).
REV 1.6.2 (2026-09-28) — NAKED-SL WINDOW FIX (retained).
REV 1.6.0 (2026-09-28) — CONTINUOUS TRAILING STOP-LOSS (retained).
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime

from binance.exceptions import BinanceAPIException

from core.client import (
    fetch_position_raw, get_filters, adjust_qty, adjust_price,
    refresh_timestamp, _run_with_timeout, _get_open_algo_orders,
    send_telegram, logger,
    _requests_lock,
    handle_order_filter_error,   # REV 1.10.0
)
from core.state import (
    PKT, get_active_trade, add_active_trade,
    ATR_CACHE, ATR_CACHE_LOCK,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    is_stopped,
)
from market.indicators import calculate_pro_indicators, get_trading_config

from core.config_center import (
    get_config as _get_central_config,
    is_time_exit_enabled,
    get as _cc_get,
)

from .utils import (
    _cl, _get_risk_unit, _r_thresholds_per_class,
)
from .repair import (
    cancel_all_sl_stops, cancel_specific_sl, reconcile_active_trade,
)
from .exit import emergency_close_retry, handle_trade_close


# ═════════════════════════════════════════════════════════════
#  REV 1.9.1 — SAFE CC NUMERIC READ
# ═════════════════════════════════════════════════════════════
def _cc_get_num(key: str, default):
    v = _cc_get(key, None)
    return default if v is None else v


# ═════════════════════════════════════════════════════════════
#  REV 1.10.0 — PER-SYMBOL MANAGE LOCK
#  Prevents manage_single_trade and reconcile_active_trade from
#  racing on the same symbol's SL/TP orders.
# ═════════════════════════════════════════════════════════════
_MANAGE_LOCKS: dict[str, threading.RLock] = {}
_MANAGE_LOCKS_GUARD = threading.Lock()


def get_symbol_manage_lock(symbol: str) -> threading.RLock:
    """
    Public accessor. Callers of reconcile_active_trade from outside
    the trade manager loop should hold this lock for the symbol.
    """
    with _MANAGE_LOCKS_GUARD:
        lk = _MANAGE_LOCKS.get(symbol)
        if lk is None:
            lk = threading.RLock()
            _MANAGE_LOCKS[symbol] = lk
        return lk


def _new_cid(tag: str, pair: str) -> str:
    """Deterministic-ish client order id per placement attempt."""
    return f"tb_{tag}_{pair}_{uuid.uuid4().hex[:12]}"


# ═════════════════════════════════════════════════════════════
#  TP1-FILL → IMMEDIATE BE
#  REV 1.6.2 — SL-first ordering.
#  REV 1.10.0 — newClientOrderId; -2021 handled via market close;
#               filter errors handled by code.
# ═════════════════════════════════════════════════════════════
def _detect_tp1_fill_and_move_be(symbol, pair, is_long, cur_amt, active):
    try:
        if active.get('sl_level', 0) >= 1:
            return
        if active.get('tp1_fill_detected'):
            return
    except Exception:
        return

    client = _cl()
    if client is None:
        return

    tp1_stored = float(active.get('tp1', 0) or 0)
    initial_qty = float(active.get('qty', 0) or 0)
    tp1_qty_stored = float(active.get('tp1_qty', 0) or 0)
    if tp1_stored <= 0 or initial_qty <= 0 or tp1_qty_stored <= 0:
        return

    tp1_still_open = False
    try:
        for o in client.futures_get_open_orders(symbol=pair):
            t = (o.get('type') or '').upper()
            if t != 'TAKE_PROFIT_MARKET':
                continue
            try:
                sp = float(o.get('stopPrice') or 0)
                if abs(sp - tp1_stored) < tp1_stored * 1e-4:
                    tp1_still_open = True
                    break
            except (TypeError, ValueError):
                continue
    except Exception as e:
        logger.debug(
            f"[_detect_tp1_fill] {pair}: open orders fetch failed: {e}"
        )
        return

    try:
        for o in _get_open_algo_orders(pair):
            t = (o.get('type') or o.get('orderType') or '').upper()
            if t != 'TAKE_PROFIT_MARKET':
                continue
            try:
                sp = float(o.get('triggerPrice') or o.get('stopPrice') or 0)
                if abs(sp - tp1_stored) < tp1_stored * 1e-4:
                    tp1_still_open = True
                    break
            except (TypeError, ValueError):
                continue
    except Exception:
        pass

    if tp1_still_open:
        return

    qty_reduction = initial_qty - abs(float(cur_amt))
    if qty_reduction < tp1_qty_stored * 0.5:
        return

    risk_unit = _get_risk_unit(active)
    entry = float(active.get('entry', 0) or 0)
    if entry <= 0:
        return

    cfg = get_trading_config()
    r_th = _r_thresholds_per_class(symbol, cfg)

    if risk_unit and risk_unit > 0:
        be_sl = (entry + risk_unit * r_th["be_stop_r"] if is_long
                 else entry - risk_unit * r_th["be_stop_r"])
    else:
        be_sl = entry * 1.0008 if is_long else entry * 0.9992

    f = get_filters(pair)
    tick = f['tickSize']
    close_side = 'SELL' if is_long else 'BUY'
    be_adj = adjust_price(be_sl, tick)

    rem_qty = adjust_qty(abs(float(cur_amt)), f['stepSize'], f['minQty'])

    # ── STEP 1: place NEW BE SL FIRST ──
    new_id = None
    for _attempt in range(2):
        try:
            resp = client.futures_create_order(
                symbol=pair, side=close_side, type='STOP_MARKET',
                stopPrice=be_adj, quantity=rem_qty, reduceOnly=True,
                timeInForce='GTC', workingType='MARK_PRICE',
                newClientOrderId=_new_cid("be_tp1", pair),
            )
            new_id = resp.get('orderId') or resp.get('algoId')
            if not new_id:
                raise ValueError("BE SL placement returned no orderId")
            break
        except BinanceAPIException as e:
            if e.code == -2021:
                # Market has already crossed the BE level. For a LONG,
                # BE sits above entry, so crossing it means we're back
                # below entry — the BE premise is invalid. Lock the
                # current PnL with a market close.
                logger.critical(
                    f"[_detect_tp1_fill] {symbol}: BE SL -2021 — "
                    f"market already crossed BE. Market-closing to lock."
                )
                try:
                    with _requests_lock:
                        client.futures_create_order(
                            symbol=pair, side=close_side, type='MARKET',
                            quantity=rem_qty, reduceOnly=True,
                            newClientOrderId=_new_cid("be_2021", pair),
                        )
                except Exception as ce:
                    logger.error(
                        f"[_detect_tp1_fill] {symbol}: emergency close "
                        f"after BE -2021 failed: {ce}"
                    )
                    try:
                        threading.Thread(
                            target=emergency_close_retry,
                            args=(symbol, pair, close_side), daemon=True,
                        ).start()
                    except Exception:
                        pass
                try:
                    send_telegram(
                        f"🚨 {symbol} TP1 → BE crossed (-2021) — "
                        f"market-closed remainder"
                    )
                except Exception:
                    pass
                return
            if handle_order_filter_error(pair, e):
                # Cache busted; re-round with fresh filters and retry.
                f = get_filters(pair)
                be_adj = adjust_price(be_sl, f['tickSize'])
                rem_qty = adjust_qty(
                    abs(float(cur_amt)), f['stepSize'], f['minQty']
                )
                continue
            logger.error(
                f"[_detect_tp1_fill] {symbol}: BE SL placement FAILED "
                f"({e.code}): {e} (old SL still active)"
            )
            try:
                send_telegram(
                    f"⚠️ {symbol} TP1 filled but BE SL failed — "
                    f"old SL still active, retrying next cycle"
                )
            except Exception:
                pass
            return
        except Exception as e:
            logger.error(
                f"[_detect_tp1_fill] {symbol}: BE SL placement FAILED: {e} "
                f"(old SL still active — no naked window)"
            )
            try:
                send_telegram(
                    f"⚠️ {symbol} TP1 filled but BE SL placement failed — "
                    f"old SL still active, retrying next scan"
                )
            except Exception:
                pass
            return

    if not new_id:
        return

    # ── STEP 2: new SL is live. Cancel OLD stops, EXCLUDE new. ──
    try:
        cancel_all_sl_stops(pair, except_ids=[new_id])
    except Exception as e:
        logger.warning(
            f"[_detect_tp1_fill] {pair}: post-place SL sweep failed: {e} "
            f"(new SL {new_id} unaffected; old SL(s) may linger)"
        )

    # ── STEP 3: persist state ──
    fresh = dict(get_active_trade(symbol) or {})
    if fresh:
        fresh['sl'] = be_adj
        fresh['sl_id'] = new_id
        fresh['sl_level'] = 1
        fresh['tp1_fill_detected'] = True
        add_active_trade(symbol, fresh)

    logger.warning(
        f"[{symbol}] TP1 FILLED → SL moved to BE at {be_adj} "
        f"(id={new_id}, SL-first placement)"
    )
    try:
        send_telegram(f"✅ {symbol} TP1 filled → SL → BE @ {be_adj}")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════
#  manage_single_trade
# ═════════════════════════════════════════════════════════════
def manage_single_trade(symbol):
    """
    REV 1.10.0 — caller (trade_manager_loop) holds the per-symbol
    manage lock. Direct callers should acquire
    get_symbol_manage_lock(symbol) first.
    """
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        return

    active = get_active_trade(symbol)
    if not active:
        return
    current_sl_price = float(active.get('sl', 0))
    sl_id_stored = active.get('sl_id')
    last_sl_level = active.get('sl_level', 0)
    entry = float(active.get('entry', 0))
    if entry == 0:
        return

    risk_unit = _get_risk_unit(active)
    r_mode = risk_unit is not None and risk_unit > 0

    raw_pos = fetch_position_raw(symbol)
    if raw_pos is None:
        return
    amt = float(raw_pos.get('positionAmt', '0'))
    if amt == 0:
        handle_trade_close(symbol, pair, reason="detected amt=0")
        return

    # Defensive markPrice read
    _mark_raw = raw_pos.get('markPrice')
    if _mark_raw is None:
        logger.debug(
            f"[{symbol}] manage: markPrice missing from position payload "
            f"— skipping tick"
        )
        return
    try:
        mark = float(_mark_raw)
    except (TypeError, ValueError):
        logger.debug(
            f"[{symbol}] manage: markPrice={_mark_raw!r} not numeric "
            f"— skipping tick"
        )
        return

    is_long = amt > 0
    pnl_pct = (
        ((mark - entry) / entry * 100) if is_long
        else ((entry - mark) / entry * 100)
    )
    close_side = 'SELL' if is_long else 'BUY'
    f = get_filters(pair)
    tick = f['tickSize']

    # TP1-fill detection
    try:
        _detect_tp1_fill_and_move_be(symbol, pair, is_long, amt, active)
        # ── REV 1.10.0 — refresh EVERYTHING the child may have changed ──
        active = get_active_trade(symbol) or active
        current_sl_price = float(active.get('sl', current_sl_price))
        last_sl_level = active.get('sl_level', last_sl_level)
        sl_id_stored = active.get('sl_id', sl_id_stored)   # ← stale bug
    except Exception as e:
        logger.debug(f"[{symbol}] TP1-fill detect failed: {e}")

    # ═══════════════════════════════════════════════════════════
    #  TIME EXIT
    # ═══════════════════════════════════════════════════════════
    try:
        entry_time_str = active.get('entry_time', '')
        if entry_time_str:
            strat_name = active.get("strategy", "UNKNOWN")

            _time_exit_on = False
            try:
                _time_exit_on = bool(is_time_exit_enabled())
            except Exception:
                _time_exit_on = False

            if not _time_exit_on:
                logger.debug(f"[{symbol}] TIME_EXIT skipped (disabled)")
            else:
                entry_dt = datetime.strptime(
                    entry_time_str, '%Y-%m-%d %I:%M:%S %p'
                )
                entry_dt = entry_dt.replace(tzinfo=PKT)
                held_min = (datetime.now(PKT) - entry_dt).total_seconds() / 60

                r_th_early = _r_thresholds_per_class(
                    symbol, get_trading_config()
                )
                bars_cap_min = float(
                    r_th_early.get("max_hold_bars", 28)
                ) * 60.0

                _live_hold = float(_cc_get_num('hold_minutes', 180))

                stored_hold = active.get('hold_time_minutes')
                _source = None
                effective_cap = None
                if stored_hold is not None:
                    try:
                        effective_cap = float(stored_hold)
                        _source = (
                            f"regime={active.get('regime_at_entry', '?')}"
                        )
                    except (TypeError, ValueError):
                        effective_cap = None

                if effective_cap is None:
                    try:
                        _cfg_hold = _get_central_config(
                            symbol, strat_name, "UNKNOWN"
                        )
                        effective_cap = float(
                            _cfg_hold.get("hold_minutes", _live_hold)
                        )
                        _source = f"config_center={effective_cap:.0f}m"
                    except Exception:
                        effective_cap = min(_live_hold, bars_cap_min)
                        _source = "bars_cap"

                if held_min > effective_cap:
                    logger.warning(
                        f"[{symbol}] MAX HOLD {held_min:.0f}m exceeded "
                        f"(cap {effective_cap:.0f}m, source={_source}, "
                        f"strategy={strat_name}) - exiting"
                    )
                    try:
                        qty_str_exit = adjust_qty(
                            abs(float(amt)), f['stepSize'], f['minQty']
                        )
                        with _requests_lock:
                            client.futures_create_order(
                                symbol=pair, side=close_side, type='MARKET',
                                quantity=qty_str_exit, reduceOnly=True,
                                newClientOrderId=_new_cid("time_exit", pair),
                            )
                        time.sleep(1.5)
                        for _ in range(5):
                            time.sleep(0.8)
                            p = fetch_position_raw(symbol)
                            if p and abs(
                                float(p.get('positionAmt', '0') or 0)
                            ) == 0:
                                break
                        handle_trade_close(symbol, pair, reason="TIME_EXIT")
                        return
                    except Exception as tex:
                        logger.error(f"[{symbol}] time exit failed: {tex}")
    except Exception as e:
        logger.debug(f"[{symbol}] time exit check error: {e}")

    # ATR %
    atr_pct = None
    try:
        with ATR_CACHE_LOCK:
            cached = ATR_CACHE.get(symbol)
            if cached and (time.time() - cached['time'] < 300):
                atr_pct = cached['atr_pct']

        if atr_pct is None:
            from core.client import get_binance_klines
            klines = get_binance_klines(pair, '1h', limit=80)
            if klines is not None and len(klines) >= 15:
                df_atr_closed = klines.iloc[:-1]
                ind_atr = calculate_pro_indicators(df_atr_closed, '1h')
                atr_pct = (
                    (ind_atr['atr'] / entry * 100)
                    if ind_atr and entry > 0 else 0.8
                )
            else:
                atr_pct = 0.8
            with ATR_CACHE_LOCK:
                ATR_CACHE[symbol] = {
                    'atr_pct': atr_pct, 'time': time.time()
                }
    except Exception as e:
        logger.warning(f"ATR calc error {symbol}: {e}")
        atr_pct = 0.8

    cfg = get_trading_config()
    r_th = _r_thresholds_per_class(symbol, cfg)

    if r_mode:
        mark_r = (
            (mark - entry) / risk_unit if is_long
            else (entry - mark) / risk_unit
        )
    else:
        mark_r = None

    # ═══════════════════════════════════════════════════════════
    #  PARTIAL 70% (disabled by default)
    # ═══════════════════════════════════════════════════════════
    try:
        unrealized_usdt = float(raw_pos.get('unRealizedProfit', '0') or 0)
        if unrealized_usdt == 0:
            unrealized_usdt = (
                (mark - entry) * float(amt) if is_long
                else (entry - mark) * abs(float(amt))
            )

        partial_done = active.get('partial_70_done', False)

        _partial_usdt = float(_cc_get_num('partial_close_usdt', 0.0))

        if _partial_usdt > 0 and not partial_done \
                and unrealized_usdt >= _partial_usdt:
            skip_partial = False
            try:
                trigger_price = (
                    entry + (_partial_usdt / abs(float(amt))) if is_long
                    else entry - (_partial_usdt / abs(float(amt)))
                )
                slippage_pct = (
                    abs(mark - trigger_price) / entry * 100 if entry else 0
                )
                if slippage_pct > 0.4:
                    if (is_long and mark < trigger_price) or \
                       (not is_long and mark > trigger_price):
                        logger.warning(
                            f"[{symbol}] partial trigger slipped "
                            f"{slippage_pct:.3f}% adverse - skip"
                        )
                        skip_partial = True
            except Exception as e:
                logger.debug(f"[{symbol}] slippage calc failed: {e}")

            if skip_partial:
                pass
            else:
                close_qty = abs(float(amt)) * 0.70
                close_qty_str = adjust_qty(
                    close_qty, f['stepSize'], f['minQty']
                )

                try:
                    limit_price = (
                        mark * 0.9985 if is_long else mark * 1.0015
                    )
                    limit_price_adj = adjust_price(limit_price, tick)
                    filled = False

                    try:
                        client.futures_create_order(
                            symbol=pair, side=close_side, type='LIMIT',
                            price=limit_price_adj, quantity=close_qty_str,
                            reduceOnly=True, timeInForce='GTX',
                            newClientOrderId=_new_cid("p70_lim", pair),
                        )
                        time.sleep(2.5)
                        fp_check = fetch_position_raw(symbol)
                        qty_after = (
                            abs(float(fp_check.get('positionAmt', '0') or 0))
                            if fp_check else abs(float(amt))
                        )
                        if qty_after < abs(float(amt)) * 0.95:
                            filled = True
                        else:
                            try:
                                oo = client.futures_get_open_orders(
                                    symbol=pair
                                )
                                for o in oo:
                                    if o.get('type') == 'LIMIT' \
                                            and o.get('reduceOnly'):
                                        client.futures_cancel_order(
                                            symbol=pair,
                                            orderId=o['orderId'],
                                        )
                            except Exception:
                                pass
                    except BinanceAPIException as e:
                        logger.debug(
                            f"[{symbol}] partial LIMIT failed {e.code}: {e}"
                        )
                    except Exception as e:
                        logger.debug(
                            f"[{symbol}] partial LIMIT failed: {e}"
                        )

                    if not filled:
                        with _requests_lock:
                            client.futures_create_order(
                                symbol=pair, side=close_side, type='MARKET',
                                quantity=close_qty_str, reduceOnly=True,
                                newClientOrderId=_new_cid("p70_mkt", pair),
                            )

                    fresh = get_active_trade(symbol)
                    if fresh:
                        fresh['partial_70_done'] = True
                        add_active_trade(symbol, fresh)
                    logger.info(
                        f"[{symbol}] PARTIAL 70% closed at "
                        f"${unrealized_usdt:.2f} pnl | qty {close_qty_str}"
                    )
                    try:
                        send_telegram(
                            f"💰 PARTIAL 70% {symbol} @ "
                            f"${unrealized_usdt:.2f} | Locked "
                            f"~${unrealized_usdt * 0.7:.2f}"
                        )
                    except Exception:
                        pass

                    # Cancel old STOPS so we can place a fresh BE
                    try:
                        oo = client.futures_get_open_orders(symbol=pair)
                        for o in oo:
                            otype = (o.get('type') or '').upper()
                            if 'STOP' in otype \
                                    and otype != 'TAKE_PROFIT_MARKET':
                                try:
                                    client.futures_cancel_order(
                                        symbol=pair, orderId=o['orderId'],
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        from core.client import _cancel_algo_order as _ca
                        for o in _get_open_algo_orders(pair):
                            otype = (
                                o.get('type') or o.get('orderType') or ''
                            ).upper()
                            if 'STOP' in otype and 'TAKE_PROFIT' not in otype:
                                oid = o.get('algoId') or o.get('orderId')
                                if oid:
                                    _ca(pair, oid)
                    except Exception:
                        pass

                    # BE SL for remainder
                    try:
                        cur_qty_rem = None
                        for _ in range(3):
                            time.sleep(0.5)
                            fp = fetch_position_raw(symbol)
                            if fp:
                                q = abs(
                                    float(fp.get('positionAmt', '0') or 0)
                                )
                                if q > 0:
                                    cur_qty_rem = q
                                    break
                        if cur_qty_rem is None:
                            cur_qty_rem = abs(float(amt)) * 0.30

                        if cur_qty_rem > 0:
                            if r_mode:
                                partial_r = r_th["partial_stop_r"]
                                be_sl_tmp = (
                                    entry + risk_unit * partial_r if is_long
                                    else entry - risk_unit * partial_r
                                )
                            else:
                                be_sl_tmp = (
                                    entry * 1.0008 if is_long
                                    else entry * 0.9992
                                )
                            be_adj = adjust_price(be_sl_tmp, tick)
                            with _requests_lock:
                                resp_be = client.futures_create_order(
                                    symbol=pair, side=close_side,
                                    type='STOP_MARKET',
                                    stopPrice=be_adj,
                                    quantity=adjust_qty(
                                        cur_qty_rem,
                                        f['stepSize'], f['minQty'],
                                    ),
                                    reduceOnly=True, timeInForce='GTC',
                                    workingType='MARK_PRICE',
                                    newClientOrderId=_new_cid(
                                        "p70_be", pair
                                    ),
                                )
                            be_id = (
                                resp_be.get('orderId')
                                or resp_be.get('algoId')
                            )
                            fresh2 = dict(get_active_trade(symbol) or {})
                            if fresh2:
                                fresh2['sl'] = be_adj
                                fresh2['sl_id'] = be_id
                                fresh2['sl_level'] = 1
                                add_active_trade(symbol, fresh2)
                    except Exception as e:
                        logger.error(
                            f"[{symbol}] partial->BE SL FAILED: {e} "
                            f"- emergency close"
                        )
                        try:
                            send_telegram(
                                f"🚨 {symbol} BE SL failed after partial - "
                                f"emergency close 30%"
                            )
                        except Exception:
                            pass
                        try:
                            threading.Thread(
                                target=emergency_close_retry,
                                args=(symbol, pair, close_side), daemon=True,
                            ).start()
                        except Exception:
                            pass
                    return
                except Exception as e:
                    logger.error(f"[{symbol}] partial 70% close failed: {e}")
    except Exception as e:
        logger.warning(f"[{symbol}] partial block error: {e}")

    # ═══════════════════════════════════════════════════════════
    #  update_sl
    # ═══════════════════════════════════════════════════════════
    def update_sl(new_sl, new_level, force=False):
        nonlocal current_sl_price, last_sl_level, sl_id_stored
        fresh = get_active_trade(symbol)
        if not fresh:
            return False
        if not force and fresh.get('sl_level', 0) >= new_level:
            return False
        if force and fresh.get('sl_level', 0) > new_level:
            return False

        cur_qty = adjust_qty(
            abs(float(amt)), f['stepSize'], f['minQty']
        )
        try:
            fresh_pos = fetch_position_raw(symbol)
            if fresh_pos and float(
                fresh_pos.get('positionAmt', '0')
            ) != 0:
                cur_qty = adjust_qty(
                    abs(float(fresh_pos['positionAmt'])),
                    f['stepSize'], f['minQty'],
                )
        except Exception as e:
            logger.warning(f"[{symbol}] SL qty re-check failed: {e}")
        if float(cur_qty) <= 0:
            logger.error(f"[{symbol}] SL zero qty, skipping")
            return False

        new_sl_adj = adjust_price(new_sl, tick)
        try:
            new_sl_f = float(new_sl_adj)
        except (TypeError, ValueError):
            logger.error(f"[{symbol}] SL parse failed for {new_sl_adj!r}")
            return False

        # Monotonic downgrade guard.
        if current_sl_price > 0:
            if is_long and new_sl_f <= current_sl_price:
                logger.debug(
                    f"[{symbol}] SL downgrade blocked: "
                    f"new {new_sl_f} <= current {current_sl_price} (LONG)"
                )
                return False
            if (not is_long) and new_sl_f >= current_sl_price:
                logger.debug(
                    f"[{symbol}] SL downgrade blocked: "
                    f"new {new_sl_f} >= current {current_sl_price} (SHORT)"
                )
                return False

        # ── REV 1.10.0 — skip if new SL is already crossed by mark ──
        if is_long and new_sl_f >= mark:
            logger.debug(
                f"[{symbol}] update_sl skip: new SL {new_sl_f} >= "
                f"mark {mark} (would -2021)"
            )
            return False
        if (not is_long) and new_sl_f <= mark:
            logger.debug(
                f"[{symbol}] update_sl skip: new SL {new_sl_f} <= "
                f"mark {mark} (would -2021)"
            )
            return False

        old_sl_id = sl_id_stored
        for attempt in range(3):
            try:
                refresh_timestamp()

                def _place_new_sl():
                    return client.futures_create_order(
                        symbol=pair, side=close_side, type='STOP_MARKET',
                        stopPrice=new_sl_adj, quantity=cur_qty,
                        reduceOnly=True,
                        timeInForce='GTC', workingType='MARK_PRICE',
                        newClientOrderId=_new_cid(
                            f"upd_l{new_level}", pair
                        ),
                    )
                new_resp = _run_with_timeout(
                    _place_new_sl, 5, f"update_sl:{pair}"
                )
                new_id = (
                    new_resp.get('orderId') or new_resp.get('algoId')
                )
                if not new_id:
                    raise ValueError("no orderId")

                try:
                    if old_sl_id and old_sl_id != new_id:
                        cancel_specific_sl(pair, old_sl_id)
                    cancel_all_sl_stops(pair, except_ids=[new_id])
                except Exception as e:
                    logger.warning(
                        f"[{symbol}] post-place SL sweep failed: {e} "
                        f"(new SL unaffected)"
                    )

                current_sl_price = new_sl_f
                last_sl_level = new_level
                sl_id_stored = new_id
                fresh = get_active_trade(symbol)
                if fresh:
                    fresh['sl'] = new_sl_adj
                    fresh['sl_id'] = new_id
                    fresh['sl_level'] = last_sl_level
                    add_active_trade(symbol, fresh)
                if force:
                    logger.info(
                        f" [{symbol}] SL → trail {new_sl_adj} "
                        f"(level {new_level})"
                    )
                else:
                    logger.info(
                        f" [{symbol}] SL → level {new_level} "
                        f"(price {new_sl_adj})"
                    )
                return True

            except BinanceAPIException as e:
                if e.code == -2021:
                    logger.debug(
                        f"[{symbol}] update_sl -2021 (stop crossed by "
                        f"mark) — skipping update, will retry next cycle"
                    )
                    return False
                if handle_order_filter_error(pair, e):
                    f2 = get_filters(pair)
                    new_sl_adj = adjust_price(new_sl, f2['tickSize'])
                    try:
                        new_sl_f = float(new_sl_adj)
                    except (TypeError, ValueError):
                        return False
                    cur_qty = adjust_qty(
                        abs(float(amt)), f2['stepSize'], f2['minQty']
                    )
                    continue
                logger.warning(
                    f"[{symbol}] SL update attempt {attempt+1}/3 "
                    f"({e.code}): {e} (old SL still active)"
                )
                time.sleep(1)
            except Exception as e:
                logger.warning(
                    f"[{symbol}] SL update attempt {attempt+1}/3: {e} "
                    f"(old SL still active)"
                )
                time.sleep(1)
        return False

    # ═══════════════════════════════════════════════════════════
    #  STEP-BASED SL UPDATES (BE → Lock1 → Lock2)
    # ═══════════════════════════════════════════════════════════
    if r_mode:
        if mark_r > r_th["be_r"] and last_sl_level < 1:
            be_sl = (
                entry + risk_unit * r_th["be_stop_r"] if is_long
                else entry - risk_unit * r_th["be_stop_r"]
            )
            if (is_long and current_sl_price < be_sl) or \
               (not is_long and current_sl_price > be_sl):
                update_sl(be_sl, 1)
        if mark_r > r_th["lock1_r"] and last_sl_level < 2:
            new_sl = (
                entry + risk_unit * r_th["lock1_stop_r"] if is_long
                else entry - risk_unit * r_th["lock1_stop_r"]
            )
            update_sl(new_sl, 2)
        if mark_r > r_th["lock2_r"] and last_sl_level < 3:
            new_sl = (
                entry + risk_unit * r_th["lock2_stop_r"] if is_long
                else entry - risk_unit * r_th["lock2_stop_r"]
            )
            update_sl(new_sl, 3)
    else:
        be_level = max(0.25, atr_pct * cfg.get('be_factor', 0.6))
        lock1_pct = cfg.get('lock1_pct', 0.9)
        lock2_pct = cfg.get('lock2_pct', 2.2)

        if pnl_pct > be_level and last_sl_level < 1:
            be_sl = entry * 1.0005 if is_long else entry * 0.9995
            if (is_long and current_sl_price < be_sl) or \
               (not is_long and current_sl_price > be_sl):
                update_sl(be_sl, 1)
        if pnl_pct > lock1_pct and last_sl_level < 2:
            new_sl = entry * 1.003 if is_long else entry * 0.997
            update_sl(new_sl, 2)
        if pnl_pct > lock2_pct and last_sl_level < 3:
            new_sl = entry * 1.006 if is_long else entry * 0.994
            update_sl(new_sl, 3)

    # ═══════════════════════════════════════════════════════════
    #  CONTINUOUS TRAILING SL
    # ═══════════════════════════════════════════════════════════
    trail_min_level    = int(_cc_get_num('trail_min_level',    1))
    trail_distance_r   = float(_cc_get_num('trail_distance_r',   0.50))
    trail_hysteresis_r = float(_cc_get_num('trail_hysteresis_r', 0.10))

    if r_mode and mark_r is not None and mark_r > 0 \
            and last_sl_level >= trail_min_level:
        try:
            trail_dist = risk_unit * trail_distance_r
            min_trail = float(tick) * 10
            if trail_dist < min_trail:
                trail_dist = min_trail

            if is_long:
                trail_sl = mark - trail_dist
                be_floor = entry + risk_unit * r_th["be_stop_r"]
                if trail_sl < be_floor:
                    trail_sl = be_floor
                improvement_r = (trail_sl - current_sl_price) / risk_unit
                if improvement_r >= trail_hysteresis_r:
                    update_sl(trail_sl, last_sl_level, force=True)
            else:
                trail_sl = mark + trail_dist
                be_ceiling = entry - risk_unit * r_th["be_stop_r"]
                if trail_sl > be_ceiling:
                    trail_sl = be_ceiling
                improvement_r = (current_sl_price - trail_sl) / risk_unit
                if improvement_r >= trail_hysteresis_r:
                    update_sl(trail_sl, last_sl_level, force=True)
        except Exception as e:
            logger.debug(f"[{symbol}] trailing SL failed: {e}")


# ═════════════════════════════════════════════════════════════
#  Trade manager loop
#  REV 1.10.0 — per-symbol lock around manage + reconcile
# ═════════════════════════════════════════════════════════════
def trade_manager_loop():
    iteration = 0
    logger.info(" Trade manager thread started")
    while not is_stopped():
        try:
            with BOT_TRACKED_LOCK:
                symbols = list(bot_tracked_symbols)
            for symbol in symbols:
                _lk = get_symbol_manage_lock(symbol)
                with _lk:
                    try:
                        manage_single_trade(symbol)
                    except Exception as e:
                        logger.debug(f"[TM] manage {symbol}: {e}")

                    # Stagger reconcile per symbol so all tracked
                    # symbols don't hit the API in the same burst.
                    try:
                        _offset = abs(hash(symbol)) % 6
                    except Exception:
                        _offset = 0
                    if (iteration + _offset) % 6 == 0:
                        try:
                            reconcile_active_trade(symbol)
                        except Exception as e:
                            logger.debug(f"[TM] reconcile {symbol}: {e}")
            iteration += 1
            for _ in range(5):
                if is_stopped():
                    break
                time.sleep(1)
        except Exception as e:
            logger.error(f"Trade manager error: {e}")
            time.sleep(10)
    logger.info(" Trade manager stopped")