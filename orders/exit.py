"""
orders/exit.py — Trade close and emergency close.

REV 1.6.1 (2026-10-03) — CORRECTNESS HARDENING:
  ✅ FIXED: `_cc_get(k, d) or d` silently coerced legitimate 0 values
     (cooldown_after_sl_min=0, cooldown_after_tp_min=0, tiny_loss_r=0)
     back to their defaults. Zero is a valid runtime value for all three
     (validation allows it). Replaced with explicit None-check helper
     `_cc_get_num()`.
  ✅ FIXED: get_total_pnl() hardcoded limit=200 → silently truncated
     income history on long trades with many funding-fee records →
     understated PnL → wrong SL/TP classification → wrong daily-loss
     count. Now paginates via CONFIG.income_page_size and
     CONFIG.max_income_pages (which existed but were unused).
  ✅ unmanaged_positions.json now anchored to CSV_EXIT_FILE's parent
     (DATA_DIR) instead of CWD-relative. Matches every other state file.
  ✅ robust_cancel_all() failure now logs WARNING (was silent); caller
     still proceeds (cooldown blocks re-entry), but operator sees it.
  ✅ Removed redundant flush_active_trades() call —
     state.remove_active_trade() already flushes internally.

REV 1.6.0 (2026-10-02) — RUNTIME TOGGLE AWARENESS:
  ✅ Removed cached imports COOLDOWN_AFTER_SL_MIN, COOLDOWN_AFTER_TP_MIN
     from core.state. Both now read LIVE via _cc_get() so UI runtime
     changes take effect on the very next close.
  ✅ `_TINY_LOSS_R` promoted to config_center.GLOBAL["tiny_loss_r"].

REV 1.5.6 (2026-10-02) — DAILY-LOSS COUNT CLARITY.
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
from pathlib import Path

from core.client import (
    fetch_position_raw, get_filters, adjust_qty,
    refresh_timestamp, _get_open_algo_orders, _cancel_algo_order,
    invalidate_account_cache, send_telegram, logger,
    _requests_lock,
)
from core.config import CONFIG
from core.state import (
    PKT, get_active_trade, remove_active_trade,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    cooldown_until, COOLDOWN_LOCK, save_cooldowns,
    fail_counts, FAIL_COUNTS_LOCK,
    daily_tracker,
    CSV_EXIT_FILE,
)

# ── REV 1.6.0 — Live runtime-tunable reads ──
from core.config_center import get as _cc_get

from .utils import _cl, _JUST_CLOSED, _JUST_CLOSED_LOCK


# ═════════════════════════════════════════════════════════════
#  REV 1.6.1 — DATA_DIR-ANCHORED PATHS
#  All state files live next to each other; do NOT use CWD-relative
#  paths (breaks when the bot is launched from a different directory).
# ═════════════════════════════════════════════════════════════
_UNMANAGED_POSITIONS_FILE = str(
    Path(CSV_EXIT_FILE).parent / "unmanaged_positions.json"
)


# ═════════════════════════════════════════════════════════════
#  Reason classification
# ═════════════════════════════════════════════════════════════
_MANUAL_UI_REASONS = frozenset({"MANUAL_UI"})


# ═════════════════════════════════════════════════════════════
#  REV 1.5.5 — FIRST-REASON TRACKER (diagnostic only)
# ═════════════════════════════════════════════════════════════
_CLOSED_REASONS: dict[str, str] = {}


# ═════════════════════════════════════════════════════════════
#  REV 1.6.1 — SAFE CC NUMERIC READ
#  `_cc_get(k, default) or default` was silently coercing legitimate
#  0 values back to the default. This helper preserves 0 while still
#  returning `default` when the key is genuinely absent (None).
# ═════════════════════════════════════════════════════════════
def _cc_get_num(key: str, default):
    v = _cc_get(key, None)
    return default if v is None else v


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
        with open(_UNMANAGED_POSITIONS_FILE, "a", encoding='utf-8') as f:
            f.write(f"{datetime.now(PKT).isoformat()} - {symbol} - {pair} - "
                    f"MANUAL CLOSE REQUIRED\n")
    except Exception as e:
        logger.warning(f"Failed to log unmanaged position {symbol}: {e}")
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
#  REV 1.6.1 — now paginates via CONFIG.income_page_size /
#              CONFIG.max_income_pages. The old limit=200 was silently
#              truncating income history on long trades.
# ═════════════════════════════════════════════════════════════
def get_total_pnl(symbol, entry_time_ms):
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        return 0.0
    total = 0.0
    page_size = int(CONFIG.income_page_size)
    max_pages = int(CONFIG.max_income_pages)
    try:
        refresh_timestamp()
        cursor = int(entry_time_ms)
        for _page in range(max_pages):
            income = client.futures_income_history(
                symbol=pair, startTime=cursor, limit=page_size
            )
            if not income:
                break
            for entry in income:
                if entry['symbol'] == pair and entry['incomeType'] in (
                    'REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE'
                ):
                    total += float(entry['income'])
            # Last page → fewer records than requested → done.
            if len(income) < page_size:
                break
            # Advance cursor past the latest timestamp we saw. Guard
            # against non-progress to avoid an infinite loop on a
            # misbehaving API.
            last_time = max(int(e.get('time', cursor)) for e in income)
            if last_time <= cursor:
                break
            cursor = last_time + 1
    except Exception as e:
        logger.debug(f"Income sum error {symbol}: {e}")
    return total


# ═════════════════════════════════════════════════════════════
#  handle_trade_close
# ═════════════════════════════════════════════════════════════
def handle_trade_close(symbol, pair=None, reason="closed"):
    if pair is None:
        pair = symbol + 'USDT'

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

    # REV 1.6.1 — log cancel failure (was silent). Proceed regardless:
    # cooldown below will block re-entry; a stray orphan order will be
    # caught by the monitor on the next tick.
    try:
        if not robust_cancel_all(pair):
            logger.warning(
                f"[{symbol}] robust_cancel_all did not confirm full cleanup "
                f"— proceeding with close (monitor will reconcile)"
            )
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

    # ── REV 1.6.0 / 1.6.1: LIVE tiny_loss_r from config_center.
    #    NOTE: 0.0 is a valid value (means "no tiny-loss tolerance").
    #    The old `or 0.30` silently coerced it back to 0.30.
    _tiny_loss_r = float(_cc_get_num("tiny_loss_r", 0.30))
    _is_real_loss = pnl < 0 and (
        reason != "TIME_EXIT" or _r_mult is None or _r_mult <= -_tiny_loss_r
    )

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

    # ── REV 1.6.0 / 1.6.1: LIVE cooldowns from config_center.
    #    NOTE: 0 is a valid value (immediate re-entry allowed). The old
    #    `or 20` / `or 15` idiom silently coerced it back.
    _cooldown_sl = int(_cc_get_num("cooldown_after_sl_min", 20))
    _cooldown_tp = int(_cc_get_num("cooldown_after_tp_min", 15))

    if pnl < 0:
        # ═══════════════════════════════════════════════════════
        #  DAILY LOSS COUNTER
        #  Both SL and RR_COLLAPSE increment the counter here,
        #  because both pass the _is_real_loss check (reason is
        #  not TIME_EXIT). Rollover handling is in DailyTracker.
        # ═══════════════════════════════════════════════════════
        if _is_real_loss:
            daily_tracker.add_loss()
        cooldown_min = _cooldown_sl
        if is_manual_close:
            send_telegram(f"🔻 MANUAL CLOSE {symbol}\nPnL: ${pnl:.2f}")
        elif reason == "TIME_EXIT":
            send_telegram(f"⏰ TIME_EXIT {symbol}\nPnL: ${pnl:.2f}{_r_txt}")
        else:
            send_telegram(f" SL HIT {symbol}\nPnL: ${pnl:.2f}")
    elif pnl > 0:
        cooldown_min = _cooldown_tp
        if is_manual_close:
            send_telegram(f"💰 MANUAL CLOSE {symbol}\nPnL: ${pnl:.2f}")
        elif reason == "TIME_EXIT":
            send_telegram(f"⏰ TIME_EXIT {symbol}\nPnL: ${pnl:.2f}{_r_txt}")
        else:
            send_telegram(f" TP HIT {symbol}\nPnL: ${pnl:.2f}")
    else:
        cooldown_min = _cooldown_tp
        if is_manual_close:
            send_telegram(f"➖ MANUAL CLOSE {symbol} (BE)")
        else:
            send_telegram(f" CLOSED {symbol} (BE)")

    with COOLDOWN_LOCK:
        cooldown_until[symbol] = datetime.now(PKT) + timedelta(minutes=cooldown_min)
    save_cooldowns()

    with BOT_TRACKED_LOCK:
        bot_tracked_symbols.discard(symbol)
    # REV 1.6.1 — remove_active_trade() already flushes internally.
    remove_active_trade(symbol)
    with FAIL_COUNTS_LOCK:
        fail_counts.pop(symbol, None)
    try:
        invalidate_account_cache()
    except Exception:
        pass
    logger.info(f" [{symbol}] Closed OFF ({output_reason})")