"""
volatility_coins.py — VOLATILITY family signal engine.

REV 21.2 (2026-09-30) — FAMILY-SPECIFIC TUNING:
  ✅ Volatility coins (high-beta alts) are wild — they extend 2-3 ATR
     during active phases and their RSI can dip to 25 without
     mean-reverting. Tuning them same as trend coins was over-restrict.
     st_rsi_sell_floor 30.0 → 25.0 (vol coins RSI dips to 25).
     st_min_dist_atr 0.5 → 0.6 (ATR already inflated — need more buffer).
     st_max_chase_atr 1.5 → 2.5 (wild extensions need room).
     st_flips_min kept at 5.
     st_flips_max 10 → 12 (more flips natural for vol coins).

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
REV 19.16 (2026-09-26) — TP2 DIRECTION FIX + NONE GUARD.
REV 19.15 (2026-09-26) — SIGNAL FREQUENCY UNLOCK.
REV 19.13 (2026-09-25) — KZ_BYPASS FROM CONFIG.
REV 19.12 (2026-09-25) — ROUTER + DEFENSIVE FIXES.
REV 19.11 (2026-09-25) — HARDENING PASS.
REV 19.9 (2026-09-24) — Initial release with REV 19.9 VOLATILE fix.
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


FAMILY_NAME  = "VOLATILITY"
FAMILY_COINS = get_family_coins("volatility_coins")

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

FALLBACK_CAPS = {"sl": 0.080, "tp1": 0.080, "tp2": 0.200}


_SPEC = FamilySpec(
    name=FAMILY_NAME,
    family_key="volatility_coins",
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
    rsi_buy_overbought=84.0,
    rsi_sell_oversold=22.0,
    top_chase_rsi=74.0,
    top_chase_flips=7,
    late_entry_guard_adx=55.0,
    late_entry_guard_dist=1.5,
    late_entry_guard_rsi=70.0,
    # ── REV 21.2: family-specific tuning ──
    st_min_dist_atr=0.6,
    st_max_chase_atr=2.5,
    st_flips_min=5,
    st_flips_max=12,
    st_rsi_buy_min=34.0,
    st_rsi_buy_max=72.0,
    st_rsi_sell_min=28.0,
    st_rsi_sell_max=66.0,
    st_rsi_sell_floor=25.0,
    pullback_dist_atr=1.8,
    mr_rsi_buy_max=32.0,
    mr_rsi_sell_min=68.0,
    mr_stoch_buy_max=28.0,
    mr_stoch_sell_min=72.0,
    rs_rsi_buy_max=35.0,
    rs_rsi_sell_min=65.0,
    td_fade_rsi_sell=65.0,
    td_fade_rsi_buy=32.0,
    td_fade_sl_atr=0.9,
    td_fade_adx_max=65.0,
    td_fade_ema_tol=1.03,
    td_fade_min_adx=20.0,
    td_fade_bb_pos_sell=0.98,
    td_fade_bb_pos_buy=0.02,
)
_SPEC._rs_rng_min = 0.005
_SPEC._rs_rng_max = 0.25

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