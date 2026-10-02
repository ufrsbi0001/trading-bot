"""
orders/manage.py — Trade management loop.

REV 1.7.1 (2026-10-02) — UNIFIED CONFIG CLEANUP:
  ✅ Time-exit fallback now uses `config_center.get_config().hold_minutes`
     instead of removed `_STRATEGY_HOLD_MIN`.
  ✅ Import of `_STRATEGY_HOLD_MIN` removed from `.utils`.

REV 1.7.0 (2026-10-02) — CONFIG CENTER INTEGRATION (Phase 1):
  ✅ Time-exit now prefers `hold_time_minutes` (frozen at entry time
     by entry.py REV 1.7.0) over the live strategy-based cap. This
     prevents regime flips mid-trade from retroactively shortening
     the hold budget. Falls back gracefully for legacy trades.

REV 1.6.3 (2026-09-28) — DEAD IMPORT CLEANUP:
  ✅ Removed two unused imports (zero behaviour change):
       • `from decimal import Decimal`   (never referenced)
       • `from core.config import CONFIG`     (never referenced — every
         config value comes via .utils: MAX_HOLD_MINUTES,
         PARTIAL_CLOSE_USDT, _STRATEGY_HOLD_MIN).

REV 1.6.2 (2026-09-28) — NAKED-SL WINDOW FIX:
  ✅ _detect_tp1_fill_and_move_be() and update_sl() now place the
     NEW stop-loss BEFORE cancelling the OLD one.

REV 1.6.1 (2026-09-28) — DIRECTIONAL NO-DOWNGRADE SL GUARD.
REV 1.6.0 (2026-09-28) — CONTINUOUS TRAILING STOP-LOSS.
REV 1.5.0 (2026-09-28) — SPLIT + P1 FIX.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from core.client import (
    fetch_position_raw, get_filters, adjust_qty, adjust_price,
    refresh_timestamp, _run_with_timeout, _get_open_algo_orders,
    send_telegram, logger,
    _requests_lock,
)
from core.state import (
    PKT, get_active_trade, add_active_trade,
    ATR_CACHE, ATR_CACHE_LOCK,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    is_stopped,
)
from market.indicators import calculate_pro_indicators, get_trading_config

# ── REV 1.7.1 — Unified config source for hold_minutes fallback ──
from core.config_center import get_config as _get_central_config

from .utils import (
    MAX_HOLD_MINUTES, PARTIAL_CLOSE_USDT,
    _cl, _get_risk_unit, _r_thresholds_per_class,
)
from .repair import (
    cancel_all_sl_stops, cancel_specific_sl, reconcile_active_trade,
)
from .exit import emergency_close_retry, handle_trade_close


# ═════════════════════════════════════════════════════════════
#  TRAILING SL TUNABLES  (REV 1.6.0)
# ═════════════════════════════════════════════════════════════
TRAIL_DISTANCE_R    = 0.50   # trail distance behind mark (in R units)
TRAIL_HYSTERESIS_R  = 0.10   # min improvement required to update (R units)
TRAIL_MIN_LEVEL     = 1      # start trailing only after SL reaches BE


# ═════════════════════════════════════════════════════════════
#  TP1-FILL → IMMEDIATE BE  (REV 1.6.2 — SL-FIRST ORDERING)
# ═════════════════════════════════════════════════════════════
def _detect_tp1_fill_and_move_be(symbol, pair, is_long, cur_amt, active):
    """If TP1 has filled but SL is still at initial, move SL to BE now.

    REV 1.6.2 — SL-first ordering: new BE SL is placed BEFORE the old
    initial SL is cancelled, so no naked window exists on failure.
    """
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

    # Is TP1 still on the book?
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
        logger.debug(f"[_detect_tp1_fill] {pair}: open orders fetch failed: {e}")
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

    # ── REV 1.6.2 STEP 1: place NEW BE SL FIRST ──
    try:
        rem_qty = adjust_qty(abs(float(cur_amt)), f['stepSize'], f['minQty'])
        resp = client.futures_create_order(
            symbol=pair, side=close_side, type='STOP_MARKET',
            stopPrice=be_adj, quantity=rem_qty, reduceOnly=True,
            timeInForce='GTC', workingType='MARK_PRICE'
        )
        new_id = resp.get('orderId') or resp.get('algoId')
        if not new_id:
            raise ValueError("BE SL placement returned no orderId")
    except Exception as e:
        logger.error(f"[_detect_tp1_fill] {symbol}: BE SL placement FAILED: {e} "
                     f"(old SL still active — no naked window)")
        try:
            send_telegram(f"⚠️ {symbol} TP1 filled but BE SL placement failed — "
                          f"old SL still active, retrying next scan")
        except Exception:
            pass
        return

    # ── REV 1.6.2 STEP 2: new SL is live. Cancel OLD stops, but EXCLUDE new. ──
    try:
        cancel_all_sl_stops(pair, except_ids=[new_id])
    except Exception as e:
        logger.warning(f"[_detect_tp1_fill] {pair}: post-place SL sweep failed: {e} "
                       f"(new SL {new_id} unaffected; old SL(s) may linger)")

    # ── REV 1.6.2 STEP 3: persist state ──
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
    mark = float(raw_pos['markPrice'])
    is_long = amt > 0
    pnl_pct = ((mark - entry) / entry * 100) if is_long else ((entry - mark) / entry * 100)
    close_side = 'SELL' if is_long else 'BUY'
    f = get_filters(pair)
    tick = f['tickSize']

    # TP1-fill detection
    try:
        _detect_tp1_fill_and_move_be(symbol, pair, is_long, amt, active)
        active = get_active_trade(symbol) or active
        current_sl_price = float(active.get('sl', current_sl_price))
        last_sl_level = active.get('sl_level', last_sl_level)
    except Exception as e:
        logger.debug(f"[{symbol}] TP1-fill detect failed: {e}")

    # ═══════════════════════════════════════════════════════════
    #  TIME EXIT — REV 1.7.0: prefer stored regime-aware hold time
    #             REV 1.7.1: fallback via config_center
    # ═══════════════════════════════════════════════════════════
    try:
        entry_time_str = active.get('entry_time', '')
        if entry_time_str:
            entry_dt = datetime.strptime(entry_time_str, '%Y-%m-%d %I:%M:%S %p')
            entry_dt = entry_dt.replace(tzinfo=PKT)
            held_min = (datetime.now(PKT) - entry_dt).total_seconds() / 60

            r_th_early = _r_thresholds_per_class(symbol, get_trading_config())
            bars_cap_min = float(r_th_early.get("max_hold_bars", 28)) * 60.0

            strat_name = active.get("strategy", "UNKNOWN")

            # ── REV 1.7.0 — prefer stored regime-aware hold time ──
            # `hold_time_minutes` was frozen at entry time (entry.py
            # REV 1.7.0) so regime flips mid-trade don't retroactively
            # change the hold budget. Falls back gracefully for old
            # trades that don't have the field.
            stored_hold = active.get('hold_time_minutes')
            _source = None
            effective_cap = None
            if stored_hold is not None:
                try:
                    effective_cap = float(stored_hold)
                    _source = f"regime={active.get('regime_at_entry', '?')}"
                except (TypeError, ValueError):
                    effective_cap = None

            if effective_cap is None:
                # ── REV 1.7.1 — fallback: config_center hold_minutes (regime-aware) ──
                try:
                    _cfg_hold = _get_central_config(symbol, strat_name, "UNKNOWN")
                    effective_cap = float(_cfg_hold.get("hold_minutes", MAX_HOLD_MINUTES))
                    _source = f"config_center={effective_cap:.0f}m"
                except Exception:
                    effective_cap = min(float(MAX_HOLD_MINUTES), bars_cap_min)
                    _source = "bars_cap"

            if held_min > effective_cap:
                logger.warning(
                    f"[{symbol}] MAX HOLD {held_min:.0f}m exceeded "
                    f"(cap {effective_cap:.0f}m, source={_source}, "
                    f"strategy={strat_name}) - exiting"
                )
                try:
                    qty_str_exit = adjust_qty(abs(float(amt)), f['stepSize'], f['minQty'])
                    with _requests_lock:
                        client.futures_create_order(
                            symbol=pair, side=close_side, type='MARKET',
                            quantity=qty_str_exit, reduceOnly=True
                        )
                    time.sleep(1.5)
                    for _ in range(5):
                        time.sleep(0.8)
                        p = fetch_position_raw(symbol)
                        if p and abs(float(p.get('positionAmt', '0') or 0)) == 0:
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
                atr_pct = (ind_atr['atr'] / entry * 100) if ind_atr and entry > 0 else 0.8
            else:
                atr_pct = 0.8
            with ATR_CACHE_LOCK:
                ATR_CACHE[symbol] = {'atr_pct': atr_pct, 'time': time.time()}
    except Exception as e:
        logger.warning(f"ATR calc error {symbol}: {e}")
        atr_pct = 0.8

    cfg = get_trading_config()
    r_th = _r_thresholds_per_class(symbol, cfg)

    if r_mode:
        mark_r = (mark - entry) / risk_unit if is_long else (entry - mark) / risk_unit
    else:
        mark_r = None

    # Partial 70% (disabled by default)
    try:
        unrealized_usdt = float(raw_pos.get('unRealizedProfit', '0') or 0)
        if unrealized_usdt == 0:
            unrealized_usdt = ((mark - entry) * float(amt) if is_long
                               else (entry - mark) * abs(float(amt)))

        partial_done = active.get('partial_70_done', False)

        if PARTIAL_CLOSE_USDT > 0 and not partial_done and unrealized_usdt >= PARTIAL_CLOSE_USDT:
            skip_partial = False
            try:
                trigger_price = (entry + (PARTIAL_CLOSE_USDT / abs(float(amt))) if is_long
                                 else entry - (PARTIAL_CLOSE_USDT / abs(float(amt))))
                slippage_pct = abs(mark - trigger_price) / entry * 100 if entry else 0
                if slippage_pct > 0.4:
                    if (is_long and mark < trigger_price) or (not is_long and mark > trigger_price):
                        logger.warning(f"[{symbol}] partial trigger slipped "
                                       f"{slippage_pct:.3f}% adverse - skip")
                        skip_partial = True
            except Exception as e:
                logger.debug(f"[{symbol}] slippage calc failed: {e}")

            if skip_partial:
                pass
            else:
                close_qty = abs(float(amt)) * 0.70
                close_qty_str = adjust_qty(close_qty, f['stepSize'], f['minQty'])

                try:
                    limit_price = mark * 0.9985 if is_long else mark * 1.0015
                    limit_price_adj = adjust_price(limit_price, tick)
                    filled = False

                    try:
                        client.futures_create_order(
                            symbol=pair, side=close_side, type='LIMIT',
                            price=limit_price_adj, quantity=close_qty_str,
                            reduceOnly=True, timeInForce='GTX'
                        )
                        time.sleep(2.5)
                        fp_check = fetch_position_raw(symbol)
                        qty_after = abs(float(fp_check.get('positionAmt', '0') or 0)) if fp_check else abs(float(amt))
                        if qty_after < abs(float(amt)) * 0.95:
                            filled = True
                        else:
                            try:
                                oo = client.futures_get_open_orders(symbol=pair)
                                for o in oo:
                                    if o.get('type') == 'LIMIT' and o.get('reduceOnly'):
                                        client.futures_cancel_order(symbol=pair, orderId=o['orderId'])
                            except Exception:
                                pass
                    except Exception:
                        pass

                    if not filled:
                        with _requests_lock:
                            client.futures_create_order(
                                symbol=pair, side=close_side, type='MARKET',
                                quantity=close_qty_str, reduceOnly=True
                            )

                    fresh = get_active_trade(symbol)
                    if fresh:
                        fresh['partial_70_done'] = True
                        add_active_trade(symbol, fresh)
                    logger.info(f"[{symbol}] PARTIAL 70% closed at ${unrealized_usdt:.2f} pnl "
                                f"| qty {close_qty_str}")
                    try:
                        send_telegram(f"💰 PARTIAL 70% {symbol} @ ${unrealized_usdt:.2f} "
                                      f"| Locked ~${unrealized_usdt*0.7:.2f}")
                    except Exception:
                        pass

                    # Cancel ONLY stops, keep TPs (need except_ids for the new BE below)
                    try:
                        oo = client.futures_get_open_orders(symbol=pair)
                        for o in oo:
                            otype = (o.get('type') or '').upper()
                            if 'STOP' in otype and otype != 'TAKE_PROFIT_MARKET':
                                try:
                                    client.futures_cancel_order(symbol=pair, orderId=o['orderId'])
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    try:
                        from core.client import _cancel_algo_order as _ca
                        for o in _get_open_algo_orders(pair):
                            otype = (o.get('type') or o.get('orderType') or '').upper()
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
                                q = abs(float(fp.get('positionAmt', '0') or 0))
                                if q > 0:
                                    cur_qty_rem = q
                                    break
                        if cur_qty_rem is None:
                            cur_qty_rem = abs(float(amt)) * 0.30

                        if cur_qty_rem > 0:
                            if r_mode:
                                partial_r = r_th["partial_stop_r"]
                                be_sl_tmp = (entry + risk_unit * partial_r if is_long
                                             else entry - risk_unit * partial_r)
                            else:
                                be_sl_tmp = entry * 1.0008 if is_long else entry * 0.9992
                            be_adj = adjust_price(be_sl_tmp, tick)
                            with _requests_lock:
                                resp_be = client.futures_create_order(
                                    symbol=pair, side=close_side, type='STOP_MARKET',
                                    stopPrice=be_adj,
                                    quantity=adjust_qty(cur_qty_rem, f['stepSize'], f['minQty']),
                                    reduceOnly=True, timeInForce='GTC', workingType='MARK_PRICE'
                                )
                            be_id = resp_be.get('orderId') or resp_be.get('algoId')
                            fresh2 = dict(get_active_trade(symbol) or {})
                            if fresh2:
                                fresh2['sl'] = be_adj
                                fresh2['sl_id'] = be_id
                                fresh2['sl_level'] = 1
                                add_active_trade(symbol, fresh2)
                    except Exception as e:
                        logger.error(f"[{symbol}] partial->BE SL FAILED: {e} - emergency close")
                        try:
                            send_telegram(f"🚨 {symbol} BE SL failed after partial - emergency close 30%")
                        except Exception:
                            pass
                        try:
                            threading.Thread(
                                target=emergency_close_retry,
                                args=(symbol, pair, close_side), daemon=True
                            ).start()
                        except Exception:
                            pass
                    return
                except Exception as e:
                    logger.error(f"[{symbol}] partial 70% close failed: {e}")
    except Exception as e:
        logger.warning(f"[{symbol}] partial block error: {e}")

    # ═══════════════════════════════════════════════════════════
    #  update_sl  (REV 1.6.0 — force param)
    #             (REV 1.6.1 — directional no-downgrade guard)
    #             (REV 1.6.2 — SL-first ordering)
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

        cur_qty = adjust_qty(abs(float(amt)), f['stepSize'], f['minQty'])
        try:
            fresh_pos = fetch_position_raw(symbol)
            if fresh_pos and float(fresh_pos.get('positionAmt', '0')) != 0:
                cur_qty = adjust_qty(abs(float(fresh_pos['positionAmt'])), f['stepSize'], f['minQty'])
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

        # ── REV 1.6.1 — directional no-downgrade guard ──
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

        # ── REV 1.6.2 — SL-FIRST: place new SL, THEN cancel old ──
        old_sl_id = sl_id_stored
        for attempt in range(3):
            try:
                refresh_timestamp()
                def _place_new_sl():
                    return client.futures_create_order(
                        symbol=pair, side=close_side, type='STOP_MARKET',
                        stopPrice=new_sl_adj, quantity=cur_qty,
                        reduceOnly=True,
                        timeInForce='GTC', workingType='MARK_PRICE'
                    )
                new_resp = _run_with_timeout(_place_new_sl, 5, f"update_sl:{pair}")
                new_id = new_resp.get('orderId') or new_resp.get('algoId')
                if not new_id:
                    raise ValueError("no orderId")

                # New SL is live. Now cancel the old one (by ID if we
                # know it, else sweep-all-except-new).
                try:
                    if old_sl_id:
                        cancel_specific_sl(pair, old_sl_id)
                    # Also sweep any unknown orphan stops (excluding new)
                    cancel_all_sl_stops(pair, except_ids=[new_id])
                except Exception as e:
                    logger.warning(f"[{symbol}] post-place SL sweep failed: {e} "
                                   f"(new SL unaffected)")

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
                    logger.info(f" [{symbol}] SL → trail {new_sl_adj} (level {new_level})")
                else:
                    logger.info(f" [{symbol}] SL → level {new_level} (price {new_sl_adj})")
                return True
            except Exception as e:
                # New placement failed — old SL is STILL ACTIVE.
                # Do NOT cancel-all here; that would create the naked
                # window we're trying to eliminate.
                logger.warning(f"[{symbol}] SL update attempt {attempt+1}/3: {e} "
                               f"(old SL still active)")
                time.sleep(1)
        return False

    # ═══════════════════════════════════════════════════════════
    #  STEP-BASED SL UPDATES (BE → Lock1 → Lock2)
    # ═══════════════════════════════════════════════════════════
    if r_mode:
        if mark_r > r_th["be_r"] and last_sl_level < 1:
            be_sl = (entry + risk_unit * r_th["be_stop_r"] if is_long
                     else entry - risk_unit * r_th["be_stop_r"])
            if (is_long and current_sl_price < be_sl) or (not is_long and current_sl_price > be_sl):
                update_sl(be_sl, 1)
        if mark_r > r_th["lock1_r"] and last_sl_level < 2:
            new_sl = (entry + risk_unit * r_th["lock1_stop_r"] if is_long
                      else entry - risk_unit * r_th["lock1_stop_r"])
            update_sl(new_sl, 2)
        if mark_r > r_th["lock2_r"] and last_sl_level < 3:
            new_sl = (entry + risk_unit * r_th["lock2_stop_r"] if is_long
                      else entry - risk_unit * r_th["lock2_stop_r"])
            update_sl(new_sl, 3)
    else:
        be_level = max(0.25, atr_pct * cfg.get('be_factor', 0.6))
        lock1_pct = cfg.get('lock1_pct', 0.9)
        lock2_pct = cfg.get('lock2_pct', 2.2)

        if pnl_pct > be_level and last_sl_level < 1:
            be_sl = entry * 1.0005 if is_long else entry * 0.9995
            if (is_long and current_sl_price < be_sl) or (not is_long and current_sl_price > be_sl):
                update_sl(be_sl, 1)
        if pnl_pct > lock1_pct and last_sl_level < 2:
            new_sl = entry * 1.003 if is_long else entry * 0.997
            update_sl(new_sl, 2)
        if pnl_pct > lock2_pct and last_sl_level < 3:
            new_sl = entry * 1.006 if is_long else entry * 0.994
            update_sl(new_sl, 3)

    # ═══════════════════════════════════════════════════════════
    #  CONTINUOUS TRAILING SL  (REV 1.6.0)
    # ═══════════════════════════════════════════════════════════
    if r_mode and mark_r is not None and mark_r > 0 and last_sl_level >= TRAIL_MIN_LEVEL:
        try:
            trail_dist = risk_unit * TRAIL_DISTANCE_R
            min_trail = float(tick) * 10
            if trail_dist < min_trail:
                trail_dist = min_trail

            if is_long:
                trail_sl = mark - trail_dist
                be_floor = entry + risk_unit * r_th["be_stop_r"]
                if trail_sl < be_floor:
                    trail_sl = be_floor
                improvement_r = (trail_sl - current_sl_price) / risk_unit
                if improvement_r >= TRAIL_HYSTERESIS_R:
                    update_sl(trail_sl, last_sl_level, force=True)
            else:
                trail_sl = mark + trail_dist
                be_ceiling = entry - risk_unit * r_th["be_stop_r"]
                if trail_sl > be_ceiling:
                    trail_sl = be_ceiling
                improvement_r = (current_sl_price - trail_sl) / risk_unit
                if improvement_r >= TRAIL_HYSTERESIS_R:
                    update_sl(trail_sl, last_sl_level, force=True)
        except Exception as e:
            logger.debug(f"[{symbol}] trailing SL failed: {e}")


# ═════════════════════════════════════════════════════════════
#  Trade manager loop
# ═════════════════════════════════════════════════════════════
def trade_manager_loop():
    iteration = 0
    logger.info(" Trade manager thread started")
    while not is_stopped():
        try:
            with BOT_TRACKED_LOCK:
                symbols = list(bot_tracked_symbols)
            for symbol in symbols:
                try:
                    manage_single_trade(symbol)
                except Exception as e:
                    logger.debug(f"[TM] manage {symbol}: {e}")
                if iteration % 6 == 0:
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