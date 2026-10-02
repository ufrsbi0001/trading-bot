"""
future.py — Trading engine orchestrator + main scan loop.

REV 1.4.28 (2026-10-02) — LIVE CONFIG READS (post-migration):
  ✅ Banner MIN_APPROVALS: was `from signals.decision_engine import
     MIN_APPROVALS` — that module no longer caches constants (REV 4.2
     reads LIVE). The import was silently failing → `?/6` shown in
     banner. Now read via `CC.get_decision_cfg()` directly.
  ✅ `_spread_label()`: was reading `CONFIG.max_spread_*` via the
     core/config.py proxy. Now reads LIVE from `CC.get(...)`.
  ✅ `use_spread_filter` gate (2 places): was `getattr(CONFIG,
     "use_spread_filter", False)`. Now `CC.get("use_spread_filter",
     False)` — matches the rest of the file.
  ✅ Dead `import signals.decision_engine as decision_engine# noqa`
     removed — was only used for the import-side-effect, no longer
     needed since we read live via CC.
  ✅ `CONFIG` import retained — still used for env-level fields
     (bot_coins, dry_run, demo_mode, testnet, api_key, api_secret).
     These belong in core/config.py and MUST NOT move to config_center.

REV 1.4.27 (2026-10-02) — DEAD RE-EXPORT CLEANUP.
REV 1.4.26 (2026-10-02) — PHASE 3 CLEANUP.
REV 1.4.25 (2026-10-02) — UNIFIED CONFIG CLEANUP.
REV 1.4.24 (2026-09-30) — DYNAMIC STARTUP BANNER VALUES.
REV 1.4.23 (2026-09-30) — FIX Bug #1: PRE-CHECKS BEFORE DECISION ENGINE.
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

from core.config import CONFIG           # env-only fields (api_key, bot_coins, ...)
from core import config_center as CC     # trading params (live reads)
from core.coins_config import get_family

# ── REV 1.4.25 — Unified config source for BE/LOCK banner ──
from core.config_center import VOL_CLASS_R_THRESHOLDS as _VOL_CLASS_R_THRESHOLDS

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
# NOTE (REV 1.4.27): `retry_on_rate_limit` and `HARDCODED_FILTERS`
# re-exports removed — no live consumer (verified via grep).
# `client.py` still exports them; import directly if needed.
_run_with_timeout         = _c._run_with_timeout
_get_open_algo_orders     = _c._get_open_algo_orders
_cancel_algo_order        = _c._cancel_algo_order
_position_amt             = _c._position_amt

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
# NOTE (REV 1.4.27): `detect_candle_patterns` re-export removed —
# no live consumer. `indicators.py` still exports it.

# ── Module-level constant snapshots (for banner/log only) ──
# NOTE: These are read ONCE at import time. For runtime-tunable values
# that change via update_runtime(), read LIVE via CC.get(...) at use site.
RISK_PERCENT               = CC.get('risk_percent')
MAX_OPEN_POSITIONS         = CC.get('max_open_positions')
MAX_DAILY_DRAWDOWN_PERCENT = CC.get('max_daily_drawdown_percent')
MIN_CONFIDENCE             = CC.get('min_confidence')
MIN_ADX                    = CC.get('min_adx')
REQUIRE_HTF_AGREEMENT      = CC.get('require_htf_agreement')
USE_5M_TREND_FILTER        = CC.get('use_5m_trend_filter')
FG_ENABLED                 = CC.get('fg_enabled')
LEVERAGE                   = CC.get('leverage')

# ── Env-only flags (from core/config.py, NOT config_center) ──
DRY_RUN                    = CONFIG.dry_run
DEMO_MODE                  = CONFIG.demo_mode
TESTNET                    = CONFIG.testnet
TRADING_MODE               = "TREND_PULLBACK"

PKT = _s.PKT
BLACKLISTED_COINS = _s.BLACKLISTED_COINS
CSV_FILE          = _s.CSV_FILE
CSV_EXIT_FILE     = _s.CSV_EXIT_FILE
PAUSE_FILE        = _s.PAUSE_FILE

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
    """Per-family spread cap — reads LIVE from config_center."""
    try:
        if family == "trend_coins":
            return float(CC.get('max_spread_trend', CC.get('max_spread_pct')))
        if family == "range_coins":
            return float(CC.get('max_spread_range', CC.get('max_spread_pct')))
        if family in ("volatility_coins", "momentum_coins"):
            return float(CC.get('max_spread_volatility', CC.get('max_spread_pct')))
    except Exception:
        pass
    return float(CC.get('max_spread_pct'))


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


def _format_be_lock_config() -> str:
    """
    REV 1.4.26 (PHASE 3) — Simplified.

    Previously had 3 fallback layers (per-class, R-scaled MED, legacy %).
    Layers 2 and 3 were 100% dead because VOL_CLASS_R_THRESHOLDS is
    always populated (LOW/MED/HIGH present in config_center). Removed.

    Returns a single string like:
        "BE/LOCK per-class [LOW BE0.7R L1@1.0R L2@1.6R | MED ...]"
    """
    per_class = _VOL_CLASS_R_THRESHOLDS
    parts: list[str] = []
    for cls in ("LOW", "MED", "HIGH"):
        c = per_class.get(cls)
        if not isinstance(c, dict):
            continue
        try:
            be_r = c["be_r"]
            l1_r = c["lock1_r"]
            l2_r = c["lock2_r"]
            parts.append(f"{cls} BE{be_r}R L1@{l1_r}R L2@{l2_r}R")
        except (KeyError, TypeError):
            continue
    if parts:
        return "BE/LOCK per-class [" + " | ".join(parts) + "]"
    return "BE/LOCK config unavailable"


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
    REV 1.4.28 — per-family spread filter state.

    Now reads LIVE from config_center (was: `getattr(CONFIG, ...)` via
    core/config.py proxy — same values but consistent with the rest
    of the file's live-read pattern).
    """
    if not CC.get("use_spread_filter", True):
        return "OFF"
    _g = CC.get("max_spread_pct", 0.15)
    _t = CC.get("max_spread_trend", 0.08)
    _r = CC.get("max_spread_range", 0.12)
    _v = CC.get("max_spread_volatility", 0.25)
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
    _be_lock_str = _format_be_lock_config()
    # NOTE (REV 1.4.26): _is_r_scaled always True now (VOL_CLASS_R_THRESHOLDS
    # always populated) — legacy warning removed.

    logger.info(f"PRO TRADING v13.3.22 - {TRADING_MODE} | Risk {RISK_PERCENT}% | "
                f"Max {MAX_OPEN_POSITIONS} | DD {MAX_DAILY_DRAWDOWN_PERCENT}% | "
                f"Lev {LEVERAGE}x | MinADX {MIN_ADX}(fallback) | "
                f"PartialClose ${CC.get('partial_close_usdt')}")

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
        f"Modern flags: taker={CC.get('use_taker_volume')} "
        f"vp={CC.get('use_volume_profile')} "
        f"avwap={CC.get('use_anchored_vwap')} "
        f"fz={CC.get('use_funding_z')} "
        f"mtf={CC.get('use_mtf_confluence')}"
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

    # ── REV 1.4.28 — read MIN_APPROVALS LIVE from config_center ──
    # (was: `from signals.decision_engine import MIN_APPROVALS` — that
    #  module no longer caches constants; REV 4.2 reads LIVE. The old
    #  import silently fell back to "?" in the banner.)
    try:
        _MA_BANNER = int(CC.get_decision_cfg().get("min_approvals", 5))
    except Exception as _ma_err:
        logger.debug(f"MIN_APPROVALS banner read failed: {_ma_err}")
        _MA_BANNER = "?"

    try:
        # Sanity-check that the decision engine module loads cleanly.
        import signals.decision_engine  # noqa: F401
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
                # REV 1.4.28 — live read (was getattr(CONFIG, ...))
                if CC.get("use_spread_filter", True):
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
                    # REV 1.4.28 — live read (was getattr(CONFIG, ...))
                    if trade_side and CC.get("use_spread_filter", True):
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