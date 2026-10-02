"""
core/config_center.py — SINGLE SOURCE OF TRUTH for ALL trading config.

REV 4.4 (2026-10-02) — ENV OVERRIDE ROBUSTNESS:
  ✅ `_apply_env_overrides()` ab unknown `CC_*` keys pe WARNING
     print karta hai (pehle silently skip hota tha — typos pakadna
     mushkil tha).
  ✅ `.env MIN_CONFIDENCE` ab config_center khud padhta hai
     (import order independent). Pehle ye sync sirf config.py ke
     import pe hota tha — agar koi directly config_center import
     kare to 60 default reh jata tha.
     Priority: CC_MIN_CONFIDENCE > .env MIN_CONFIDENCE > default 60.
  ✅ `load_dotenv()` ab config_center ke top pe bhi call hota hai —
     standalone import mein bhi .env load hoti hai.

REV 4.3 (2026-10-02) — PHASE 3 CLEANUP:
  ✅ Added per-family FILTERS keys: st_rsi_sell_floor,
     rsi_buy_overbought, rsi_sell_oversold. Yeh pehle sirf FamilySpec
     mein thin (family files). Ab config_center single source.
     FamilySpec values fallback ke taur par rehti hain.

REV 4.2 (2026-10-02) — PHASE 2:
  ✅ Added GLOBAL["min_confidence"] default (60). config.py syncs
     CONFIG.min_confidence here at import time — .env MIN_CONFIDENCE
     still works. signals/base._mk() reads from here.

REV 4.1 (2026-10-02) — PHASE 1 CLEANUP (dead keys removed):
  ✅ Removed GLOBAL["adx_counter_trend_hard_max"] — duplicate of
     DECISION["adx_counter_trend_hard_max"]. decision_engine.py
     sirf DECISION wala padhta hai, GLOBAL wala 100% dead tha.
  ✅ Removed GLOBAL["atr_ratio_min"] / ["atr_ratio_max"] — never read
     anywhere. decision_engine uses DECISION["atr_ratio_extreme"].
  ✅ Removed GLOBAL["cvd_z_min"] — never read. Only cvd_slope + cvd_div
     are used in _filter_volume_confirmation.
  ✅ Removed GLOBAL["min_adx"] — dead. future.py uses CONFIG.min_adx
     (from .env), strategy code uses FAMILY[*]["FILTERS"]["min_adx_st"].

REV 4.0 (2026-10-02) — DECISION ENGINE CENTRALIZATION:
  ✅ Added `DECISION` dict — decision_engine.py ki saari hardcoded
     values ab yahan hain (min_approvals, btc_bias_mode, adx cutoffs,
     loss_streak, wr_mult bounds).
  ✅ Added `get_decision_cfg()` helper — parsed copy return karta hai
     (high_risk_coins frozenset ban jata hai).
  ✅ `_apply_env_overrides()` extended — ab dono handle karta hai:
       • CC_<key>       → GLOBAL overrides
       • CC_DEC_<key>   → DECISION overrides
     e.g., CC_DEC_MIN_APPROVALS=4 .env mein set karein.

REV 3.2 (2026-10-02) — INDICATORS MIGRATION (Option B):
  ✅ GLOBAL now contains ALL keys previously in indicators._BASE_CFG
     (tp1_qty, cooldown_hours, min_dist_pct/max_dist_pct, atr_ratio_*,
     be_factor, lock1_pct, lock2_pct, min_adx, cvd_z_min,
     funding_extreme, min_rr).
  ✅ GLOBAL sl_atr/tp1_atr/tp2_atr aligned to indicators.py values
     (1.8 / 2.5 / 5.0) — was 2.5 / 3.75 / 7.0.
  ✅ REGIME values aligned to indicators.py (VOLATILE 1.4/1.3,
     QUIET 0.9, CHOP 1.1/1.0, UNKNOWN 1.0). Added rsi_period
     (14 default, 21 for CHOP).
  ✅ get_config() gains `include_family: bool = True` param so
     indicators.get_trading_config() can request a family-agnostic
     flat dict (backwards-compat shape).

REV 3.1 (2026-10-02) — VOL CLASS R-THRESHOLDS.
REV 3.0 (2026-10-02) — UNIFIED CONFIG.
"""
from __future__ import annotations

import os
from copy import deepcopy

# ── REV 4.4 — Load .env at import time ──
# Ensures _apply_env_overrides() can read .env MIN_CONFIDENCE even
# when config.py hasn't been imported yet (e.g., direct
# `from core.config_center import ...`). config.py also calls
# load_dotenv(); duplicate call is a no-op (python-dotenv caches).
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════
#  VOL CLASS CAP MULTIPLIERS
# ═══════════════════════════════════════════════════════════════
VOL_CLASS_CAP_MULT: dict[str, float] = {
    "LOW":  0.80,
    "MED":  1.00,
    "HIGH": 1.40,
}


# ═══════════════════════════════════════════════════════════════
#  VOL CLASS R-MULTIPLE THRESHOLDS
# ═══════════════════════════════════════════════════════════════
VOL_CLASS_R_THRESHOLDS: dict[str, dict[str, float]] = {
    "HIGH": {
        "be_r": 0.80, "be_stop_r": 0.20,
        "lock1_r": 1.20, "lock1_stop_r": 0.55,
        "lock2_r": 1.80, "lock2_stop_r": 0.95,
        "max_hold_bars": 26,
    },
    "MED": {
        "be_r": 0.75, "be_stop_r": 0.15,
        "lock1_r": 1.10, "lock1_stop_r": 0.55,
        "lock2_r": 1.70, "lock2_stop_r": 0.90,
        "max_hold_bars": 28,
    },
    "LOW": {
        "be_r": 0.70, "be_stop_r": 0.15,
        "lock1_r": 1.00, "lock1_stop_r": 0.50,
        "lock2_r": 1.60, "lock2_stop_r": 0.85,
        "max_hold_bars": 30,
    },
}


# ═══════════════════════════════════════════════════════════════
#  RR FLOORS PER STRATEGY
# ═══════════════════════════════════════════════════════════════
MIN_RR: dict[str, float] = {
    "SUPERTREND_RIDE":  1.5,
    "TREND_DOWN_FADE":  1.5,
    "MEAN_REVERSION":   1.8,
    "RANGE_SCALPER":    1.8,
}


# ═══════════════════════════════════════════════════════════════
#  COUNTER-TREND STRATEGIES
# ═══════════════════════════════════════════════════════════════
COUNTER_TREND_STRATEGIES: frozenset = frozenset({
    "TREND_DOWN_FADE",
    "MEAN_REVERSION",
    "RANGE_SCALPER",
    "CHOP_FADE",
})


# ═══════════════════════════════════════════════════════════════
#  DECISION ENGINE CONFIG (REV 4.0 — centralized)
#  Yeh sab values pehle signals/decision_engine.py mein hardcoded
#  thin. Ab yahan hain — tuning ke liye sirf yahan ya .env change.
#
#  Env override: CC_DEC_<KEY> (e.g., CC_DEC_MIN_APPROVALS=4)
# ═══════════════════════════════════════════════════════════════
DECISION: dict = {
    # ── Approval threshold ──
    # 5 = strict (kam trades), 4 = relaxed (zyada trades), 3 = aggressive
    "min_approvals":              5,
    "total_filters":              6,

    # ── BTC bias gate ──
    # "full"           = har TREND_UP mein SHORT block,
    #                    TREND_DOWN mein LONG block
    # "high_risk_only" = sirf high-risk meme coins block (RECOMMENDED)
    # "disabled"       = gate band, counter-trend allowed
    "btc_bias_enabled":           True,
    "btc_bias_mode":              "full",
    "btc_bias_high_risk_coins":   (
        "1000BONKUSDT,1000PEPEUSDT,1000SHIBUSDT,1000FLOKIUSDT,"
        "WIFUSDT,TRUMPUSDT,PENGUUSDT,BOMEUSDT,PUMPBTCUSDT"
    ),
    "btc_regime_ttl_sec":         600,

    # ── Counter-trend ADX thresholds ──
    "adx_counter_trend_hard_max": 45.0,
    "adx_counter_trend_soft_max": 35.0,

    # ── Volatility filter ──
    "atr_ratio_extreme":          2.5,

    # ── Loss streak guard ──
    "loss_streak_trigger":        4,
    "loss_streak_cooldown_min":   60,

    # ── Win-rate confidence multiplier ──
    "wr_mult_min":                0.85,
    "wr_mult_max":                1.10,
    "wr_mult_min_trades":         15,
}


# ═══════════════════════════════════════════════════════════════
#  GLOBAL DEFAULTS
#  REV 3.2 — now includes everything previously in
#            market/indicators.py `_BASE_CFG`.
#  REV 4.1 — dead keys removed (adx_counter_trend_hard_max,
#            atr_ratio_min/max, cvd_z_min, min_adx).
#  REV 4.2 — added min_confidence (single source, .env synced).
# ═══════════════════════════════════════════════════════════════
GLOBAL: dict = {
    # ── Supertrend / ATR distances (fallback; family overrides) ──
    "sl_atr":                  1.8,     # REV 3.2 — was 2.5
    "tp1_atr":                 2.5,     # REV 3.2 — was 3.75
    "tp2_atr":                 5.0,     # REV 3.2 — was 7.0

    # ── Sizing / partials ──
    "tp1_qty":                 0.75,    # REV 3.2 — from indicators._BASE_CFG
    "cooldown_hours":          12,      # REV 3.2 — from indicators._BASE_CFG

    # ── Min RR (scalar fallback — see MIN_RR dict for per-strategy) ──
    "min_rr":                  1.5,     # REV 3.2

    # ── Confidence floor ──
    # REV 4.2 — single source for signals/base._mk().
    # REV 4.4 — .env MIN_CONFIDENCE read directly here (import-order
    #           independent). config.py also syncs CONFIG.min_confidence
    #           here at import time for backward compat.
    # Priority: CC_MIN_CONFIDENCE > .env MIN_CONFIDENCE > default 60.
    "min_confidence":          60,

    # ── Entry guards ──
    "signal_drift_pct":        0.5,
    "min_sl_pct":              0.0075,
    "min_dist_atr":            0.4,
    "max_dist_atr":            1.4,
    "pullback_dist_atr":       0.8,

    # ── Distance filters (percent-based) ──
    "min_dist_pct":            0.0025,  # REV 3.2
    "max_dist_pct":            0.050,   # REV 3.2

    # ── Late-entry guard ──
    "late_guard_adx":          35.0,
    "late_guard_rsi":          58.0,
    "late_guard_dist":         1.0,

    # ── Top-chase guard ──
    "top_chase_rsi":           68.0,
    "top_chase_flips":         4,

    # ── Extended-move guard ──
    "max_move_20bar_pct":      0.10,
    "rsi_1h_extreme_buy":      72.0,
    "rsi_1h_extreme_sell":     28.0,
    "rsi_4h_extreme_buy":      72.0,
    "rsi_4h_extreme_sell":     28.0,
    "near_120high_tol":        0.997,
    "near_120low_tol":         1.003,

    # ── Exhausted filter ──
    "exhausted_rsi_buy":       78.0,
    "exhausted_rsi_sell":      22.0,
    "exhausted_dist_mult":     1.5,

    # ── Hold time (fallback; regime + strategy override) ──
    "hold_minutes":            180,

    # ── R-multiple thresholds ──
    "be_r_min":                0.75,
    "be_stop_r":               0.15,
    "lock1_r_min":             1.10,
    "lock1_stop_r":            0.55,
    "lock2_r_min":             1.70,
    "lock2_stop_r":            0.90,
    "partial_stop_r":          0.30,

    # ── Legacy % SL fallback (used when risk_unit missing) ──
    "be_factor":               0.6,     # REV 3.2
    "lock1_pct":               0.9,     # REV 3.2
    "lock2_pct":               2.2,     # REV 3.2

    # ── Trailing SL ──
    "trail_distance_r":        0.50,
    "trail_hysteresis_r":      0.10,
    "trail_min_level":         1,

    # ── Supertrend flips (fallback) ──
    "min_flips":               3,
    "max_flips":               10,

    # ── RSI bands (fallback) ──
    "rsi_buy_min":             34.0,
    "rsi_buy_max":             72.0,
    "rsi_sell_min":            28.0,
    "rsi_sell_max":            66.0,

    # ── Counter-trend ADX cutoff ──
    # NOTE: `adx_counter_trend_hard_max` was removed in REV 4.1 —
    #       it lives ONLY in DECISION dict now (decision_engine reads
    #       from there). This GLOBAL key was 100% dead.
    "counter_trend_adx_cutoff": 35.0,

    # ── Late guard for TD_FADE ──
    "td_fade_rsi_sell":        65.0,
    "td_fade_rsi_buy":         32.0,
    "td_fade_sl_atr":          0.9,
    "td_fade_adx_max":         55.0,
    "td_fade_ema_tol":         1.02,
    "td_fade_min_adx":         20.0,

    # ── Sentiment cutoffs ──
    # NOTE: `cvd_z_min` was removed in REV 4.1 — never read anywhere.
    "funding_extreme":         0.0008,  # REV 3.2
}


# ═══════════════════════════════════════════════════════════════
#  REGIME OVERRIDES
#  REV 3.2 — sl_mult/tp_mult/rsi_period aligned to
#            market/indicators.py `_REGIME_CFG` (source of truth
#            for the live Supertrend/ATR path).
# ═══════════════════════════════════════════════════════════════
REGIME: dict = {
    "TREND_UP": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "rsi_period":      14,      # REV 3.2
        "hold_minutes":    300,
        "late_guard_adx":  38.0,
    },
    "TREND_DOWN": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "rsi_period":      14,      # REV 3.2
        "hold_minutes":    300,
        "late_guard_adx":  38.0,
    },
    "VOLATILE": {
        "sl_mult":         1.4,     # REV 3.2 — was 1.3 (match indicators.py)
        "tp_mult":         1.3,     # REV 3.2 — was 0.70
        "rsi_period":      14,      # REV 3.2
        "hold_minutes":    120,
        "late_guard_adx":  35.0,
    },
    "QUIET": {
        "sl_mult":         0.7,
        "tp_mult":         0.9,     # REV 3.2 — was 0.60
        "rsi_period":      14,      # REV 3.2
        "hold_minutes":    180,
    },
    "CHOP": {
        "sl_mult":         1.1,     # REV 3.2 — was 1.0
        "tp_mult":         1.0,     # REV 3.2 — was 0.45
        "rsi_period":      21,      # REV 3.2 — CHOP uses RSI 21
        "hold_minutes":    90,
        "late_guard_adx":  32.0,
    },
    "UNKNOWN": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,     # REV 3.2 — was 0.55
        "rsi_period":      14,      # REV 3.2
        "hold_minutes":    120,
    },
}


# ═══════════════════════════════════════════════════════════════
#  STRATEGY × REGIME OVERRIDES
# ═══════════════════════════════════════════════════════════════
STRATEGY_REGIME: dict = {
    "SUPERTREND_RIDE": {
        "TREND_UP":    {"hold_minutes": 300, "tp1_atr": 2.8},
        "TREND_DOWN":  {"hold_minutes": 300, "tp1_atr": 2.8},
        "VOLATILE":    {"hold_minutes": 120, "tp1_atr": 1.8},
        "CHOP":        {"hold_minutes": 90,  "tp1_atr": 1.2},
    },
    "TREND_DOWN_FADE": {
        "TREND_DOWN":  {"hold_minutes": 180, "sl_atr": 1.8},
        "CHOP":        {"hold_minutes": 90,  "sl_atr": 1.5},
    },
    "MEAN_REVERSION": {
        "CHOP":        {"hold_minutes": 120, "tp1_atr": 1.5},
        "QUIET":       {"hold_minutes": 180, "tp1_atr": 1.8},
    },
    "RANGE_SCALPER": {
        "CHOP":        {"hold_minutes": 90,  "sl_atr": 1.5},
        "QUIET":       {"hold_minutes": 120, "sl_atr": 1.5},
        "VOLATILE":    {"hold_minutes": 60,  "sl_atr": 1.8},
    },
}


# ═══════════════════════════════════════════════════════════════
#  FAMILY BASELINES
#  REV 4.3 — added per-family FILTERS keys:
#            st_rsi_sell_floor, rsi_buy_overbought, rsi_sell_oversold
#            (previously only in FamilySpec files).
# ═══════════════════════════════════════════════════════════════
FAMILY: dict = {
    "momentum_coins": {
        "ST_PARAMS": {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 7.5},
        "CAPS": {"sl": 0.060, "tp1": 0.090, "tp2": 0.150},
        "FILTERS": {
            "max_dist_atr": 1.4, "pullback_dist_atr": 0.8, "min_dist_atr": 0.4,
            "min_adx_st": 22.0, "min_flips": 2, "max_flips": 10,
            "late_guard_adx": 35.0, "late_guard_rsi": 58.0, "late_guard_dist": 1.0,
            "top_chase_rsi": 68.0, "top_chase_flips": 4,
            "rsi_buy_min": 36.0, "rsi_buy_max": 74.0,
            "rsi_sell_min": 26.0, "rsi_sell_max": 66.0,
            "st_rsi_sell_floor": 25.0,
            "rsi_buy_overbought": 86.0,
            "rsi_sell_oversold": 20.0,
        },
    },
    "volatility_coins": {
        "ST_PARAMS": {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 7.5},
        "CAPS": {"sl": 0.065, "tp1": 0.0975, "tp2": 0.1625},
        "FILTERS": {
            "max_dist_atr": 1.4, "pullback_dist_atr": 0.8, "min_dist_atr": 0.6,
            "min_adx_st": 22.0, "min_flips": 3, "max_flips": 12,
            "late_guard_adx": 35.0, "late_guard_rsi": 58.0, "late_guard_dist": 1.0,
            "top_chase_rsi": 68.0, "top_chase_flips": 4,
            "rsi_buy_min": 34.0, "rsi_buy_max": 72.0,
            "rsi_sell_min": 28.0, "rsi_sell_max": 66.0,
            "st_rsi_sell_floor": 25.0,
            "rsi_buy_overbought": 84.0,
            "rsi_sell_oversold": 22.0,
        },
    },
    "trend_coins": {
        "ST_PARAMS": {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 7.0},
        "CAPS": {"sl": 0.045, "tp1": 0.0675, "tp2": 0.125},
        "FILTERS": {
            "max_dist_atr": 1.4, "pullback_dist_atr": 0.8, "min_dist_atr": 0.5,
            "min_adx_st": 22.0, "min_flips": 3, "max_flips": 15,
            "late_guard_adx": 35.0, "late_guard_rsi": 58.0, "late_guard_dist": 1.0,
            "top_chase_rsi": 68.0, "top_chase_flips": 4,
            "rsi_buy_min": 34.0, "rsi_buy_max": 70.0,
            "rsi_sell_min": 30.0, "rsi_sell_max": 65.0,
            "st_rsi_sell_floor": 28.0,
            "rsi_buy_overbought": 82.0,
            "rsi_sell_oversold": 25.0,
        },
    },
    "range_coins": {
        "ST_PARAMS": {"sl_atr": 1.8, "tp1_atr": 2.7, "tp2_atr": 4.5},
        "CAPS": {"sl": 0.040, "tp1": 0.060, "tp2": 0.100},
        "FILTERS": {
            "max_dist_atr": 1.4, "pullback_dist_atr": 0.8, "min_dist_atr": 0.2,
            "min_adx_st": 24.0, "min_flips": 4, "max_flips": 10,
            "late_guard_adx": 35.0, "late_guard_rsi": 58.0, "late_guard_dist": 1.0,
            "top_chase_rsi": 68.0, "top_chase_flips": 4,
            "rsi_buy_min": 40.0, "rsi_buy_max": 68.0,
            "rsi_sell_min": 33.0, "rsi_sell_max": 60.0,
            "st_rsi_sell_floor": 30.0,
            "rsi_buy_overbought": 78.0,
            "rsi_sell_oversold": 22.0,
        },
    },
}

DEFAULT_FAMILY = "trend_coins"


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════
def get_baseline(family: str) -> dict:
    return FAMILY.get(family, FAMILY[DEFAULT_FAMILY])


def get_regime_cfg(regime: str) -> dict:
    return REGIME.get(regime or "UNKNOWN", REGIME["UNKNOWN"])


def get_min_rr(strategy: str) -> float:
    return float(MIN_RR.get(strategy, 1.5))


def get_decision_cfg() -> dict:
    """
    REV 4.0 — Decision engine config accessor.

    Returns DECISION dict (copy) with high_risk_coins parsed as frozenset.
    Called by signals/decision_engine.py at import time.

    Usage:
        from core.config_center import get_decision_cfg
        cfg = get_decision_cfg()
        min_appr = cfg["min_approvals"]
    """
    out = dict(DECISION)
    raw = out.get("btc_bias_high_risk_coins", "")
    if isinstance(raw, str):
        out["btc_bias_high_risk_coins"] = frozenset(
            c.strip().upper() for c in raw.split(",") if c.strip()
        )
    elif isinstance(raw, (list, tuple, set, frozenset)):
        out["btc_bias_high_risk_coins"] = frozenset(
            str(c).strip().upper() for c in raw if c
        )
    return out


def get_vol_class_mult(vol_class: str) -> float:
    return float(VOL_CLASS_CAP_MULT.get(vol_class, 1.0))


def get_r_thresholds(vol_class: str) -> dict:
    return dict(VOL_CLASS_R_THRESHOLDS.get(vol_class,
                                           VOL_CLASS_R_THRESHOLDS["MED"]))


def get_config(symbol: str = "", strategy: str = "",
               regime: str = "UNKNOWN",
               vol_class: str = "",
               include_family: bool = True) -> dict:
    """
    Resolve effective config for (symbol, strategy, regime).

    Hierarchy (later layers override earlier):
      1. GLOBAL
      2. REGIME[regime]
      3. STRATEGY_REGIME[strategy][regime]
      4. FAMILY[family]        (only if include_family=True)
      5. COIN identity          (only if include_family=True)

    REV 3.2 — `include_family=False` skips family/coin merge, returning
    a flat GLOBAL+REGIME dict. This is what market/indicators.
    get_trading_config() uses, to preserve its legacy shape.
    """
    merged = deepcopy(GLOBAL)

    # Layer 2 — regime
    merged.update(get_regime_cfg(regime))

    # Layer 3 — strategy × regime
    if strategy:
        sr = STRATEGY_REGIME.get(strategy, {}).get(regime, {})
        merged.update(sr)

    family = DEFAULT_FAMILY
    coin_vol_class = vol_class or "MED"

    if include_family:
        # Layer 4/5 — family + coin
        if symbol:
            try:
                from core.coins_config import get_family, get_coin_vol_class
                family = get_family(symbol) or DEFAULT_FAMILY
                coin_vol_class = vol_class or get_coin_vol_class(symbol) or "MED"
            except Exception:
                pass

        baseline = get_baseline(family)
        merged.update(baseline.get("ST_PARAMS", {}))
        merged.update(baseline.get("FILTERS", {}))
        merged["CAPS"] = dict(baseline.get("CAPS", {}))
    else:
        # Skip family/coin merge — flat GLOBAL + REGIME output
        # (used by indicators.get_trading_config for shape compat)
        merged["CAPS"] = {}

    merged["_meta"] = {
        "symbol": symbol, "strategy": strategy, "regime": regime,
        "family": family, "vol_class": coin_vol_class,
        "include_family": include_family,
    }
    return merged


def describe(symbol: str, strategy: str, regime: str,
             vol_class: str = "") -> str:
    cfg = get_config(symbol, strategy, regime, vol_class)
    meta = cfg.pop("_meta", {})
    lines = [
        "=" * 70,
        f"  CONFIG: {meta.get('symbol','?')} "
        f"[{meta.get('strategy','?')} @ {meta.get('regime','?')}]",
        f"  family={meta.get('family','?')} vol_class={meta.get('vol_class','?')}",
        "=" * 70,
    ]
    for k in sorted(cfg.keys()):
        v = cfg[k]
        if isinstance(v, (int, float, str, bool)):
            lines.append(f"  {k:<28} = {v}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
#  ENV OVERRIDES
#  REV 4.0 — now handles BOTH GLOBAL and DECISION:
#    • CC_<key>=value       → GLOBAL overrides
#    • CC_DEC_<key>=value   → DECISION overrides
#  REV 4.4 — unknown CC_* keys now log a WARNING (was silent skip).
#    Also reads .env MIN_CONFIDENCE directly (import-order independent).
# ═══════════════════════════════════════════════════════════════
def _apply_env_overrides() -> None:
    for env_key, raw_val in os.environ.items():
        if not env_key.startswith("CC_"):
            continue

        # ── Pick target dict ──
        if env_key.startswith("CC_DEC_"):
            cfg_key = env_key[7:].lower()
            target = DECISION
        else:
            cfg_key = env_key[3:].lower()
            target = GLOBAL

        if cfg_key not in target:
            # REV 4.4 — surface silent skip so typos don't go unnoticed
            print(f"[config_center] ⚠️  env override IGNORED: "
                  f"{env_key}={raw_val} — key {cfg_key!r} not in "
                  f"{'DECISION' if target is DECISION else 'GLOBAL'}")
            continue

        try:
            original = target[cfg_key]
            if isinstance(original, bool):
                target[cfg_key] = raw_val.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(original, int):
                target[cfg_key] = int(raw_val)
            elif isinstance(original, float):
                target[cfg_key] = float(raw_val)
            else:
                target[cfg_key] = raw_val
            print(f"[config_center] env override: {env_key} = {target[cfg_key]}")
        except Exception as e:
            print(f"[config_center] env override failed {env_key}={raw_val}: {e}")

    # ── REV 4.4 — MIN_CONFIDENCE (no CC_ prefix) sync from .env ──
    # Fallback for when config.py hasn't imported config_center yet
    # (e.g., direct `python -c "from core.config_center import ..."`).
    # Priority: CC_MIN_CONFIDENCE > .env MIN_CONFIDENCE > default 60.
    #
    # If CC_MIN_CONFIDENCE is set, the loop above already handled it
    # (via CC_ prefix) — skip the .env fallback to avoid double-logging.
    if "CC_MIN_CONFIDENCE" not in os.environ:
        _raw = os.environ.get("MIN_CONFIDENCE")
        if _raw:
            try:
                GLOBAL["min_confidence"] = float(_raw.strip())
                print(f"[config_center] env override: MIN_CONFIDENCE = "
                      f"{GLOBAL['min_confidence']}")
            except (ValueError, TypeError):
                pass


_apply_env_overrides()


if __name__ == "__main__":
    print("=" * 70)
    print("  CONFIG CENTER DIAGNOSTIC — REV 4.4 (unified + decision)")
    print("=" * 70)
    samples = [
        ("POLUSDT",  "SUPERTREND_RIDE",  "TREND_DOWN"),
        ("APTUSDT",  "SUPERTREND_RIDE",  "TREND_UP"),
        ("BTCUSDT",  "SUPERTREND_RIDE",  "TREND_UP"),
        ("HYPEUSDT", "SUPERTREND_RIDE",  "VOLATILE"),
        ("XRPUSDT",  "RANGE_SCALPER",    "CHOP"),
    ]
    for sym, strat, reg in samples:
        print()
        print(describe(sym, strat, reg))
    print()
    print("=" * 70)
    print("  Flat get_config(include_family=False) test:")
    flat = get_config(include_family=False)
    flat.pop("_meta", None)
    print(f"  Keys: {len(flat)}")
    print(f"  sl_atr={flat['sl_atr']} tp1_atr={flat['tp1_atr']} tp2_atr={flat['tp2_atr']}")
    print(f"  min_rr={flat['min_rr']} tp1_qty={flat['tp1_qty']}")
    print(f"  min_confidence={flat.get('min_confidence')}")
    print(f"  sl_mult={flat['sl_mult']} tp_mult={flat['tp_mult']} rsi_period={flat['rsi_period']}")
    print()
    print("=" * 70)
    print("  FAMILY FILTERS new keys (REV 4.3):")
    for _fam in ("momentum_coins", "volatility_coins", "trend_coins", "range_coins"):
        _f = FAMILY[_fam]["FILTERS"]
        print(f"  {_fam:20} "
              f"floor={_f.get('st_rsi_sell_floor')} "
              f"ob={_f.get('rsi_buy_overbought')} "
              f"os={_f.get('rsi_sell_oversold')}")
    print()
    print("=" * 70)
    print("  DECISION config test (REV 4.0):")
    dec = get_decision_cfg()
    print(f"  min_approvals              = {dec['min_approvals']}")
    print(f"  total_filters              = {dec['total_filters']}")
    print(f"  btc_bias_enabled           = {dec['btc_bias_enabled']}")
    print(f"  btc_bias_mode              = {dec['btc_bias_mode']}")
    print(f"  btc_bias_high_risk_coins   = {len(dec['btc_bias_high_risk_coins'])} coins")
    print(f"    → {sorted(dec['btc_bias_high_risk_coins'])[:3]} ...")
    print(f"  btc_regime_ttl_sec         = {dec['btc_regime_ttl_sec']}")
    print(f"  adx_counter_trend_hard_max = {dec['adx_counter_trend_hard_max']}")
    print(f"  adx_counter_trend_soft_max = {dec['adx_counter_trend_soft_max']}")
    print(f"  atr_ratio_extreme          = {dec['atr_ratio_extreme']}")
    print(f"  loss_streak_trigger        = {dec['loss_streak_trigger']}")
    print(f"  loss_streak_cooldown_min   = {dec['loss_streak_cooldown_min']}")
    print(f"  wr_mult_min                = {dec['wr_mult_min']}")
    print(f"  wr_mult_max                = {dec['wr_mult_max']}")
    print(f"  wr_mult_min_trades         = {dec['wr_mult_min_trades']}")
    print("=" * 70)