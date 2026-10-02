"""
orders/exit.py — Trade close and emergency close.

REV 1.5.6 (2026-10-02) — DAILY-LOSS COUNT CLARITY:
  ✅ Comment added to make it explicit that BOTH SL and
     RR_COLLAPSE (and non-tiny TIME_EXIT losses) increment the
     daily loss counter. Behaviour unchanged from REV 1.5.5 —
     only documentation clarity. The actual rollover bug was in
     core/state.py's DailyTracker (fixed in state REV 1.3.16).

REV 1.5.5 (2026-09-29) — DUPLICATE-CLOSE REASON TRACKING.
REV 1.5.4 (2026-09-28) — DEAD IMPORT CLEANUP.
REV 1.5.3 (2026-09-28) — DEAD CONSTANT REMOVAL.
REV 1.5.2 (2026-09-28) — MANUAL_UI REASON HANDLING.
REV 1.5.1 (2026-09-28) — DECISION ENGINE FEEDBACK.
REV 1.5.0 (2026-09-28) — SPLIT FROM orders.py.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from core.client import (
    fetch_position_raw, get_filters, adjust_qty,
    refresh_timestamp, _get_open_algo_orders, _cancel_algo_order,
    invalidate_account_cache, send_telegram, logger,
    _requests_lock,
)
from core.state import (
    PKT, get_active_trade, remove_active_trade, flush_active_trades,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    cooldown_until, COOLDOWN_LOCK, save_cooldowns,
    fail_counts, FAIL_COUNTS_LOCK,
    daily_tracker,
    CSV_EXIT_FILE,
    COOLDOWN_AFTER_SL_MIN, COOLDOWN_AFTER_TP_MIN,
)

from .utils import _cl, _JUST_CLOSED, _JUST_CLOSED_LOCK


# ═════════════════════════════════════════════════════════════
#  Reason classification
# ═════════════════════════════════════════════════════════════
# Reasons that mean "the user or an external event closed this,
# and the caller is waiting on a response". These get a short
# PnL-retry window and a distinct Telegram message.
_MANUAL_UI_REASONS = frozenset({"MANUAL_UI"})

# PATCH: a TIME_EXIT loss smaller than this fraction of planned risk
# is not counted as a loss by the daily limit / loss-streak guard.
_TINY_LOSS_R = 0.30


# ═════════════════════════════════════════════════════════════
#  REV 1.5.5 — FIRST-REASON TRACKER (diagnostic only)
# ═════════════════════════════════════════════════════════════
_CLOSED_REASONS: dict[str, str] = {}


# ═════════════════════════════════════════════════════════════
#  Emergency close retry loop
# ═════════════════════════════════════════════════════════════
def emergency_close_retry(symbol, pair, close_side):
    client = _cl()
    if client is None:
        logger.error(f"[{symbol}] emergency_close_retry: client not initialized")
        return

    for attempt in range(10):
        try:
            refresh_timestamp()
            with _requests_lock:
                pos_arr = client.futures_position_information(symbol=pair)
            if not pos_arr:
                time.sleep(5); continue
            amt = abs(float(pos_arr[0].get('positionAmt', 0) or 0))
            if amt == 0:
                logger.info(f"✅ EMERGENCY CLOSE verified for {symbol}")
                send_telegram(f"✅ EMERGENCY CLOSE {symbol}")
                try: handle_trade_close(symbol, pair, reason="emergency_close")
                except Exception: pass
                return

            f = get_filters(pair)
            qty_str = adjust_qty(amt, f['stepSize'], f['minQty'])
            with _requests_lock:
                client.futures_create_order(
                    symbol=pair, side=close_side, type='MARKET',
                    quantity=qty_str, reduceOnly=True
                )
            logger.info(f" EMERGENCY CLOSE attempt {attempt+1} {symbol}")

            for _ in range(5):
                time.sleep(1)
                try:
                    with _requests_lock:
                        pos = client.futures_position_information(symbol=pair)[0]
                    if float(pos['positionAmt']) == 0:
                        logger.info(f"✅ EMERGENCY CLOSE verified for {symbol}")
                        send_telegram(f"✅ EMERGENCY CLOSE {symbol}")
                        try: handle_trade_close(symbol, pair, reason="emergency_close")
                        except Exception: pass
                        return
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Emergency close attempt {attempt+1} failed: {e}")
        time.sleep(5)

    logger.critical(f"❌ Could not close {symbol}")
    try:
        with open("unmanaged_positions.json", "a", encoding='utf-8') as f:
            f.write(f"{datetime.now(PKT).isoformat()} - {symbol} - {pair} - "
                    f"MANUAL CLOSE REQUIRED\n")
    except Exception:
        pass
    send_telegram(f"🚨 CRITICAL {symbol} – MANUAL CHECK REQUIRED")


# ═════════════════════════════════════════════════════════════
#  robust_cancel_all
# ═════════════════════════════════════════════════════════════
def robust_cancel_all(symbol_pair):
    client = _cl()
    if client is None:
        return False
    for attempt in range(3):
        try:
            refresh_timestamp()
            with _requests_lock:
                try:
                    client.futures_cancel_all_open_orders(symbol=symbol_pair)
                except Exception as e:
                    logger.debug(f"robust_cancel {symbol_pair}: {e}")
                for o in _get_open_algo_orders(symbol_pair):
                    oid = o.get('algoId') or o.get('orderId')
                    if oid:
                        _cancel_algo_order(symbol_pair, oid)
                try:
                    open_orders = client.futures_get_open_orders(symbol=symbol_pair)
                    for o in open_orders:
                        try:
                            client.futures_cancel_order(symbol=symbol_pair, orderId=o['orderId'])
                        except Exception:
                            pass
                except Exception:
                    pass
                for o in _get_open_algo_orders(symbol_pair):
                    oid = o.get('algoId') or o.get('orderId')
                    if oid:
                        _cancel_algo_order(symbol_pair, oid)
            time.sleep(0.3)
            remaining = client.futures_get_open_orders(symbol=symbol_pair)
            remaining_algo = _get_open_algo_orders(symbol_pair)
            if not remaining and not remaining_algo:
                logger.info(f"  [{symbol_pair}] All orphaned orders cancelled")
                return True
        except Exception as e:
            logger.debug(f"robust_cancel {symbol_pair} {attempt+1}: {e}")
        time.sleep(1)
    return False


# ═════════════════════════════════════════════════════════════
#  get_total_pnl
# ═════════════════════════════════════════════════════════════
def get_total_pnl(symbol, entry_time_ms):
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        return 0.0
    total = 0.0
    try:
        refresh_timestamp()
        income = client.futures_income_history(
            symbol=pair, startTime=entry_time_ms, limit=200
        )
        for entry in income:
            if entry['symbol'] == pair and entry['incomeType'] in (
                'REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE'
            ):
                total += float(entry['income'])
    except Exception as e:
        logger.debug(f"Income sum error {symbol}: {e}")
    return total


# ═════════════════════════════════════════════════════════════
#  handle_trade_close
# ═════════════════════════════════════════════════════════════
def handle_trade_close(symbol, pair=None, reason="closed"):
    if pair is None:
        pair = symbol + 'USDT'

    # ── REV 1.5.5: defensive reason normalization ──
    if not reason:
        reason = "closed"

    client = _cl()

    # ═══════════════════════════════════════════════════════════
    #  REV 1.5.5 — DUPLICATE-GUARD WITH FIRST-REASON LOOKUP
    # ═══════════════════════════════════════════════════════════
    with _JUST_CLOSED_LOCK:
        now = time.time()
        stale = [k for k, v in list(_JUST_CLOSED.items()) if now - v > 60]
        for k in stale:
            del _JUST_CLOSED[k]
            _CLOSED_REASONS.pop(k, None)

        if symbol in _JUST_CLOSED:
            first_reason = _CLOSED_REASONS.get(symbol, "unknown")
            logger.info(
                f"[{symbol}] handle_trade_close skip — already closed "
                f"(first_reason={first_reason}, this_call={reason}, "
                f"within 60s)"
            )
            return

        _JUST_CLOSED[symbol] = now
        _CLOSED_REASONS[symbol] = reason

    try:
        robust_cancel_all(pair)
    except Exception as e:
        logger.debug(f"robust_cancel_all in handle_trade_close failed: {e}")

    active = get_active_trade(symbol)
    entry_time_str = active.get('entry_time')
    entry_time_ms = 0
    if entry_time_str:
        try:
            dt = datetime.strptime(entry_time_str, '%Y-%m-%d %I:%M:%S %p')
            entry_time_ms = int(dt.replace(tzinfo=PKT).timestamp() * 1000)
        except Exception as e:
            logger.warning(f"entry_time parse failed {symbol}: {e}")

    if entry_time_ms == 0:
        pnl = 0.0
        reason = "MANUAL"
    else:
        pnl = get_total_pnl(symbol, entry_time_ms)

    # ── PnL retry window ──
    if pnl == 0.0 and reason != "MANUAL":
        retries = 3 if reason in _MANUAL_UI_REASONS else 10
        for _retry in range(retries):
            time.sleep(1.5)
            pnl_retry = get_total_pnl(symbol, entry_time_ms)
            if pnl_retry != 0.0:
                pnl = pnl_retry
                logger.info(f"[{symbol}] PnL retry {_retry+1} got {pnl_retry:.4f} for {reason}")
                break

    if pnl == 0.0:
        reason = "MANUAL" if reason in ("closed", "detected amt=0") else reason
    else:
        # PATCH A: TIME_EXIT is kept as its own reason (was mislabeled SL/TP)
        if reason in ("closed", "detected amt=0", "emergency_close"):
            reason = "SL" if pnl < 0 else "TP"

    # ── PATCH B: R-multiple of this close and "real loss" flag ──
    _r_mult = None
    try:
        _e = float(active.get('entry', 0) or 0)
        _isl = float(active.get('initial_sl', 0) or 0)
        _q = float(active.get('qty', 0) or 0)
        _risk_usd = abs(_e - _isl) * _q
        if _risk_usd > 0:
            _r_mult = pnl / _risk_usd
    except Exception:
        _r_mult = None

    # ── REV 1.5.6: explicit real-loss classification ──
    # Any of the following counts as a "real loss" (feeds daily loss
    # counter AND decision-engine loss-streak guard):
    #   • SL hit        (pnl < 0, reason="SL")
    #   • RR_COLLAPSE   (pnl < 0, reason="RR_COLLAPSE")
    #   • MANUAL close with negative pnl
    #   • TIME_EXIT with R <= -0.30 (real loss, not tiny noise)
    # Tiny TIME_EXIT losses (|R| < 0.30) are ignored.
    _is_real_loss = pnl < 0 and (
        reason != "TIME_EXIT" or _r_mult is None or _r_mult <= -_TINY_LOSS_R
    )

    # ── Normalize output reason for CSV/logs ──
    output_reason = "MANUAL" if reason in _MANUAL_UI_REASONS else reason
    is_manual_close = reason in _MANUAL_UI_REASONS

    try:
        with open(CSV_EXIT_FILE, 'a', encoding='utf-8') as ef:
            ef.write(f"{datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')},"
                     f"{symbol},{pnl:.2f},{output_reason}\n")
    except Exception as e:
        logger.warning(f"Exit log write failed: {e}")

    # ── Feed decision engine memory ──
    try:
        from signals.decision_engine import record_trade_result
        strategy_name = (active.get('strategy', 'UNKNOWN')
                         if active else 'UNKNOWN')
        if pnl < 0 and not _is_real_loss:
            logger.info(f"[{symbol}] tiny TIME_EXIT loss "
                        f"(R={_r_mult if _r_mult is None else round(_r_mult, 2)}) "
                        f"- not fed to loss-streak guard")
        else:
            record_trade_result(strategy_name, pnl)
    except Exception as _de:
        logger.debug(f"[decision] record_trade_result failed: {_de}")

    _r_txt = "" if _r_mult is None else f" ({_r_mult:+.2f}R)"
    if pnl < 0:
        # ═══════════════════════════════════════════════════════
        #  REV 1.5.6 — DAILY LOSS COUNTER
        #  Both SL and RR_COLLAPSE increment the counter here,
        #  because both pass the _is_real_loss check (reason is
        #  not TIME_EXIT). Rollover handling is in DailyTracker.
        # ═══════════════════════════════════════════════════════
        if _is_real_loss:
            daily_tracker.add_loss()
        cooldown_min = COOLDOWN_AFTER_SL_MIN
        if is_manual_close:
            send_telegram(f"🔻 MANUAL CLOSE {symbol}\nPnL: ${pnl:.2f}")
        elif reason == "TIME_EXIT":
            send_telegram(f"⏰ TIME_EXIT {symbol}\nPnL: ${pnl:.2f}{_r_txt}")
        else:
            send_telegram(f" SL HIT {symbol}\nPnL: ${pnl:.2f}")
    elif pnl > 0:
        cooldown_min = COOLDOWN_AFTER_TP_MIN
        if is_manual_close:
            send_telegram(f"💰 MANUAL CLOSE {symbol}\nPnL: ${pnl:.2f}")
        elif reason == "TIME_EXIT":
            send_telegram(f"⏰ TIME_EXIT {symbol}\nPnL: ${pnl:.2f}{_r_txt}")
        else:
            send_telegram(f" TP HIT {symbol}\nPnL: ${pnl:.2f}")
    else:
        cooldown_min = COOLDOWN_AFTER_TP_MIN
        if is_manual_close:
            send_telegram(f"➖ MANUAL CLOSE {symbol} (BE)")
        else:
            send_telegram(f" CLOSED {symbol} (BE)")

    with COOLDOWN_LOCK:
        cooldown_until[symbol] = datetime.now(PKT) + timedelta(minutes=cooldown_min)
    save_cooldowns()

    with BOT_TRACKED_LOCK:
        bot_tracked_symbols.discard(symbol)
    remove_active_trade(symbol)
    flush_active_trades()
    with FAIL_COUNTS_LOCK:
        fail_counts.pop(symbol, None)
    try:
        invalidate_account_cache()
    except Exception:
        pass
    logger.info(f" [{symbol}] Closed OFF ({output_reason})")