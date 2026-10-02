"""
future.py — Trading engine orchestrator + main scan loop.

REV 1.4.24 (2026-09-30) — DYNAMIC STARTUP BANNER VALUES:
  ✅ "Running in TUNED mode" line now reads MIN_APPROVALS from
     decision_engine dynamically instead of hardcoded "3/6". Also
     updates st_rsi_sell_floor label to "per-family" since REV 21.2
     made it family-specific (25/28/30) — no single global value.
  ✅ "Decision engine: active" line now shows the actual
     MIN_APPROVALS value from decision_engine.
  ✅ _spread_label() now includes the global cap (max_spread_pct)
     for parity with the config.py banner. Previously only per-family
     values were shown, hiding the global fallback from logs.
  All three changes are COSMETIC — no behaviour change. They just
  prevent misleading log lines when MIN_APPROVALS / spread caps are
  tuned via .env or code.

REV 1.4.23 (2026-09-30) — FIX Bug #1: PRE-CHECKS BEFORE DECISION ENGINE:
  ✅ Cooldown / Rotation / IN POSITION / Same Candle pre-checks now
     run BEFORE evaluate_trade(). Previously the decision engine was
     called first (logging "APPROVED"), then these checks blocked the
     trade afterwards — producing misleading log spam and wasting the
     6-filter vote.
     Live evidence (2026-09-30 12:21+): VIRTUAL SHORT opened at 12:21:01,
     but every subsequent scan still logged:
       "[decision] ✅ VIRTUAL SELL [SUPERTREND_RIDE] 5/6 conf=80% ... APPROVED"
     8+ times over 4 minutes, while the actual trade was never placed
     because the position was already open.
     After fix: pre-checks short-circuit BEFORE evaluate_trade(), so
     the decision engine is never called for already-open symbols.
     Added a post-decision safety re-check as belt-and-suspenders for
     the case where the decision engine somehow allows a trade on an
     already-tracked symbol (e.g. race with active_trades_list update).

REV 1.4.22 (2026-09-30) — HOIST STRATEGY EXTRACT.
REV 1.4.21 (2026-09-30) — CONDITIONAL 5M FILTER + RENAME.
REV 1.4.20 (2026-09-29) — SAME-SIDE RACE FIX.
REV 1.4.19 (2026-09-29) — FULL SCAN (PHASE 1 REMOVED).
REV 1.4.18 (2026-09-29) — DIAGNOSTIC + SPREAD CAP WIRING.
REV 1.4.17 (2026-09-29) — PER-FAMILY SPREAD + STARTUP RECONCILE.
REV 1.4.16 (2026-09-29) — SPREAD FILTER WIRED IN.
REV 1.4.15 (2026-09-29) — FAST-SKIP + PARALLEL FETCH + 5M PRE-GATE.
REV 1.4.14 (2026-09-29) — SIGNAL_PRICE PROPAGATION.
REV 1.4.13 (2026-09-29) — MTF GATE REMOVED (DUPLICATE FILTER).
REV 1.4.12 (2026-09-29) — BTC REGIME WIRING FOR REV 1.0.5.
REV 1.4.11 (2026-09-28) — PRE-GATE DIAGNOSTICS + MINADX BANNER.
REV 1.4.10 (2026-09-28) — UNUSED RE-EXPORTS REMOVED.
REV 1.4.9 (2026-09-28) — DEAD RE-EXPORTS REMOVED.
REV 1.4.8 (2026-09-28) — DEAD MIN_ADX FLOOR FIX + GETATTR CLEANUP.
REV 1.4.7 (2026-09-28) — LAZY STRATEGY EXTRACTION.
REV 1.4.6 (2026-09-28) — DEAD __getattr__ PROXY REMOVED.
REV 1.4.5 (2026-09-28) — HOISTED STRATEGY EXTRACTION.
REV 1.4.4 (2026-09-28) — DECISION ENGINE GATE.
REV 1.4.3 (2026-09-26) — MTF COUNTER FIX + DEAD IMPORTS.
REV 1.4.2 (2026-09-26) — REMOVED DEAD CONFIG REFERENCE.
REV 1.4.1 (2026-09-25) — IDIOMATIC FAMILY LOOKUP.
REV 1.4.0 (2026-09-24) — PHASE 3 MTF CONFLUENCE GATE.
REV 1.3.9 (2026-09-24) — N+1 API FIX IN SCAN LOOP.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from datetime import datetime

from core.config import CONFIG
from core.coins_config import get_family

import core.client as _c
import core.state as _s
import market.indicators as _i
import orders as _o
import signals.router as _fr

from core.client import logger

# ─── REV 1.4.21 — counter-trend classification for 5m filter ───
try:
    from signals.decision_engine import COUNTER_TREND_STRATEGIES as _CT_STRATEGIES
except Exception:
    _CT_STRATEGIES = frozenset()


# ─────────────────────────────────────────────────────────────
# RE-EXPORTS
# ─────────────────────────────────────────────────────────────
set_client_keys           = _c.set_client_keys
_validate_api_credentials = _c._validate_api_credentials
get_client                = _c.get_client
get_account_cached        = _c.get_account_cached
invalidate_account_cache  = _c.invalidate_account_cache
get_bot_coins             = _c.get_bot_coins
validate_symbols          = _c.validate_symbols
get_binance_klines        = _c.get_binance_klines
get_balance_or_last_good  = _c.get_balance_or_last_good
get_live_prices           = _c.get_live_prices
send_telegram             = _c.send_telegram
get_filters               = _c.get_filters
adjust_qty                = _c.adjust_qty
adjust_price              = _c.adjust_price
fetch_position_raw        = _c.fetch_position_raw
refresh_timestamp         = _c.refresh_timestamp
retry_on_rate_limit       = _c.retry_on_rate_limit
_run_with_timeout         = _c._run_with_timeout
_get_open_algo_orders     = _c._get_open_algo_orders
_cancel_algo_order        = _c._cancel_algo_order
_position_amt             = _c._position_amt
HARDCODED_FILTERS         = _c.HARDCODED_FILTERS

clear_stop                = _s.clear_stop
request_stop              = _s.request_stop
is_stopped                = _s.is_stopped
load_active_trades        = _s.load_active_trades
add_active_trade          = _s.add_active_trade
remove_active_trade       = _s.remove_active_trade
get_active_trade          = _s.get_active_trade
flush_active_trades       = _s.flush_active_trades
load_cooldowns            = _s.load_cooldowns
save_cooldowns            = _s.save_cooldowns
get_scan_results          = _s.get_scan_results
set_scan_results          = _s.set_scan_results
cleanup_entry_candles     = _s.cleanup_entry_candles
can_trade_coin            = _s.can_trade_coin
record_coin_trade         = _s.record_coin_trade
get_v2_stats_str          = _s.get_v2_stats_str
increment_v2_stat         = _s.increment_v2_stat
daily_tracker             = _s.daily_tracker

active_trades             = _s.active_trades
ACTIVE_TRADES_LOCK        = _s.ACTIVE_TRADES_LOCK
bot_tracked_symbols       = _s.bot_tracked_symbols
BOT_TRACKED_LOCK          = _s.BOT_TRACKED_LOCK
cooldown_until            = _s.cooldown_until
COOLDOWN_LOCK             = _s.COOLDOWN_LOCK
last_entry_candle         = _s.last_entry_candle
ENTRY_CANDLE_LOCK         = _s.ENTRY_CANDLE_LOCK
scan_results              = _s.scan_results
SCAN_RESULTS_LOCK         = _s.SCAN_RESULTS_LOCK
ATR_CACHE                 = _s.ATR_CACHE
ATR_CACHE_LOCK            = _s.ATR_CACHE_LOCK
FAIL_COUNTS_LOCK          = _s.FAIL_COUNTS_LOCK
fail_counts               = _s.fail_counts

place_order_fixed         = _o.place_order_fixed
manage_single_trade       = _o.manage_single_trade
handle_trade_close        = _o.handle_trade_close
emergency_close_retry     = _o.emergency_close_retry
cancel_specific_sl        = _o.cancel_specific_sl
cancel_all_sl_stops       = _o.cancel_all_sl_stops
reconcile_active_trade    = _o.reconcile_active_trade
trade_manager_loop        = _o.trade_manager_loop
robust_cancel_all         = _o.robust_cancel_all
cancel_orphan_bot_orders  = _o.cancel_orphan_bot_orders
sync_existing_positions   = _o.sync_existing_positions
clean_orders              = _o.clean_orders
get_trade_status          = _o.get_trade_status

get_trading_config        = _i.get_trading_config
get_fear_greed_index      = _i.get_fear_greed_index
get_cached_indicator      = _i.get_cached_indicator
set_cached_indicator      = _i.set_cached_indicator
cleanup_indicator_cache   = _i.cleanup_indicator_cache
detect_candle_patterns    = _i.detect_candle_patterns

RISK_PERCENT               = CONFIG.risk_percent
MAX_OPEN_POSITIONS         = CONFIG.max_open_positions
MAX_DAILY_DRAWDOWN_PERCENT = CONFIG.max_daily_drawdown_percent
MIN_CONFIDENCE             = CONFIG.min_confidence
MIN_ADX                    = CONFIG.min_adx
REQUIRE_HTF_AGREEMENT      = CONFIG.require_htf_agreement
USE_5M_TREND_FILTER        = CONFIG.use_5m_trend_filter
FG_ENABLED                 = CONFIG.fg_enabled
LEVERAGE                   = CONFIG.leverage
DRY_RUN                    = CONFIG.dry_run
DEMO_MODE                  = CONFIG.demo_mode
TESTNET                    = CONFIG.testnet
TRADING_MODE               = "TREND_PULLBACK"

PKT = _s.PKT
BLACKLISTED_COINS = _s.BLACKLISTED_COINS
CSV_FILE          = _s.CSV_FILE
CSV_EXIT_FILE     = _s.CSV_EXIT_FILE
PAUSE_FILE        = _s.PAUSE_FILE

_EXPECTED_R_KEYS = ("be_r_min", "lock1_r_min", "lock2_r_min")

_PARALLEL_FETCH_WORKERS = 8


@lru_cache(maxsize=128)
def _get_min_adx_for_coin(symbol: str) -> float:
    """Effective min-ADX gate for a symbol (family-loosest floor)."""
    if not symbol:
        return MIN_ADX
    sym_upper = symbol.upper()
    try:
        pair = sym_upper if sym_upper.endswith("USDT") else sym_upper + "USDT"
        family = get_family(pair)
        mod = _fr.FAMILIES.get(family)
        if mod and hasattr(mod, "MIN_ADX"):
            floors = list(mod.MIN_ADX.values())
            if floors:
                return float(min(floors))
    except Exception as e:
        logger.debug(f"_get_min_adx_for_coin({symbol}) failed: {e}")
    return MIN_ADX


def _get_spread_cap_for_family(family: str) -> float:
    """Per-family spread cap."""
    try:
        if family == "trend_coins":
            return float(getattr(CONFIG, "max_spread_trend", CONFIG.max_spread_pct))
        if family == "range_coins":
            return float(getattr(CONFIG, "max_spread_range", CONFIG.max_spread_pct))
        if family in ("volatility_coins", "momentum_coins"):
            return float(getattr(CONFIG, "max_spread_volatility", CONFIG.max_spread_pct))
    except Exception:
        pass
    return float(CONFIG.max_spread_pct)


def _reconcile_on_startup():
    """Startup position reconcile (uses removesuffix)."""
    try:
        client = _c.get_client()
        if client is None:
            return
        pos_all = client.futures_position_information()
        real_positions = {
            (p.get('symbol') or '').removesuffix('USDT'): p
            for p in (pos_all or [])
            if abs(float(p.get('positionAmt', 0) or 0)) > 0
        }
        with _s.BOT_TRACKED_LOCK:
            tracked = set(_s.bot_tracked_symbols)
            for sym in tracked:
                if sym not in real_positions:
                    if not _s.get_active_trade(sym):
                        _s.bot_tracked_symbols.discard(sym)
                        logger.info(f"[RECONCILE-STARTUP] {sym} tracked but no real pos -> removed")
        with _s.BOT_TRACKED_LOCK:
            for sym in real_positions:
                if sym not in _s.bot_tracked_symbols:
                    if _s.get_active_trade(sym):
                        _s.bot_tracked_symbols.add(sym)
                        logger.info(f"[RECONCILE-STARTUP] {sym} real pos + active file -> re-tracked")
        logger.info(f"[RECONCILE-STARTUP] done — {len(real_positions)} real positions, {len(_s.bot_tracked_symbols)} tracked")
    except Exception as e:
        logger.warning(f"[RECONCILE-STARTUP] failed: {e}")


def _format_be_lock_config(cfg: dict) -> tuple[str, bool]:
    try:
        per_class = getattr(_o, "_PER_CLASS_CFG", None)
        if isinstance(per_class, dict) and per_class:
            parts: list[str] = []
            for cls in ("LOW", "MED", "HIGH"):
                c = per_class.get(cls)
                if not isinstance(c, dict):
                    continue
                try:
                    be_r  = c["be_r"]; l1_r = c["lock1_r"]; l2_r = c["lock2_r"]
                    parts.append(f"{cls} BE{be_r}R L1@{l1_r}R L2@{l2_r}R")
                except (KeyError, TypeError):
                    continue
            if parts:
                return ("BE/LOCK per-class [" + " | ".join(parts) + "]", True)
    except Exception as e:
        logger.debug(f"_format_be_lock_config layer-1 failed: {e}")

    try:
        has_r = all(k in cfg for k in _EXPECTED_R_KEYS)
        if has_r:
            return (f"BE {cfg['be_r_min']}R | "
                    f"LOCK {cfg['lock1_r_min']}R/{cfg['lock2_r_min']}R "
                    f"(R-scaled, MED fallback)", True)
    except Exception as e:
        logger.debug(f"_format_be_lock_config layer-2 failed: {e}")

    try:
        return (f"BE {cfg.get('be_factor', 0.6)} | "
                f"LOCK {cfg.get('lock1_pct', 0.9)}/{cfg.get('lock2_pct', 2.2)} "
                f"(legacy %)", False)
    except Exception as e:
        logger.debug(f"_format_be_lock_config failed: {e}")
        return ("BE/LOCK config unavailable", False)


def stop_bot() -> None:
    _s.request_stop()
    logger.info("Stop signal received from UI")


def _extract_family_rejection(reasons: list) -> str:
    """Extract the raw family-router rejection summary."""
    for r in (reasons or []):
        if not isinstance(r, str):
            continue
        r = r.strip()
        if not r:
            continue
        if r.startswith("strat="):
            continue
        return r[:60]
    return ""


def _format_skip_reasons(reasons, sig_1h, conf, lvl, min_rr, adx_v, min_adx) -> str:
    """Surface the ACTUAL family-router rejection reason."""
    family_rej = _extract_family_rejection(reasons)
    if family_rej:
        return f"SKIP ({family_rej})"

    parts: list[str] = []
    if "BUY" not in sig_1h and "SELL" not in sig_1h:
        parts.append("NoSetup")
    if conf < MIN_CONFIDENCE:
        parts.append(f"C{conf:.0f}%")
    rr_val = lvl.get("RR", 0)
    if rr_val < min_rr and rr_val > 0:
        parts.append(f"RR{rr_val:.1f}")
    if adx_v < min_adx and len(parts) < 2:
        parts.append(f"ADX{adx_v:.0f}")

    if not parts:
        return "SKIP"
    return f"SKIP ({','.join(parts[:2])})"


def _display_tf_signal(ind: dict | None) -> str:
    if not ind:
        return "N/A"
    try:
        e20 = float(ind.get("ema20", 0.0))
        e50 = float(ind.get("ema50", 0.0))
        p   = float(ind.get("price", 0.0))
    except (TypeError, ValueError):
        return "N/A"
    if p <= 0 or e50 <= 0:
        return "N/A"
    if e20 > e50 and p > e50:
        return "BUY"
    if e20 < e50 and p < e50:
        return "SELL"
    return "NEUTRAL"


def _compute_est_qty(balance: float, live_price: float, sl_price: float) -> float:
    try:
        if live_price <= 0 or sl_price <= 0 or balance <= 0:
            return 0.0
        risk_usd = balance * (RISK_PERCENT / 100.0)
        sl_dist = abs(live_price - sl_price)
        if sl_dist <= 0:
            return 0.0
        return risk_usd / sl_dist
    except Exception:
        return 0.0


def _safe_family(symbol: str) -> str:
    try:
        return get_family(symbol) or "unknown"
    except Exception:
        return "unknown"


def _extract_strategy(reasons: list) -> str:
    for r in (reasons or []):
        if isinstance(r, str) and r.startswith("strat="):
            return r.split("=", 1)[1].strip()
    return "UNKNOWN"


def _spread_label() -> str:
    """
    REV 1.4.24 — per-family spread filter state.

    Now includes the global fallback cap (max_spread_pct) so the
    banner matches config.py's `_spread_label()`. Previously only
    per-family values were shown, hiding the global fallback.
    """
    if not getattr(CONFIG, "use_spread_filter", False):
        return "OFF"
    _g = getattr(CONFIG, "max_spread_pct", 0.15)
    _t = getattr(CONFIG, "max_spread_trend", 0.08)
    _r = getattr(CONFIG, "max_spread_range", 0.12)
    _v = getattr(CONFIG, "max_spread_volatility", 0.25)
    return (f"ON (global {_g:.2f}% | trend {_t:.2f}% | "
            f"range {_r:.2f}% | vol {_v:.2f}%)")


def _update_btc_regime() -> None:
    """Compute BTC 1h regime, push into decision_engine (once per cycle)."""
    try:
        btc_df = _c.get_binance_klines("BTCUSDT", "1h", limit=250)
        if btc_df is None or len(btc_df) < 100:
            return
        btc_df_closed = btc_df.iloc[:-1] if len(btc_df) >= 3 else btc_df
        btc_ind = _i.calculate_pro_indicators(
            btc_df_closed, "1h", symbol="BTCUSDT"
        )
        if not btc_ind:
            return
        from signals.decision_engine import set_btc_regime
        set_btc_regime(btc_ind.get("regime", "UNKNOWN"))
        logger.debug(
            f"[BTC] regime={btc_ind.get('regime')} "
            f"mom={btc_ind.get('momentum_pct', 0):.2%} "
            f"hurst={btc_ind.get('hurst', 0):.3f} "
            f"adx={btc_ind.get('adx', 0):.1f}"
        )
    except Exception as _btce:
        logger.debug(f"BTC regime update failed: {_btce}")


def _fetch_coin_indicators(coin: str) -> tuple:
    """Thread-safe klines fetch + indicator computation for one coin."""
    dfs_closed: dict = {}
    results: dict = {}
    try:
        for tf in ('1h', '4h', '1d'):
            cached_ind = _i.get_cached_indicator(coin, tf)
            if cached_ind is not None:
                results[tf] = cached_ind
                if tf == '1h':
                    df_raw = _c.get_binance_klines(coin, tf)
                    dfs_closed[tf] = (
                        df_raw.iloc[:-1]
                        if df_raw is not None and len(df_raw) > 1
                        else df_raw
                    )
                continue
            df_raw = _c.get_binance_klines(coin, tf)
            if df_raw is None or len(df_raw) < 61:
                continue
            df_closed = df_raw.iloc[:-1] if len(df_raw) >= 3 else df_raw
            if tf == '1h':
                dfs_closed[tf] = df_closed
            ind = _i.calculate_pro_indicators(df_closed, tf, symbol=coin)
            if ind:
                results[tf] = ind
                _i.set_cached_indicator(coin, tf, ind)
    except Exception as e:
        logger.debug(f"_fetch_coin_indicators({coin}) failed: {e}")
    return coin, results, dfs_closed


# ═════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═════════════════════════════════════════════════════════════
def main_loop():
    if _c.get_client() is None:
        logger.error("Client not initialized.")
        return
    if not CONFIG.bot_coins:
        logger.critical("❌ CONFIG.bot_coins is empty")
        return

    _s.load_cooldowns()
    _o.clean_orders()
    coins = _c.validate_symbols(_c.get_bot_coins())
    if not coins:
        logger.error("No valid symbols.")
        return

    _cfg = _i.get_trading_config()
    _be_lock_str, _is_r_scaled = _format_be_lock_config(_cfg)

    if not _is_r_scaled:
        logger.warning(
            "⚠️  indicators.py has LEGACY (%) BE/LOCK config. "
            "REV 13.0 expects R-scaled keys."
        )

    logger.info(f"PRO TRADING v13.3.22 - {TRADING_MODE} | Risk {RISK_PERCENT}% | "
                f"Max {MAX_OPEN_POSITIONS} | DD {MAX_DAILY_DRAWDOWN_PERCENT}% | "
                f"Lev {LEVERAGE}x | MinADX {MIN_ADX}(fallback) | "
                f"PartialClose ${CONFIG.partial_close_usdt}")

    try:
        _floor_parts: list[str] = []
        for _fam_name, _fam_mod in _fr.FAMILIES.items():
            _madx = getattr(_fam_mod, "MIN_ADX", None)
            if isinstance(_madx, dict) and _madx:
                try:
                    _floor_parts.append(
                        f"{_fam_name}={float(min(_madx.values())):.1f}"
                    )
                except Exception:
                    continue
        if _floor_parts:
            logger.info("MinADX per-family floors: " + " | ".join(_floor_parts))
        else:
            logger.info(f"MinADX per-family floors: none — using fallback {MIN_ADX}")
    except Exception as _mfe:
        logger.debug(f"per-family MinADX log failed: {_mfe}")

    logger.info(
        f"Modern flags: taker={CONFIG.use_taker_volume} "
        f"vp={CONFIG.use_volume_profile} "
        f"avwap={CONFIG.use_anchored_vwap} "
        f"fz={CONFIG.use_funding_z} "
        f"mtf={CONFIG.use_mtf_confluence}"
    )

    logger.info(f" {_be_lock_str}")
    logger.info(f"Signal engine: family_router (4 families)")

    logger.info(f"Spread filter: {_spread_label()}")

    logger.info(
        f"5m trend filter: {'ON (trend-following only — counter-trend bypass)' if USE_5M_TREND_FILTER else 'OFF'}"
    )

    _debug_flag = os.getenv("DEBUG_STRATEGY", "false").strip().lower() == "true"
    logger.info(
        f"Strategy debug: {'✅ ON (per-coin rejections logged)' if _debug_flag else '⚠️ OFF'}"
    )

    try:
        _fam_summary = _fr.format_family_summary().replace("\n", " | ")
        logger.info(f"Total coins: {len(coins)} | Families: {_fam_summary}")
    except Exception as e:
        logger.warning(f"Family summary failed: {e}")

    # ── REV 1.4.24 — read MIN_APPROVALS dynamically ──
    try:
        from signals.decision_engine import MIN_APPROVALS as _MA_BANNER
    except Exception:
        _MA_BANNER = "?"

    try:
        import signals.decision_engine as decision_engine# noqa: F401
        logger.info(f"Decision engine: ✅ active (6-filter vote, {_MA_BANNER}/6 required)")
        logger.info("BTC-bias gate:   ✅ ACTIVE (REV 1.0.5) — updated each scan cycle")
        logger.info("MTF gate:        ✅ decision_engine only (REV 1.4.13 — hard-gate removed)")
    except Exception as e:
        logger.warning(f"Decision engine: ❌ not loaded ({e}) — trade gate DISABLED")

    logger.info(f"Scan: parallel fetch (workers={_PARALLEL_FETCH_WORKERS})")
    logger.info(f"Running in TUNED mode — MIN_APPROVALS={_MA_BANNER}/6, "
                f"counter_trend_cutoff=35, st_rsi_sell_floor per-family")

    _o.sync_existing_positions()

    with _s.BOT_TRACKED_LOCK:
        startup_syms = list(_s.bot_tracked_symbols)
    if startup_syms:
        logger.info(f" Startup reconciliation for {len(startup_syms)} symbols...")
        for s in startup_syms:
            try:
                _o.reconcile_active_trade(s)
            except Exception as e:
                logger.warning(f"startup reconcile {s} failed: {e}")

    try:
        for s in _c.get_bot_coins():
            if s.removesuffix('USDT') not in _s.bot_tracked_symbols:
                _o.cancel_orphan_bot_orders(s)
    except Exception:
        pass

    try:
        _c.refresh_timestamp()
        bal_init = float(_c.get_client().futures_account()['totalWalletBalance'])
        _s.daily_tracker.reset_if_new_day(bal_init)
        _s.daily_tracker.update_peak(bal_init)
    except Exception as e:
        logger.warning(f"Initial balance fetch error: {e}")

    if not Path(CSV_FILE).exists():
        with open(CSV_FILE, 'w', encoding='utf-8') as f:
            f.write('timestamp,symbol,side,entry,price,sl,tp1,tp2,qty,'
                    'conf,rr,pattern,fg,strategy\n')
    if not Path(CSV_EXIT_FILE).exists():
        with open(CSV_EXIT_FILE, 'w', encoding='utf-8') as f:
            f.write('timestamp,symbol,realized_pnl,reason\n')

    try:
        while not _s.is_stopped():
            try:
                if Path(PAUSE_FILE).exists():
                    logger.info(" PAUSE file detected - paused 30s.")
                    time.sleep(30); continue

                fg_value, fg_class = _i.get_fear_greed_index()
                live_prices = _c.get_live_prices()

                book_tickers: dict = {}
                if getattr(CONFIG, "use_spread_filter", False):
                    try:
                        book_tickers = _c.get_book_tickers() or {}
                    except Exception as _bte:
                        logger.debug(f"book_tickers fetch failed: {_bte}")

                _update_btc_regime()

                degraded = False
                try:
                    balance, degraded = _c.get_balance_or_last_good()
                    if not degraded:
                        _s.daily_tracker.update_peak(balance)
                except Exception as e:
                    logger.error(f"Balance fetch failed (no last-good): {e} — pausing")
                    time.sleep(30); continue

                if degraded:
                    logger.warning("🔶 Degraded mode — skipping new entries this cycle")
                    time.sleep(30); continue

                is_limited, reason = _s.daily_tracker.is_limit_reached(balance)
                if is_limited:
                    logger.warning(f" Daily limit: {reason}. Paused 60s.")
                    time.sleep(60); continue

                active_trades_list = []
                try:
                    _positions_all = _c.get_client().futures_position_information()
                    _pos_by_sym = {
                        p.get('symbol'): p
                        for p in (_positions_all or [])
                        if abs(float(p.get('positionAmt', 0) or 0)) > 0
                    }
                    for coin in coins:
                        _p = _pos_by_sym.get(coin)
                        if _p is None:
                            continue
                        try:
                            st = _o.get_trade_status(coin.replace('USDT', ''), pos=_p)
                        except TypeError:
                            st = _o.get_trade_status(coin.replace('USDT', ''))
                        if st:
                            active_trades_list.append(st)
                except Exception as _pex:
                    logger.debug(f"batch position fetch failed: {_pex}")

                if active_trades_list:
                    logger.info(f" {len(active_trades_list)} active | Bal ${balance:.2f}")
                else:
                    logger.info(f" No active trades | Bal ${balance:.2f}")

                scan_rows = []
                trade_executed = False

                candidates = list(coins)

                # ═══════════════════════════════════════════════════════
                #  PHASE 2 — PARALLEL FETCH
                # ═══════════════════════════════════════════════════════
                indicators_by_coin: dict = {}
                if candidates:
                    try:
                        with ThreadPoolExecutor(
                            max_workers=_PARALLEL_FETCH_WORKERS,
                            thread_name_prefix="ind_fetch",
                        ) as pool:
                            futures = {
                                pool.submit(_fetch_coin_indicators, c): c
                                for c in candidates
                            }
                            for fut in as_completed(futures):
                                _coin_for_fut = futures[fut]
                                try:
                                    _c_key, _res, _dfs = fut.result()
                                    indicators_by_coin[_c_key] = (_res, _dfs)
                                except Exception as _fe:
                                    logger.debug(
                                        f"Parallel fetch failed for "
                                        f"{_coin_for_fut}: {_fe}"
                                    )
                    except Exception as _pe:
                        logger.warning(
                            f"Parallel fetch pool error: {_pe} — "
                            f"falling back to serial for remaining"
                        )
                        for c in candidates:
                            if c in indicators_by_coin:
                                continue
                            try:
                                _c_key, _res, _dfs = _fetch_coin_indicators(c)
                                indicators_by_coin[_c_key] = (_res, _dfs)
                            except Exception:
                                continue

                # ═══════════════════════════════════════════════════════
                #  PHASE 3 — SERIAL EVALUATION
                # ═══════════════════════════════════════════════════════
                for coin in candidates:
                    results, dfs_closed = indicators_by_coin.get(coin, ({}, {}))
                    ind_1h = results.get('1h')
                    ind_4h = results.get('4h')
                    ind_1d = results.get('1d')
                    df_1h = dfs_closed.get('1h')

                    symbol = coin.replace('USDT', '')
                    _family = _safe_family(coin)
                    live_price = live_prices.get(coin, 0.0)

                    if not ind_1h or not ind_4h or df_1h is None:
                        scan_rows.append({
                            'symbol': symbol,
                            'sig_1h': '-', 'sig_4h': '-', 'sig_1d': '-',
                            'conf': 0, 'price': live_price, 'rr': 0, 'adx': 0,
                            'pattern': 'TrendPullback', 'regime': 'UNKNOWN',
                            'killzone': '-', 'family': _family,
                            'action': 'No Data'
                        })
                        continue

                    if live_price <= 0:
                        live_price = ind_1h.get('price', 0.0)

                    _regime   = ind_1h.get('regime', 'UNKNOWN')
                    _killzone = ind_1h.get('killzone', 'NONE')

                    candle_time = None
                    try:
                        candle_time = df_1h.index[-1]
                    except Exception:
                        candle_time = None

                    _signal_price = float(ind_1h.get('price', 0.0) or 0.0)

                    sig_1h, conf, reasons, lvl, div = _fr.generate_signal_live(
                        ind_1h, ind_4h, ind_1d,
                        symbol=coin,
                    )

                    sig_4h = _display_tf_signal(ind_4h)
                    sig_1d = _display_tf_signal(ind_1d)
                    pat_disp = "TrendPullback"

                    cfg = _i.get_trading_config()
                    min_rr = cfg['min_rr']
                    min_adx_here = _get_min_adx_for_coin(coin)
                    trade_side = None
                    skip_reason = None
                    strategy_name = "UNKNOWN"

                    _strategy_name_for_check = "UNKNOWN"

                    _is_buy  = ("STRONG_BUY"  in sig_1h or sig_1h == "BUY")
                    _is_sell = ("STRONG_SELL" in sig_1h or sig_1h == "SELL")

                    if _is_buy or _is_sell:
                        _conf_ok = conf >= MIN_CONFIDENCE
                        _rr_ok   = lvl.get('RR', 0) >= min_rr
                        _adx_ok  = ind_1h['adx'] >= min_adx_here

                        _side_temp = 'BUY' if _is_buy else 'SELL'

                        # ═══════════════════════════════════════════════
                        #  REV 1.4.21 — CONDITIONAL 5M TREND FILTER
                        # ═══════════════════════════════════════════════
                        _trend_ok = True
                        _trend_reason = ""
                        _strategy_name_for_check = _extract_strategy(reasons)
                        _is_counter_trend = (
                            _strategy_name_for_check in _CT_STRATEGIES
                        )

                        if (USE_5M_TREND_FILTER
                                and not _is_counter_trend
                                and _conf_ok and _rr_ok and _adx_ok):
                            _trend_ok, _trend_reason = _i.check_short_term_trend(
                                coin, _side_temp
                            )

                        if _conf_ok and _rr_ok and _adx_ok and _trend_ok:
                            trade_side = _side_temp
                            _s.increment_v2_stat('signals')
                        else:
                            if not _conf_ok:
                                _s.increment_v2_stat('low_conf')
                            if not _rr_ok:
                                _s.increment_v2_stat('low_rr')
                            if not _adx_ok:
                                _s.increment_v2_stat('low_adx')
                            if not _trend_ok:
                                _s.increment_v2_stat('trend')
                                skip_reason = f'V2-Trend: {_trend_reason}'

                    # ═══════════════════════════════════════════════════════
                    #  REV 1.4.23 — PRE-CHECKS BEFORE DECISION ENGINE (FIX Bug #1)
                    #  Cooldown / Rotation / IN POSITION / Same Candle must
                    #  run BEFORE evaluate_trade() — otherwise the decision
                    #  engine logs APPROVED for signals that will be blocked
                    #  anyway, wasting the 6-filter vote and polluting the
                    #  log (VIRTUAL 12:21+ incident, 2026-09-30).
                    # ═══════════════════════════════════════════════════════
                    _pre_block_reason = None

                    if trade_side:
                        with _s.COOLDOWN_LOCK:
                            _cd = _s.cooldown_until.get(symbol)
                            if _cd is not None and datetime.now(PKT) < _cd:
                                _cd_remain = int(
                                    (_cd - datetime.now(PKT)).total_seconds() // 60
                                )
                                _pre_block_reason = f"Cooldown {_cd_remain}m"

                    if _pre_block_reason is None and trade_side:
                        rot_ok, rot_reason = _s.can_trade_coin(symbol)
                        if not rot_ok:
                            _pre_block_reason = f"V2-Rotation: {rot_reason}"
                            _s.increment_v2_stat('rotation')

                    if _pre_block_reason is None and trade_side:
                        if any(t.get('symbol') == symbol
                               for t in active_trades_list):
                            _pre_block_reason = "IN POSITION"

                    if _pre_block_reason is None and trade_side and candle_time is not None:
                        with _s.ENTRY_CANDLE_LOCK:
                            if _s.last_entry_candle.get(symbol) == candle_time:
                                _pre_block_reason = "Same Candle"

                    if _pre_block_reason:
                        trade_side = None
                        skip_reason = _pre_block_reason

                    # ═══════════════════════════════════════════════════════
                    #  DECISION ENGINE (only if not pre-blocked)
                    # ═══════════════════════════════════════════════════════
                    if trade_side:
                        strategy_name = _strategy_name_for_check
                        try:
                            from signals.decision_engine import evaluate_trade
                            decision = evaluate_trade(
                                symbol=symbol,
                                side=trade_side,
                                ind_1h=ind_1h,
                                ind_4h=ind_4h,
                                ind_1d=ind_1d,
                                strategy=strategy_name,
                                conf=conf,
                                rr=lvl.get('RR', 0),
                                active_trades_list=active_trades_list,
                            )
                            if not decision.approved:
                                skip_reason = f"DECISION: {decision.top_reason}"
                                _s.increment_v2_stat('decision')
                                trade_side = None
                        except ImportError:
                            pass
                        except Exception as _de:
                            logger.debug(
                                f"[decision] {symbol}: "
                                f"{type(_de).__name__}: {_de} — allowing trade"
                            )

                    # ── POST-DECISION SAFETY RE-CHECK ──
                    # Belt-and-suspenders: if somehow the decision engine
                    # allowed a trade on an already-tracked symbol (race
                    # with local active_trades_list update), block it now.
                    if trade_side and any(
                        t.get('symbol') == symbol for t in active_trades_list
                    ):
                        trade_side = None
                        skip_reason = "IN POSITION"

                    # ── SPREAD FILTER ──
                    if trade_side and getattr(CONFIG, "use_spread_filter", False):
                        _bt = book_tickers.get(coin)
                        if _bt:
                            _bid = _bt.get('bid', 0.0)
                            _ask = _bt.get('ask', 0.0)
                            _spread_pct = _c.calc_spread_pct(_bid, _ask)
                            _cap = _get_spread_cap_for_family(_family)
                            if _spread_pct > 0 and _spread_pct > _cap:
                                skip_reason = (
                                    f"SPREAD {_spread_pct:.3f}% > "
                                    f"{_cap:.2f}% ({_family})"
                                )
                                logger.info(
                                    f"[{symbol}] {skip_reason} — "
                                    f"skipping entry (bid={_bid} ask={_ask})"
                                )
                                trade_side = None

                    # ═══════════════════════════════════════════════════
                    #  ORDER PLACEMENT
                    # ═══════════════════════════════════════════════════
                    if trade_side:
                        est_qty = _compute_est_qty(balance, live_price, lvl['SL'])

                        if candle_time is not None:
                            with _s.ENTRY_CANDLE_LOCK:
                                _s.last_entry_candle[symbol] = candle_time

                        order_succeeded = False
                        try:
                            order_succeeded = _o.place_order_fixed(
                                symbol, trade_side,
                                est_qty, lvl['SL'], lvl['TP1'], lvl['TP2'],
                                live_price, conf, lvl.get('RR', 0),
                                pat_disp, fg_value,
                                strategy=strategy_name,
                                signal_price=_signal_price,
                            )
                        except Exception as order_err:
                            logger.error(f"[{symbol}] place_order_fixed RAISED: "
                                         f"{type(order_err).__name__}: {order_err}",
                                         exc_info=True)
                            order_succeeded = False

                        if order_succeeded:
                            if not DRY_RUN:
                                _s.record_coin_trade(symbol)

                            try:
                                _new_status = _o.get_trade_status(symbol)
                                if _new_status:
                                    active_trades_list.append(_new_status)
                                    logger.debug(
                                        f"[{symbol}] local active_trades_list "
                                        f"updated → {len(active_trades_list)} total"
                                    )
                                else:
                                    active_trades_list.append({
                                        'symbol': symbol,
                                        'side': 'LONG' if trade_side == 'BUY' else 'SHORT',
                                    })
                            except Exception as _ase:
                                logger.debug(
                                    f"[{symbol}] local active_trades append failed: {_ase}"
                                )
                                active_trades_list.append({
                                    'symbol': symbol,
                                    'side': 'LONG' if trade_side == 'BUY' else 'SHORT',
                                })

                            action = f" EXECUTED {trade_side} [{strategy_name}]"
                            trade_executed = True
                        else:
                            with _s.ENTRY_CANDLE_LOCK:
                                if symbol in _s.last_entry_candle:
                                    del _s.last_entry_candle[symbol]
                            action = " FAILED"
                    elif skip_reason:
                        action = skip_reason
                    else:
                        action = _format_skip_reasons(
                            reasons, sig_1h, conf, lvl, min_rr,
                            ind_1h['adx'], min_adx_here
                        )

                    scan_rows.append({
                        'symbol': symbol, 'sig_1h': sig_1h,
                        'sig_4h': sig_4h, 'sig_1d': sig_1d,
                        'conf': conf, 'price': live_price,
                        'rr': lvl.get('RR', 0),
                        'adx': ind_1h['adx'],
                        'pattern': pat_disp,
                        'regime': _regime,
                        'killzone': _killzone,
                        'family': _family,
                        'action': action
                    })

                ts = datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')
                if trade_executed:
                    ts += f" | {len(_s.bot_tracked_symbols)} active"
                _s.set_scan_results(scan_rows, ts)

                logger.info(f" Scan completed {ts} | "
                            f"Trade Executed: {trade_executed} | "
                            f"{_s.get_v2_stats_str()}")
                _i.cleanup_indicator_cache()
                _s.cleanup_entry_candles()

                for _ in range(15):
                    if _s.is_stopped():
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                logger.info(" Stopped by User")
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(10)
    finally:
        try:
            _s.save_cooldowns()
            logger.info(" Cooldowns saved — main_loop exit")
        except Exception as e:
            logger.warning(f"save_cooldowns on exit failed: {e}")


if __name__ == "__main__":
    from core.config import print_config_banner
    from getpass import getpass
    import sys
    import signal as _signal

    print_config_banner()

    if DEMO_MODE and TESTNET:
        logger.warning("Both DEMO_MODE and TESTNET True — DEMO takes priority.")
    mode_label = ("DEMO" if DEMO_MODE else
                  "TESTNET" if TESTNET else "!!! MAINNET - REAL MONEY !!!")
    logger.warning(f"===== STARTUP MODE: {mode_label} =====")

    if not DEMO_MODE and not TESTNET:
        confirm = input(f"MAINNET with real funds (Lev {LEVERAGE}x, "
                        f"Risk {RISK_PERCENT}%). Type 'YES': ")
        if confirm != "YES":
            logger.info("Exiting."); sys.exit()

    api_key = CONFIG.api_key
    api_secret = CONFIG.api_secret
    if not api_key or not api_secret:
        api_key = getpass("Enter API Key: ").strip().strip('"').strip("'")
        api_secret = getpass("Enter Secret Key: ").strip().strip('"').strip("'")
    _c.set_client_keys(api_key, api_secret)

    ok, bal = _c._validate_api_credentials(_c.get_client())
    if not ok:
        logger.critical("Aborting startup — fix API key and re-run.")
        sys.exit(1)
    logger.info(f" Connected! Futures Wallet Balance: ${bal:.2f}")

    _s.load_active_trades()
    _s.clear_stop()
    _reconcile_on_startup()
    threading.Thread(target=_o.trade_manager_loop,
                     daemon=True, name="tm_engine").start()

    def _signal_handler(sig, frame):
        logger.info("Shutdown signal — stopping...")
        stop_bot()
        _s.save_cooldowns()
        sys.exit(0)
    _signal.signal(_signal.SIGINT, _signal_handler)
    _signal.signal(_signal.SIGTERM, _signal_handler)

    main_loop()
    _s.save_cooldowns()