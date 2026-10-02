"""
orders/entry.py — Order placement.

REV 1.7.3 (2026-10-02) — COSMETIC CLEANUP:
  ✅ `_apply_vol_class_sizing` — `mult_map` promoted to module-level
     `_VOL_CLASS_QTY_MULT` (clarity vs config_center's
     VOL_CLASS_CAP_MULT which is a different concept).

REV 1.7.2 (2026-10-02) — PHASE 2 CLEANUP:
  ✅ Signal drift cap now uses ACTUAL regime (was hardcoded "UNKNOWN").
     Fetches regime from cached 1h indicator before resolving
     signal_drift_pct from config_center. Prevents silent failures
     if regime-dependent drift tuning is added later.

REV 1.7.1 (2026-10-02) — PHASE 1 CLEANUP:
  ✅ RR check now uses `config_center.get_min_rr(strategy)` (per-strategy
     floors from MIN_RR dict) instead of `cfg['min_rr']` (GLOBAL scalar).
     Fixes mismatch where strategy signal-gen used MIN_RR[strategy]=1.8
     but entry.py read GLOBAL min_rr=1.5.
     Fallback: get_min_rr() returns 1.5 if strategy not in MIN_RR.

REV 1.7.0 (2026-10-02) — CONFIG CENTER INTEGRATION (Phase 1):
  ✅ Added `config_center` import.
  ✅ Signal drift cap now reads `signal_drift_pct` from config_center
     (was hardcoded 0.8%).
  ✅ Regime snapshot added at entry time — stores `regime_at_entry`
     and `hold_time_minutes` in active_trades.json. manage.py will
     use these frozen values instead of the LIVE regime (which can
     flip mid-trade).

REV 1.6.0 (2026-10-01) — SIGNAL DRIFT CAP:
  ✅ Rejects entry if live price drifted > 0.8% from signal price.
     Prevents stale signal entries (e.g., FET 1.8% adverse drift).
  ✅ FIX: Removed redundant `bot_tracked_symbols.discard()` on
     drift reject — the check runs BEFORE slot reservation, so no
     cleanup is needed. Kept the check position (before slot add).

REV 1.5.10 (2026-09-30) — RR COLLAPSE FLOAT TOLERANCE.
REV 1.5.9 (2026-09-30) — SAFETY GUARDS (Bug #3 + Bug #5).
REV 1.5.8 (2026-09-30) — MAX_QTY CAP (fix -4005 loop).
Contains: place_order_fixed.
"""
from __future__ import annotations
import threading
import time
from decimal import Decimal
from datetime import datetime
from core.client import (
    fetch_position_raw, get_filters, adjust_qty, adjust_price,
    invalidate_account_cache, refresh_timestamp,
    _run_with_timeout, send_telegram, logger,
    _requests_lock, VALID_SYMBOLS, _filters_ok_to_trade,
)
from core.state import (
    PKT, add_active_trade,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    cooldown_until, COOLDOWN_LOCK,
    CSV_FILE,
)
from market.indicators import get_trading_config
from core.coins_config import get_caps
from .utils import (
    LEVERAGE, RISK_PERCENT, MAX_OPEN_POSITIONS, MAX_TOTAL_MARGIN_PCT,
    DRY_RUN, _cl,
)
from .exit import emergency_close_retry, handle_trade_close

# ── REV 1.7.0 — Centralized config (single source of truth) ──
from core.config_center import get_config as _get_central_config
# ── REV 1.7.1 — Per-strategy RR floor (fixes MIN_RR vs GLOBAL mismatch) ──
from core.config_center import get_min_rr as _get_min_rr_central

# ─── REV 1.5.3 — counter-trend size scaler (guarded import) ───
try:
    from signals.decision_engine import COUNTER_TREND_STRATEGIES as _CT_STRATS
except Exception:
    _CT_STRATS = frozenset()


# ── REV 1.7.3 — QTY scaling constants (module-level for clarity) ──
# NOTE: this is QTY scaling (position size), distinct from
# config_center.VOL_CLASS_CAP_MULT which scales SL/TP distance caps.
_VOL_CLASS_QTY_MULT = {
    "HIGH": Decimal("0.4"),
    "MED":  Decimal("0.7"),
    "LOW":  Decimal("1.0"),
}


def _apply_vol_class_sizing(symbol: str, qty_dec: Decimal, step: Decimal, min_qty: Decimal) -> Decimal:
    """REV 1.5.6 — Vol-class based position sizing.

    REV 1.7.3 — uses module-level `_VOL_CLASS_QTY_MULT` (was inline
    `mult_map`). Renamed for clarity vs config_center's
    `VOL_CLASS_CAP_MULT` which scales SL/TP distance caps, not qty.
    """
    try:
        from core.coins_config import get_coin_vol_class
        vcls = get_coin_vol_class(symbol)
    except Exception:
        vcls = "MED"

    mult = _VOL_CLASS_QTY_MULT.get(vcls, Decimal("0.7"))
    try:
        scaled = qty_dec * mult
        scaled = (scaled // step) * step
        if scaled < min_qty:
            scaled = min_qty
        if scaled < qty_dec:
            logger.info(f"[{symbol}] vol-class {vcls} size {mult}x → {scaled} (was {qty_dec})")
        return scaled
    except Exception as e:
        logger.debug(f"[{symbol}] vol sizing failed: {e}")
        return qty_dec


def _apply_counter_trend_sizing(symbol: str, strategy: str, qty_dec: Decimal, step: Decimal, min_qty: Decimal) -> Decimal:
    """REV 1.5.3 / 1.5.9 — scale down qty for counter-trend strategies."""
    if not _CT_STRATS:
        return qty_dec
    
    _strat = (strategy or "").strip()
    is_counter_trend = (
        _strat in _CT_STRATS
        or _strat == ""
        or _strat.upper() == "UNKNOWN"
    )
    if not is_counter_trend:
        return qty_dec
    
    try:
        scaled = qty_dec * Decimal("0.6")
        scaled = (scaled // step) * step
        if scaled < min_qty:
            scaled = min_qty
        if scaled < qty_dec:
            _label = _strat if _strat else "UNKNOWN"
            logger.info(f"[{symbol}] counter-trend size 0.6x → {scaled} (was {qty_dec}) [{_label}]")
        return scaled
    except Exception as e:
        logger.debug(f"[{symbol}] counter-trend scaling failed: {e}")
        return qty_dec


def _snapshot_regime(symbol: str, strategy: str) -> tuple[str, int]:
    """
    REV 1.7.0 — Capture regime + hold time at entry time.

    Returns (regime_string, hold_minutes). Safe defaults on any failure.
    Called from place_order_fixed before writing active_trades.
    """
    _regime_now = 'UNKNOWN'
    _hold_min = 180
    try:
        from market.indicators import get_cached_indicator
        _ind_1h = get_cached_indicator(symbol + 'USDT', '1h')
        if _ind_1h:
            _regime_now = _ind_1h.get('regime', 'UNKNOWN')
            _cfg_at_entry = _get_central_config(symbol, strategy, _regime_now)
            _hold_min = int(_cfg_at_entry.get('hold_minutes', 180))
            logger.info(
                f"[{symbol}] REGIME SNAPSHOT: {_regime_now} "
                f"→ hold={_hold_min}m, "
                f"sl_atr={_cfg_at_entry.get('sl_atr')}, "
                f"tp1_atr={_cfg_at_entry.get('tp1_atr')}, "
                f"tp_mult={_cfg_at_entry.get('tp_mult')}"
            )
    except Exception as _re:
        logger.debug(f"[{symbol}] regime snapshot failed: {_re}")
    return _regime_now, _hold_min


def _get_regime_now(symbol: str) -> str:
    """
    REV 1.7.2 — Fetch current regime from cached 1h indicator.
    Used by signal-drift check to resolve regime-dependent config.
    Safe default: "UNKNOWN".
    """
    try:
        from market.indicators import get_cached_indicator
        _ind = get_cached_indicator(symbol + 'USDT', '1h')
        if _ind:
            return _ind.get('regime', 'UNKNOWN') or 'UNKNOWN'
    except Exception:
        pass
    return 'UNKNOWN'


def place_order_fixed(symbol, side, quantity, sl_price, tp1_price, tp2_price,
                      entry_price_est, conf=50, rr=0, pattern="NONE", fg=50,
                      strategy="UNKNOWN", signal_price=None):
    """Place a market entry with attached SL/TP1/TP2."""
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        logger.error(f"[{symbol}] place_order_fixed: client not initialized")
        return False

    if not _filters_ok_to_trade(pair):
        logger.warning(f"🚫 [{symbol}] Filters unverified (fallback × 2+) — skipping entry")
        return False

    if DRY_RUN:
        logger.info(f"🧪 DRY RUN: would place {side} {pair} qty≈{quantity} "
                    f"SL={sl_price} TP1={tp1_price} TP2={tp2_price} "
                    f"strategy={strategy} signal_price={signal_price}")
        return True

    # ═══════════════════════════════════════════════════════════
    #  REV 1.7.0 — SIGNAL DRIFT CAP (now from config_center)
    #  REV 1.7.2 — Regime is now ACTUAL (was hardcoded "UNKNOWN").
    #  Reject entry if price drifted > signal_drift_pct from signal.
    #  Default 0.5% (was hardcoded 0.8%).
    #
    #  NOTE: This check runs BEFORE bot_tracked_symbols.add(),
    #        so no slot cleanup is needed on reject.
    # ═══════════════════════════════════════════════════════════
    _regime_drift = _get_regime_now(symbol)
    _cfg_drift = _get_central_config(symbol, strategy, _regime_drift)
    _MAX_SIGNAL_DRIFT_PCT = float(_cfg_drift.get("signal_drift_pct", 0.5)) * 100
    if signal_price is not None and signal_price > 0 and entry_price_est > 0:
        try:
            _sig_p = float(signal_price)
            _live_p = float(entry_price_est)
            _drift_pct = abs(_live_p - _sig_p) / _sig_p * 100.0
            if _drift_pct > _MAX_SIGNAL_DRIFT_PCT:
                logger.warning(
                    f"[{symbol}] SIGNAL DRIFT REJECT: "
                    f"signal={_sig_p:.6f} live={_live_p:.6f} "
                    f"drift={_drift_pct:.3f}% > {_MAX_SIGNAL_DRIFT_PCT:.2f}% "
                    f"[regime={_regime_drift}] "
                    f"— price moved too far since signal"
                )
                return False
        except (TypeError, ValueError) as _e:
            logger.debug(f"[{symbol}] drift check failed: {_e}")

    logger.info(f"🚀 Attempting to place {side} order for {pair} [strategy={strategy}]")

    with COOLDOWN_LOCK:
        if symbol in cooldown_until and datetime.now(PKT) < cooldown_until[symbol]:
            remaining = (cooldown_until[symbol] - datetime.now(PKT)).seconds // 60
            logger.info(f" {symbol} cooldown {remaining}m left")
            return False

    with BOT_TRACKED_LOCK:
        if len(bot_tracked_symbols) >= MAX_OPEN_POSITIONS:
            logger.warning(f" Max {MAX_OPEN_POSITIONS} reached, skip {symbol}")
            return False
        if symbol in bot_tracked_symbols:
            logger.warning(f" {symbol} already tracked")
            return False
        bot_tracked_symbols.add(symbol)
        logger.info(f" Slot reserved for {symbol} ({len(bot_tracked_symbols)}/{MAX_OPEN_POSITIONS})")

    try:
        if VALID_SYMBOLS and pair not in VALID_SYMBOLS:
            logger.error(f"Symbol {pair} not in VALID_SYMBOLS")
            with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
            return False
        
        try:
            pos_list = client.futures_position_information(symbol=pair)
            if pos_list and float(pos_list[0]['positionAmt']) != 0:
                logger.warning(f" {symbol} already in position")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False
        except Exception as e:
            logger.warning(f"Position pre-check failed for {pair}: {e}")

        try:
            account = client.futures_account()
            try:
                from core.client import _ACCOUNT_CACHE, _ACCOUNT_CACHE_LOCK
                with _ACCOUNT_CACHE_LOCK:
                    _ACCOUNT_CACHE['data'] = account
                    _ACCOUNT_CACHE['time'] = time.time()
            except Exception:
                pass
            
            available = float(account['availableBalance'])
            wallet_balance = float(account.get('totalWalletBalance', available))
            required_margin = (float(quantity) * entry_price_est) / LEVERAGE
            
            if required_margin > available * 0.80:
                logger.warning(f" Insufficient margin for {symbol}")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False
            
            try:
                used_margin_total = float(account.get('totalInitialMargin', wallet_balance - available))
            except (TypeError, ValueError):
                used_margin_total = max(0.0, wallet_balance - available)
            
            projected_total = used_margin_total + required_margin
            max_total_allowed = wallet_balance * MAX_TOTAL_MARGIN_PCT
            if wallet_balance > 0 and projected_total > max_total_allowed:
                logger.warning(f" Aggregate exposure limit hit for {symbol}")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False
        except Exception as e:
            logger.warning(f"Account fetch failed: {e}")
            with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
            return False

        try:
            client.futures_change_margin_type(symbol=pair, marginType='ISOLATED')
        except Exception as e:
            if '-4046' not in str(e) and 'No need to change margin type' not in str(e):
                logger.debug(f"Margin type warning: {e}")
        
        try:
            client.futures_change_leverage(symbol=pair, leverage=LEVERAGE)
        except Exception as e:
            logger.error(f"Leverage change FAILED for {pair}: {e}")
            with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
            return False

        f = get_filters(pair)
        step = f['stepSize']
        min_qty = f['minQty']
        min_notional = f['minNotional']
        max_qty = f.get('maxQty')
        cfg = get_trading_config()
        
        final_sl_price = float(sl_price)
        min_dist = entry_price_est * cfg['min_dist_pct']
        if side == 'BUY':
            if final_sl_price >= entry_price_est - min_dist:
                final_sl_price = entry_price_est - min_dist
        else:
            if final_sl_price <= entry_price_est + min_dist:
                final_sl_price = entry_price_est + min_dist

        # ── REV 1.5.5 — RESOLVE THE R-REFERENCE PRICE ──
        _ref_price = entry_price_est
        if signal_price is not None:
            try:
                _sp = float(signal_price)
                if _sp > 0:
                    _ref_price = _sp
            except (TypeError, ValueError):
                pass
        
        intended_dist = abs(_ref_price - final_sl_price)
        intended_dist_pct = (intended_dist / _ref_price * 100) if _ref_price > 0 else 0
        logger.info(f" [SIZE] Intended SL dist {intended_dist:.4f} ({intended_dist_pct:.3f}%) [ref={_ref_price:.6f} est={entry_price_est:.6f}]")

        try:
            risk_amount = float(wallet_balance) * (RISK_PERCENT / 100.0)
            actual_dist = abs(_ref_price - final_sl_price)
            if actual_dist > 0:
                calc_qty = risk_amount / actual_dist
                qty_dec = (Decimal(str(calc_qty)) // step) * step
                if qty_dec < min_qty:
                    qty_dec = min_qty
                logger.info(f" [C1] Qty finalized {qty_dec} from risk ${risk_amount:.2f} dist {actual_dist:.6f} (orig {quantity})")
            else:
                qty_dec = Decimal(str(quantity))
        except Exception as e:
            logger.warning(f"Qty final calc failed: {e}, using original")
            qty_dec = Decimal(str(quantity))

        qty_dec = _apply_counter_trend_sizing(symbol, strategy, qty_dec, step, min_qty)
        qty_dec = _apply_vol_class_sizing(symbol, qty_dec, step, min_qty)

        if max_qty is not None and qty_dec > max_qty:
            logger.warning(f"[{symbol}] qty {qty_dec} > exchange maxQty {max_qty} — capping")
            qty_dec = max_qty
            qty_dec = (qty_dec // step) * step
            if qty_dec < min_qty:
                logger.error(f"[{symbol}] capped qty {qty_dec} < minQty {min_qty} — aborting entry")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False

        qty_str = adjust_qty(qty_dec, step, min_qty)
        qty_dec = Decimal(qty_str)
        notional = float(qty_dec) * entry_price_est
        
        if notional < min_notional:
            _pre_bump_qty = qty_dec
            logger.warning(f"Notional {notional:.2f} < min {min_notional}, increasing qty")
            qty_dec = Decimal(str(min_notional / entry_price_est * 1.02))
            qty_dec = (qty_dec // step) * step
            if qty_dec < min_qty:
                qty_dec = min_qty
            if max_qty is not None and qty_dec > max_qty:
                logger.error(f"[{symbol}] min_notional bump pushed qty {qty_dec} > maxQty {max_qty} — aborting")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False
            if _pre_bump_qty > 0 and qty_dec > _pre_bump_qty * Decimal("1.5"):
                _bump_ratio = float(qty_dec / _pre_bump_qty)
                _bumped_risk_usd = float(qty_dec) * abs(_ref_price - final_sl_price)
                _bumped_risk_pct = ((_bumped_risk_usd / wallet_balance * 100.0) if wallet_balance > 0 else 0.0)
                logger.warning(f"[{symbol}] min_notional bump {_pre_bump_qty} → {qty_dec} ({_bump_ratio:.2f}×) would push risk to {_bumped_risk_pct:.3f}% — aborting entry")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False
            
            qty_str = format(qty_dec, f'.{abs(step.as_tuple().exponent)}f')
            notional = float(qty_dec) * entry_price_est
            if notional < min_notional:
                logger.error("Still below min notional, aborting")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False

        tp1_ratio = Decimal(str(cfg['tp1_qty']))
        prec = abs(step.as_tuple().exponent)
        qty_tp1_dec = (qty_dec * tp1_ratio)
        qty_tp1_dec = (qty_tp1_dec // step) * step
        if qty_tp1_dec < min_qty:
            qty_tp1_dec = min_qty
        qty_tp2_dec = qty_dec - qty_tp1_dec
        qty_tp2_dec = (qty_tp2_dec // step) * step
        if qty_tp2_dec < min_qty:
            qty_tp1_dec = qty_dec
            qty_tp2_dec = Decimal('0')
        
        qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
        qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'
        sl_price = final_sl_price

        market_resp = None
        for mkt_attempt in range(2):
            try:
                def _place_market():
                    return client.futures_create_order(symbol=pair, side=side, type='MARKET', quantity=qty_str)
                market_resp = _run_with_timeout(_place_market, 6, f"market:{pair}")
                break
            except Exception as e:
                err_str = str(e).lower()
                if 'rate' in err_str and mkt_attempt == 0:
                    time.sleep(2); refresh_timestamp(); continue
                logger.error(f"Market order error: {e}")
                with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                return False
        
        if market_resp is None or not market_resp.get('orderId'):
            logger.error(f"Market order failed for {pair}")
            with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
            return False
        
        logger.info(f"✅ Market {side} {pair} {qty_str} orderId={market_resp.get('orderId')}")
        time.sleep(3)

        pos = None
        for _ in range(3):
            try:
                pos_arr = _run_with_timeout(lambda: client.futures_position_information(symbol=pair), 5, f"pos:{pair}")
                pos = pos_arr[0]
                break
            except Exception:
                time.sleep(1)
        
        if pos is None:
            logger.error(f"❌ Position fetch FAILED for {pair} - keeping tracked")
            _regime_now, _hold_min = _snapshot_regime(symbol, strategy)
            add_active_trade(symbol, {
                'entry': entry_price_est, 'qty': qty_str, 'sl': '0', 'tp1': '0', 'tp2': '0', 'side': side,
                'entry_time': datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p'), 'sl_id': 0, 'sl_level': 0,
                'unverified': True, 'initial_sl': float(sl_price), 'strategy': strategy,
                'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
                'regime_at_entry': _regime_now,
                'hold_time_minutes': _hold_min,
            })
            return False

        amt = float(pos.get('positionAmt', 0) or 0)
        entry_price = None
        if amt == 0:
            logger.critical(f"[{symbol}] Position=0 after market order. Verifying...")
            try:
                with _requests_lock:
                    o = client.futures_get_order(symbol=pair, orderId=market_resp['orderId'])
                status = (o or {}).get('status', 'UNKNOWN')
                if status == 'FILLED':
                    entry_price = float(o.get('avgPrice') or entry_price_est or 0) or entry_price_est
                    amt = abs(float(qty_str))
                else:
                    logger.error(f"[{symbol}] Order not FILLED (status={status})")
                    with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                    return False
            except Exception as e:
                logger.critical(f"[{symbol}] Order verify failed: {e}. KEEPING tracked.")
                _regime_now, _hold_min = _snapshot_regime(symbol, strategy)
                add_active_trade(symbol, {
                    'entry': entry_price_est, 'qty': qty_str, 'sl': '0', 'tp1': '0', 'tp2': '0', 'side': side,
                    'entry_time': datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p'), 'sl_id': 0, 'sl_level': 0,
                    'unverified': True, 'initial_sl': float(sl_price), 'strategy': strategy,
                    'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
                    'regime_at_entry': _regime_now,
                    'hold_time_minutes': _hold_min,
                })
                return False
        else:
            entry_price = float(pos['entryPrice'])

        # ── REV 1.5.4 / 1.5.5 — P0 RE-ANCHOR SL/TP TO ACTUAL FILL ──
        _orig_risk    = abs(_ref_price - final_sl_price)
        _orig_reward1 = abs(float(tp1_price) - _ref_price)
        _orig_reward2 = abs(float(tp2_price) - _ref_price)
        if _orig_risk > 0 and entry_price > 0:
            if side == 'BUY':
                sl_price  = entry_price - _orig_risk
                tp1_price = entry_price + _orig_reward1
                tp2_price = entry_price + _orig_reward2
            else:
                sl_price  = entry_price + _orig_risk
                tp1_price = entry_price - _orig_reward1
                tp2_price = entry_price - _orig_reward2
            logger.info(f"[{symbol}] P0 RE-ANCHOR: est {entry_price_est:.6f} → fill {entry_price:.6f} | ref {_ref_price:.6f} | risk {_orig_risk:.6f} preserved | SL {float(sl_price):.6f} TP1 {float(tp1_price):.6f} TP2 {float(tp2_price):.6f}")
        else:
            logger.warning(f"[{symbol}] P0 RE-ANCHOR SKIPPED (orig_risk={_orig_risk} entry={entry_price}) — falling back to signal anchors")

        _shared_entry_time_str = datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')

        # ── REV 1.7.0 — Regime snapshot (frozen for trade lifetime) ──
        _regime_now, _hold_min = _snapshot_regime(symbol, strategy)

        try:
            add_active_trade(symbol, {
                'entry': entry_price, 'qty': qty_str, 'sl': '0', 'tp1': '0', 'tp2': '0', 'side': side,
                'entry_time': _shared_entry_time_str, 'sl_id': 0, 'sl_level': 0, 'unverified': True,
                'initial_sl': float(sl_price), 'strategy': strategy, 'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
                'regime_at_entry': _regime_now,
                'hold_time_minutes': _hold_min,
            })
        except Exception as e:
            logger.debug(f"Early active_trade record failed: {e}")

        close_side = 'SELL' if side == 'BUY' else 'BUY'
        min_dist_post = entry_price * cfg['min_dist_pct']
        sl_price_f = float(sl_price)
        directional_dist = (entry_price - sl_price_f) if side == 'BUY' else (sl_price_f - entry_price)
        actual_dist_post = abs(entry_price - sl_price_f)
        slip_pct = ((actual_dist_post - intended_dist) / intended_dist * 100) if intended_dist > 0 else 0
        logger.info(f" [FILL] entry {entry_price:.4f} (est {entry_price_est:.4f}) SL dist {actual_dist_post:.4f} vs intended {intended_dist:.4f} ({slip_pct:+.1f}%)")

        try:
            _sgn = 1.0 if side == 'BUY' else -1.0
            _drift_sig = ((entry_price - _ref_price) / _ref_price * 100.0 * _sgn if _ref_price > 0 else 0.0)
            _drift_est = ((entry_price - entry_price_est) / entry_price_est * 100.0 * _sgn if entry_price_est > 0 else 0.0)
            logger.info(f"TELEMETRY: {symbol} {side} strat={strategy} signal={_ref_price:.6f} est={entry_price_est:.6f} fill={entry_price:.6f} adverse_vs_signal={_drift_sig:+.3f}% adverse_vs_est={_drift_est:+.3f}%")
        except Exception as _te:
            logger.debug(f"[{symbol}] telemetry failed: {_te}")

        if directional_dist <= 0:
            logger.critical(f"[{symbol}] Fill crossed SL! entry {entry_price:.6f} SL {sl_price_f:.6f} dir_dist {directional_dist:.6f} - aborting")
            try: send_telegram(f"🚨 {symbol} CROSSED SL - closing\nEntry {entry_price:.4f} SL {sl_price_f:.4f}")
            except Exception: pass
            
            closed_ok = False
            thread_launched = False
            try:
                def _sync_close():
                    try:
                        with _requests_lock:
                            client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=qty_str, reduceOnly=True)
                    except Exception as ce:
                        if '-2021' in str(ce) or 'would immediately trigger' in str(ce).lower(): return True
                        raise
                    time.sleep(0.6)
                    p = fetch_position_raw(symbol)
                    if p is None: return False
                    return abs(float(p.get('positionAmt', '0') or 0)) == 0
                for _ in range(3):
                    if _sync_close():
                        closed_ok = True
                        break
                    time.sleep(0.8)
                if not closed_ok:
                    logger.warning(f"[{symbol}] crossed SL sync close failed, launching emergency_close_retry")
                    threading.Thread(target=emergency_close_retry, args=(symbol, pair, close_side), daemon=True).start()
                    thread_launched = True
            except Exception as ce:
                logger.error(f"Crossed SL emergency close failed: {ce}")
                threading.Thread(target=emergency_close_retry, args=(symbol, pair, close_side), daemon=True).start()
                thread_launched = True
            
            if thread_launched: return False
            try:
                time.sleep(2.5)
                handle_trade_close(symbol, pair, reason="CROSSED_SL")
            except Exception as htce:
                logger.warning(f"handle_trade_close CROSSED_SL failed: {htce}")
            return False

        if actual_dist_post < min_dist_post * 0.7:
            logger.warning(f"[{symbol}] SL too tight after fill (dist {actual_dist_post:.6f} < {min_dist_post:.6f}), widening")
            if side == 'BUY': sl_price = entry_price - min_dist_post
            else: sl_price = entry_price + min_dist_post
            sl_price_f = float(sl_price)
            actual_dist_post = min_dist_post
            directional_dist = min_dist_post
            try:
                risk_amount = float(wallet_balance) * (RISK_PERCENT / 100.0)
                corrected_qty_tight = Decimal(str(risk_amount / actual_dist_post)) if actual_dist_post > 0 else qty_dec
                corrected_qty_tight = (corrected_qty_tight // f['stepSize']) * f['stepSize']
                if corrected_qty_tight < f['minQty']: corrected_qty_tight = f['minQty']
                if corrected_qty_tight < qty_dec:
                    reduce_qty_t = qty_dec - corrected_qty_tight
                    reduce_str_t = adjust_qty(reduce_qty_t, f['stepSize'], f['minQty'])
                    if Decimal(reduce_str_t) >= f['minQty']:
                        try:
                            with _requests_lock:
                                client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=reduce_str_t, reduceOnly=True)
                            logger.info(f"[{symbol}] TIGHT FIX: Reducing {reduce_str_t} qty, target {corrected_qty_tight} (was {qty_dec})")
                            time.sleep(0.8)
                            p_check = fetch_position_raw(symbol)
                            settled = False; live_amt_t = 0.0
                            if p_check is not None:
                                live_amt_t = abs(float(p_check.get('positionAmt', '0') or 0))
                                if live_amt_t <= float(corrected_qty_tight) * 1.02: settled = True
                                else: logger.warning(f"[{symbol}] tight reduce not settled, live {live_amt_t} > target {corrected_qty_tight}")
                            if settled:
                                qty_dec = Decimal(str(live_amt_t)) if live_amt_t > 0 else corrected_qty_tight
                                qty_str = format(qty_dec, f'.{prec}f')
                                qty_tp1_dec = (qty_dec * Decimal(str(cfg['tp1_qty'])))
                                qty_tp1_dec = (qty_tp1_dec // f['stepSize']) * f['stepSize']
                                if qty_tp1_dec < f['minQty']: qty_tp1_dec = f['minQty']
                                qty_tp2_dec = qty_dec - qty_tp1_dec
                                qty_tp2_dec = (qty_tp2_dec // f['stepSize']) * f['stepSize']
                                if qty_tp2_dec < f['minQty']: qty_tp1_dec = qty_dec; qty_tp2_dec = Decimal('0')
                                qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
                                qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'
                        except Exception as re_t:
                            logger.error(f"Tight branch reduce failed: {re_t}")
            except Exception as fix_t:
                logger.error(f"Tight branch fix failed: {fix_t}", exc_info=True)

        elif intended_dist > 0 and actual_dist_post > intended_dist * 1.05:
            risk_multiplier = actual_dist_post / intended_dist
            logger.warning(f"[{symbol}] SL widened vs intended (actual {actual_dist_post:.6f} vs intended {intended_dist:.6f}) - real risk {risk_multiplier:.2f}x")
            try: send_telegram(f"⚠️ {symbol} slippage: SL wider than intended, risk {risk_multiplier:.2f}x\nEntry {entry_price:.4f} SL {sl_price_f:.4f}")
            except Exception: pass
            try:
                risk_amount = float(wallet_balance) * (RISK_PERCENT / 100.0)
                _caps = get_caps(symbol)
                max_allowed_dist = entry_price * _caps["sl"]
                if actual_dist_post > max_allowed_dist:
                    logger.warning(f"[{symbol}] Capping SL dist from {actual_dist_post:.6f} to {max_allowed_dist:.6f} (per-coin cap {_caps['sl']*100:.2f}%)")
                    if side == 'BUY': sl_price = entry_price - max_allowed_dist
                    else: sl_price = entry_price + max_allowed_dist
                    sl_price_f = float(sl_price)
                    actual_dist_post = max_allowed_dist
                    directional_dist = max_allowed_dist
                
                corrected_qty_dec = Decimal(str(risk_amount / actual_dist_post)) if actual_dist_post > 0 else qty_dec
                corrected_qty_dec = (corrected_qty_dec // f['stepSize']) * f['stepSize']
                if corrected_qty_dec < f['minQty']: corrected_qty_dec = f['minQty']
                if corrected_qty_dec < (qty_dec * Decimal('0.5')):
                    logger.critical(f"[{symbol}] Corrected qty {corrected_qty_dec} < 50% of {qty_dec} - closing position to prevent huge loss")
                    try:
                        with _requests_lock:
                            client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=qty_str, reduceOnly=True)
                    except Exception as ce: logger.error(f"Emergency close failed: {ce}")
                    with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                    return False
                
                if corrected_qty_dec < qty_dec:
                    reduce_qty = qty_dec - corrected_qty_dec
                    reduce_str = adjust_qty(reduce_qty, f['stepSize'], f['minQty'])
                    if Decimal(reduce_str) >= f['minQty']:
                        try:
                            with _requests_lock:
                                client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=reduce_str, reduceOnly=True)
                            logger.info(f"[{symbol}] FIXED RISK: Reduced {reduce_str} qty, new qty {corrected_qty_dec} (was {qty_dec})")
                            time.sleep(0.8)
                            p_check = fetch_position_raw(symbol)
                            settled = False; live_amt = 0.0
                            if p_check is not None:
                                live_amt = abs(float(p_check.get('positionAmt', '0') or 0))
                                if live_amt <= float(corrected_qty_dec) * 1.02: settled = True
                                else: logger.warning(f"[{symbol}] wide reduce not settled - live {live_amt} > target {corrected_qty_dec}")
                            if settled:
                                qty_dec = Decimal(str(live_amt)) if live_amt > 0 else corrected_qty_dec
                                qty_str = format(qty_dec, f'.{prec}f')
                                qty_tp1_dec = (qty_dec * Decimal(str(cfg['tp1_qty'])))
                                qty_tp1_dec = (qty_tp1_dec // f['stepSize']) * f['stepSize']
                                if qty_tp1_dec < f['minQty']: qty_tp1_dec = f['minQty']
                                qty_tp2_dec = qty_dec - qty_tp1_dec
                                qty_tp2_dec = (qty_tp2_dec // f['stepSize']) * f['stepSize']
                                if qty_tp2_dec < f['minQty']: qty_tp1_dec = qty_dec; qty_tp2_dec = Decimal('0')
                                qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
                                qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'
                        except Exception as re:
                            logger.error(f"Reduce qty failed: {re}")
                if float(qty_dec) * entry_price < f['minNotional']:
                    logger.critical(f"[{symbol}] post-fix notional {float(qty_dec)*entry_price:.2f} < minNotional {f['minNotional']} - aborting")
                    try:
                        with _requests_lock:
                            client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=qty_str, reduceOnly=True)
                    except Exception: pass
                    with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                    return False
            except Exception as fix_e:
                logger.error(f"SL fix logic failed: {fix_e}", exc_info=True)

        if side == 'BUY':
            if tp1_price <= entry_price + min_dist_post: tp1_price = entry_price + min_dist_post
            if tp2_price <= entry_price + min_dist_post: tp2_price = entry_price + min_dist_post
        else:
            if tp1_price >= entry_price - min_dist_post: tp1_price = entry_price - min_dist_post
            if tp2_price >= entry_price - min_dist_post: tp2_price = entry_price - min_dist_post

        _tp1_before_cap = float(tp1_price)
        _tp2_before_cap = float(tp2_price)
        _caps = get_caps(symbol)
        if side == 'BUY':
            tp1_price = min(tp1_price, entry_price * (1 + _caps["tp1"]))
            tp2_price = min(tp2_price, entry_price * (1 + _caps["tp2"]))
        else:
            tp1_price = max(tp1_price, entry_price * (1 - _caps["tp1"]))
            tp2_price = max(tp2_price, entry_price * (1 - _caps["tp2"]))

        # ── REV 1.5.10 — RR RE-CHECK AFTER CAPS (with float tolerance) ──
        # ── REV 1.7.1 — Now uses per-strategy RR floor from config_center ──
        _risk_final = abs(entry_price - float(sl_price))
        _reward_final = abs(float(tp1_price) - entry_price)
        _rr_final = (_reward_final / _risk_final) if _risk_final > 0 else 0.0
        _min_rr = _get_min_rr_central(strategy)
        _RR_COLLAPSE_TOL = 0.02
        
        logger.info(f"[{symbol}] RR check: signal_rr={rr:.2f} → placed_rr={_rr_final:.3f} (min {_min_rr:.2f}, tol {_RR_COLLAPSE_TOL}) [strategy={strategy}] | caps sl={_caps['sl']:.4f} tp1={_caps['tp1']:.4f} tp2={_caps['tp2']:.4f} | tp1 {_tp1_before_cap:.6f}→{float(tp1_price):.6f} tp2 {_tp2_before_cap:.6f}→{float(tp2_price):.6f}")
        
        if _rr_final < _min_rr - _RR_COLLAPSE_TOL:
            logger.critical(f"[{symbol}] RR COLLAPSE: {rr:.2f} → {_rr_final:.3f} (min {_min_rr:.2f}, tol {_RR_COLLAPSE_TOL}) — closing position immediately")
            try: send_telegram(f"🚨 {symbol} RR collapsed {rr:.2f}→{_rr_final:.2f} (min {_min_rr:.2f}) — closing")
            except Exception: pass
            closed_ok = False; thread_launched = False
            try:
                with _requests_lock:
                    client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=qty_str, reduceOnly=True)
                time.sleep(1.5)
                p = fetch_position_raw(symbol)
                if p is not None and abs(float(p.get('positionAmt', '0') or 0)) == 0: closed_ok = True
            except Exception as ce:
                logger.error(f"[{symbol}] RR-collapse close failed: {ce}")
            if not closed_ok:
                threading.Thread(target=emergency_close_retry, args=(symbol, pair, close_side), daemon=True).start()
                thread_launched = True
            if not thread_launched:
                try: handle_trade_close(symbol, pair, reason="RR_COLLAPSE")
                except Exception: pass
            with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
            return False
        
        if _rr_final < _min_rr:
            logger.warning(f"[{symbol}] RR boundary: {rr:.3f} → {_rr_final:.4f} (min {_min_rr:.3f}, within tol {_RR_COLLAPSE_TOL}) — keeping position (float drift, not a real collapse)")

        try:
            _final_risk_usd = float(qty_dec) * abs(entry_price - float(sl_price))
            _final_risk_pct = ((_final_risk_usd / wallet_balance * 100.0) if wallet_balance > 0 else 0.0)
            _target_risk_pct = RISK_PERCENT
            logger.info(f"[{symbol}] RISK CHECK: actual {_final_risk_pct:.3f}% (target {_target_risk_pct:.3f}%) | qty={qty_dec} dist={abs(entry_price - float(sl_price)):.6f} strategy={strategy}")
            if _final_risk_pct > _target_risk_pct * 1.2:
                logger.warning(f"[{symbol}] RISK OVER-SIZE: {_final_risk_pct:.3f}% > {_target_risk_pct * 1.2:.3f}% (compound-scaling issue? qty={qty_dec})")
        except Exception as _rce:
            logger.debug(f"[{symbol}] risk-check log failed: {_rce}")

        sl_adj = adjust_price(sl_price, f['tickSize'])
        tp1_adj = adjust_price(tp1_price, f['tickSize'])
        tp2_adj = adjust_price(tp2_price, f['tickSize'])
        sl_placed = False; sl_id = None
        for attempt in range(3):
            try:
                def _place_sl():
                    return client.futures_create_order(symbol=pair, side=close_side, type='STOP_MARKET', stopPrice=sl_adj, quantity=qty_str, reduceOnly=True, timeInForce='GTC', workingType='MARK_PRICE')
                sl_resp = _run_with_timeout(_place_sl, 5, f"sl:{pair}")
                sl_id = sl_resp.get('orderId') or sl_resp.get('algoId')
                logger.info(f" SL placed at {sl_adj} (ID: {sl_id})")
                sl_placed = True; break
            except Exception as e:
                err_str = str(e).lower()
                if '-2021' in str(e) or 'would immediately trigger' in err_str:
                    logger.critical(f"[{symbol}] SL -2021 would immediately trigger (stop {sl_adj}) - closing position now")
                    try:
                        with _requests_lock:
                            client.futures_create_order(symbol=pair, side=close_side, type='MARKET', quantity=qty_str, reduceOnly=True)
                    except Exception as ce: logger.error(f"-2021 emergency close failed: {ce}")
                    threading.Thread(target=emergency_close_retry, args=(symbol, pair, close_side), daemon=True).start()
                    time.sleep(0.5)
                    try: handle_trade_close(symbol, pair, reason="SL_-2021")
                    except Exception as htce: logger.warning(f"handle_trade_close SL_-2021 failed: {htce}")
                    with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
                    return False
                logger.warning(f"SL attempt {attempt+1} failed: {e}")
                time.sleep(1.5)
        
        if not sl_placed:
            logger.error("SL placement FAILED - emergency close")
            threading.Thread(target=emergency_close_retry, args=(symbol, pair, close_side), daemon=True).start()
            with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
            send_telegram(f" SL FAILED {symbol}")
            return False

        tp1_id = None; tp1_placed = False
        for tp1_attempt in range(2):
            try:
                def _place_tp1():
                    return client.futures_create_order(symbol=pair, side=close_side, type='TAKE_PROFIT_MARKET', stopPrice=tp1_adj, quantity=qty_tp1, reduceOnly=True, timeInForce='GTC', workingType='MARK_PRICE')
                tp1_resp = _run_with_timeout(_place_tp1, 5, f"tp1:{pair}")
                tp1_id = tp1_resp.get('orderId') or tp1_resp.get('algoId')
                logger.info(f" TP1 at {tp1_adj} Qty {qty_tp1} (ID: {tp1_id})")
                tp1_placed = True; break
            except Exception as e:
                logger.warning(f"TP1 attempt {tp1_attempt+1} failed: {e}")
                time.sleep(1)
        if not tp1_placed:
            logger.error(f"TP1 FAILED for {symbol}")
            send_telegram(f"⚠️ TP1 FAILED for {symbol}")

        tp2_id = None
        if Decimal(qty_tp2) > 0:
            tp2_placed = False
            for tp2_attempt in range(2):
                try:
                    def _place_tp2():
                        return client.futures_create_order(symbol=pair, side=close_side, type='TAKE_PROFIT_MARKET', stopPrice=tp2_adj, quantity=qty_tp2, reduceOnly=True, timeInForce='GTC', workingType='MARK_PRICE')
                    tp2_resp = _run_with_timeout(_place_tp2, 5, f"tp2:{pair}")
                    tp2_id = tp2_resp.get('orderId') or tp2_resp.get('algoId')
                    logger.info(f" TP2 at {tp2_adj} Qty {qty_tp2} (ID: {tp2_id})")
                    tp2_placed = True; break
                except Exception as e:
                    logger.warning(f"TP2 attempt {tp2_attempt+1} failed: {e}")
                    time.sleep(1)
            if not tp2_placed:
                logger.error(f"TP2 FAILED for {symbol}")

        add_active_trade(symbol, {
            'entry': entry_price, 'qty': qty_str, 'sl': sl_adj, 'tp1': tp1_adj, 'tp2': tp2_adj, 'side': side,
            'entry_time': _shared_entry_time_str, 'sl_id': sl_id, 'sl_level': 0, 'initial_sl': float(sl_adj),
            'tp1_id': tp1_id, 'tp2_id': tp2_id, 'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2, 'strategy': strategy,
            'regime_at_entry': _regime_now,
            'hold_time_minutes': _hold_min,
        })

        try:
            with open(CSV_FILE, 'a', encoding='utf-8') as logf:
                logf.write(f"{datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')},"
                           f"{symbol},{side},{entry_price},{entry_price_est},"
                           f"{sl_adj},{tp1_adj},{tp2_adj},{qty_str},{conf:.1f},{rr:.2f},"
                           f"{pattern},{fg},{strategy}\n")
        except Exception as e:
            logger.warning(f"CSV write failed: {e}")

        logger.info(f" {side} {pair} {qty_str} | SL {sl_adj} TP1 {tp1_adj} TP2 {tp2_adj} | strategy={strategy}")
        send_telegram(f" NEW TRADE [{strategy}]\n{symbol} {side}\nEntry: ${entry_price:.2f}\nSL: {sl_adj}\nTP1: {tp1_adj}\nTP2: {tp2_adj}")
        return True

    except Exception as e:
        logger.error(f"Unexpected error in place_order for {symbol}: {e}", exc_info=True)
        with BOT_TRACKED_LOCK: bot_tracked_symbols.discard(symbol)
        return False