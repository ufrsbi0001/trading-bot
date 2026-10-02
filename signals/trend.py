"""
trend_coins.py — TREND family signal engine.

REV 21.2 (2026-09-30) — FAMILY-SPECIFIC TUNING:
  ✅ Trend coins (BTC, ETH, SOL, BNB, etc.) are liquid and smooth —
     they need ROOM to extend but reject earlier than high-beta alts.
     st_rsi_sell_floor 30.0 → 28.0 (trend coins rarely hit 30).
     st_min_dist_atr kept at 0.5 (already reasonable).
     st_max_chase_atr 1.5 → 1.8 (give room for smooth trends).
     st_flips_min 5 → 4 (early trends need fewer flips confirmed).
     st_flips_max kept at 10.

REV 21.1 (2026-09-30) — OVER-FIT REBALANCE.
REV 21.0 (2026-09-29) — DATA-DRIVEN TUNING PASS.
REV 20.6 (2026-09-29) — LIVE FIRE UNLOCK (TUNING).
REV 20.5 (2026-09-29) — RANGE-SCALPER BOUNDS WIDENED.
REV 20.4 (2026-09-29) — BOUNCE-ZONE SHORT FIX.
REV 20.3 (2026-09-29) — BATCH 3.7 UNLOCK FIXES.
REV 20.2 (2026-09-28) — RSI BANDS + PULLBACK ENTRY.
REV 20.0 (2026-09-28) — BATCH 3 REFACTOR.
REV 19.19 (2026-09-28) — SHARED HTF ALIGNMENT.
REV 19.18 (2026-09-28) — DEBUG PRINT GATED BEHIND ENV FLAG.
REV 19.17 (2026-09-26) — DEAD CODE CLEANUP + COMMENT TRUTH-UP.
REV 19.16 (2026-09-26) — TP2 DIRECTION FIX + DEAD-CODE + NONE GUARD.
REV 19.15 (2026-09-26) — SIGNAL FREQUENCY UNLOCK.
REV 19.14 (2026-09-25) — DEFENSIVE HARDENING.
REV 19.13 (2026-09-25) — KZ_BYPASS FROM CONFIG.
REV 19.12 (2026-09-24) — DEAD CONSTANTS REMOVED.
REV 19.11 (2026-09-24) — SINGLE SOURCE OF TRUTH FOR MIN_CONFIDENCE.
REV 19.10 (2026-09-24) — PHASE 2 MODERN INDICATOR WIRING.
REV 19.9 (2026-09-23) — VOLATILE FIX + DEAD CODE REMOVAL.
REV 19.8 (2026-09-23) — PER-COIN DIAGNOSTICS FIX.
REV 19.7 (2026-09-23) — BUG FIXES (SELL side + region alignment).
REV 19.6 (2026-09-23) — PER-COIN RSI FOR ALL 5 STRATEGIES.
REV 19.5 (2026-09-23) — STRATEGY TUNING.
REV 19.2 (2026-09-22) — DEAD CONSTANT CLEANUP.
REV 19.1 (2026-09-22) — TD_FADE BUY BRANCH FIX.
REV 19.0 (2026-09-22) — PER-COIN FILTERS.
"""
from __future__ import annotations

from core.config import CONFIG
from core.coins_config import get_family_coins

from signals.base import (
    FamilySpec, DiagnosticsTracker, build_all_strategies,
    route_signal as _route_signal_base,
    generate_signal_live as _generate_signal_live_base,
    is_family_coin as _is_family_coin_base,
    calc_risk_usd, get_risk_per_trade,
    calc_position_size, calc_notional, calc_margin,
)


FAMILY_NAME  = "TREND"
FAMILY_COINS = get_family_coins("trend_coins")

__all__ = [
    "FAMILY_NAME", "FAMILY_COINS",
    "REGIME_STRATEGIES", "ALL_STRATEGIES", "DISABLED_STRATEGIES",
    "is_family_coin",
    "route_signal", "generate_signal_live",
    "reset_diagnostics", "get_diagnostics", "format_diagnostics",
    "calc_risk_usd", "get_risk_per_trade",
    "calc_position_size", "calc_notional", "calc_margin",
]


REGIME_STRATEGIES = {
    "TREND_UP":   {"SUPERTREND_RIDE"},
    "TREND_DOWN": {"TREND_DOWN_FADE", "SUPERTREND_RIDE"},
    "CHOP":       {"MEAN_REVERSION", "RANGE_SCALPER"},
    "QUIET":      {"RANGE_SCALPER"},
    "VOLATILE":   {"SUPERTREND_RIDE", "RANGE_SCALPER"},
    "UNKNOWN":    {"SUPERTREND_RIDE"},
}

DISABLED_STRATEGIES: set[str] = {"CHOP_FADE"}

MIN_ADX = {
    "MEAN_REVERSION":   15.0,
    "RANGE_SCALPER":    12.0,
    "SUPERTREND_RIDE":  18.0,
    "TREND_DOWN_FADE":  20.0,
}

FALLBACK_CAPS = {"sl": 0.050, "tp1": 0.050, "tp2": 0.125}


_SPEC = FamilySpec(
    name=FAMILY_NAME,
    family_key="trend_coins",
    regime_strategies=REGIME_STRATEGIES,
    disabled_strategies=DISABLED_STRATEGIES,
    min_adx=MIN_ADX,
    fallback_caps=FALLBACK_CAPS,
    min_confidence=CONFIG.min_confidence,
    min_adx_env=18.0,
    min_sl_pct=0.0075,
    min_adx_outside_kz=30.0,
    kz_bypass=CONFIG.kz_bypass,
    block_ny_am_for_st=False,
    block_ny_am_for_td_fade=False,
    block_quiet=False,
    counter_trend_adx_cutoff=35.0,
    rsi_buy_overbought=82.0,
    rsi_sell_oversold=25.0,
    top_chase_rsi=72.0,
    top_chase_flips=6,
    late_entry_guard_adx=55.0,
    late_entry_guard_dist=1.3,
    late_entry_guard_rsi=70.0,
    # ── REV 21.2: family-specific tuning ──
    st_min_dist_atr=0.5,
    st_max_chase_atr=1.8,
    st_flips_min=4,
    st_flips_max=10,
    st_rsi_buy_min=38.0,
    st_rsi_buy_max=70.0,
    st_rsi_sell_min=30.0,
    st_rsi_sell_max=65.0,
    st_rsi_sell_floor=28.0,
    pullback_dist_atr=1.8,
    mr_rsi_buy_max=30.0,
    mr_rsi_sell_min=70.0,
    mr_stoch_buy_max=25.0,
    mr_stoch_sell_min=75.0,
    rs_rsi_buy_max=35.0,
    rs_rsi_sell_min=65.0,
    td_fade_rsi_sell=65.0,
    td_fade_rsi_buy=32.0,
    td_fade_sl_atr=0.9,
    td_fade_adx_max=55.0,
    td_fade_ema_tol=1.02,
    td_fade_min_adx=20.0,
    td_fade_bb_pos_sell=0.98,
    td_fade_bb_pos_buy=0.02,
)
_SPEC._rs_rng_min = 0.004
_SPEC._rs_rng_max = 0.20

_TRACKER = DiagnosticsTracker()
_ALL_STRATEGIES = build_all_strategies(_SPEC, _TRACKER)
ALL_STRATEGIES = _ALL_STRATEGIES


def is_family_coin(symbol: str) -> bool:
    return _is_family_coin_base(FAMILY_COINS, symbol)


def route_signal(ind_1h, ind_4h, ind_1d=None,
                 profile: str = "MIXED", symbol: str = "",
                 mode: str = None):
    return _route_signal_base(
        _SPEC, _TRACKER, _ALL_STRATEGIES,
        ind_1h, ind_4h, ind_1d,
        profile=profile, symbol=symbol, mode=mode,
    )


def generate_signal_live(ind_1h, ind_4h, ind_1d=None,
                         symbol="", mode="MULTI"):
    return _generate_signal_live_base(
        _SPEC, _TRACKER, FAMILY_COINS, _ALL_STRATEGIES,
        ind_1h, ind_4h, ind_1d, symbol=symbol, mode=mode,
    )


def reset_diagnostics() -> None:
    _TRACKER.reset()


def get_diagnostics() -> dict:
    return _TRACKER.snapshot()


def format_diagnostics() -> str:
    return _TRACKER.format()