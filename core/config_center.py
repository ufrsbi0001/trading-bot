"""
core/config_center.py — SINGLE SOURCE OF TRUTH for ALL trading config.

REV 5.5 (2026-10-02) — REDUNDANT FILTER SHADOW CLEANUP:
  ✅ Removed 7 keys from FAMILY[*].FILTERS (all 4 families) that were
     identical to their GLOBAL counterparts — pure redundant shadows:
       • late_guard_adx      (35.0)
       • late_guard_dist     (1.0)
       • late_guard_rsi      (58.0)
       • max_dist_atr        (1.4)
       • pullback_dist_atr   (0.8)
       • top_chase_flips     (4)
       • top_chase_rsi       (68.0)
     Behaviour: ZERO change. get_config() deep-copies GLOBAL, then
     overlays FAMILY.FILTERS. Absent keys retain GLOBAL values.
     Benefit: -28 lines of duplicate config, single tune point.
     Verified via scripts/audit_config_overlaps.py REV 5.4.

REV 5.3 (2026-10-02) — ENTRY/EXIT CONSTANTS PROMOTED:
  ✅ Added 9 GLOBAL keys previously hardcoded in orders/entry.py and
     orders/exit.py (margin_buffer_pct, counter_trend_size_mult,
     min_notional_bump_mult, corrected_qty_close_ratio, sl_tight_ratio,
     sl_wide_ratio, risk_oversize_warn_mult, rr_collapse_tol, tiny_loss_r).
  ✅ Added VOL_CLASS_QTY_MULT dict (position-size scaling).

REV 5.2 (2026-10-02) — TRAILING SL KEYS RE-ADDED.

REV 5.1 (2026-10-02) — DEAD KEY CLEANUP (8 keys).

REV 5.0 (2026-10-02) — CONFIG UNIFICATION.
REV 4.5 (2026-10-02) — TIME_EXIT TOGGLE.
REV 4.4 (2026-10-02) — ENV OVERRIDE ROBUSTNESS.
REV 4.3 (2026-10-02) — PHASE 3 CLEANUP.
REV 4.2 (2026-10-02) — PHASE 2: GLOBAL["min_confidence"] default.
REV 4.1 (2026-10-02) — PHASE 1 CLEANUP (dead keys removed).
REV 4.0 (2026-10-02) — DECISION ENGINE CENTRALIZATION.
REV 3.2 (2026-10-02) — INDICATORS MIGRATION (Option B).
REV 3.1 (2026-10-02) — VOL CLASS R-THRESHOLDS.
REV 3.0 (2026-10-02) — UNIFIED CONFIG.
"""
from __future__ import annotations

import os
from copy import deepcopy

# ── Load .env at import time ──
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════
#  VOL CLASS CAP MULTIPLIERS  (scales SL/TP distance caps)
# ═══════════════════════════════════════════════════════════════
VOL_CLASS_CAP_MULT: dict[str, float] = {
    "LOW":  0.80,
    "MED":  1.00,
    "HIGH": 1.40,
}


# ═══════════════════════════════════════════════════════════════
#  VOL CLASS QTY MULTIPLIERS  (scales position size)
#  NOTE: distinct from VOL_CLASS_CAP_MULT which scales SL/TP
#        distance caps. This one scales the entry qty.
# ═══════════════════════════════════════════════════════════════
VOL_CLASS_QTY_MULT: dict[str, float] = {
    "LOW":  1.00,
    "MED":  0.70,
    "HIGH": 0.40,
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
#  DECISION ENGINE CONFIG
#  Env override: CC_DEC_<KEY> (e.g., CC_DEC_MIN_APPROVALS=4)
# ═══════════════════════════════════════════════════════════════
DECISION: dict = {
    # ── Approval threshold ──
    "min_approvals":              5,
    "total_filters":              6,

    # ── BTC bias gate ──
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
#  REV 5.0 — includes ALL trading params migrated from
#            core/config.py Config dataclass.
#  REV 5.1 — 8 dead keys removed.
#  REV 5.2 — 3 trailing SL keys re-added.
#  REV 5.3 — 9 entry/exit constants promoted.
# ═══════════════════════════════════════════════════════════════
GLOBAL: dict = {
    # ═══════════════════════════════════════════════════════════
    #  MIGRATED FROM core/config.py (REV 5.0)
    #  Env override: CC_<KEY> (preferred) or legacy .env name
    #  (see _LEGACY_ENV_MAP at bottom of file).
    # ═══════════════════════════════════════════════════════════
    # ── Risk / Position Sizing ──
    "leverage":                   5,
    "risk_percent":               0.5,
    "max_open_positions":         3,
    "max_same_side_positions":    2,
    "max_total_margin_pct":       0.60,
    "max_daily_loss_trades":      3,
    "max_daily_drawdown_percent": 4.0,
    "max_account_drawdown":       10.0,
    "cooldown_after_sl_min":      20,
    "cooldown_after_tp_min":      15,
    "partial_close_usdt":         15.0,

    # ── Signal filters ──
    "min_adx":                    22,
    "require_htf_agreement":      True,
    "use_5m_trend_filter":        True,
    "fg_enabled":                 False,
    "max_trades_per_coin_per_day": 2,
    "htf_align_relaxed":          False,

    # ── Modern indicator flags ──
    "use_taker_volume":           False,
    "use_volume_profile":         False,
    "use_anchored_vwap":          False,
    "use_funding_z":              False,
    "use_mtf_confluence":         False,

    # ── Killzone / Spread filter ──
    "kz_bypass":                  False,
    "use_spread_filter":          True,
    "max_spread_pct":             0.15,
    "max_spread_trend":           0.08,
    "max_spread_range":           0.12,
    "max_spread_volatility":      0.25,

    # ═══════════════════════════════════════════════════════════
    #  EXISTING KEYS (pre-REV 5.0)
    # ═══════════════════════════════════════════════════════════
    # ── Supertrend / ATR distances (fallback; family overrides) ──
    "sl_atr":                  1.8,
    "tp1_atr":                 2.5,
    "tp2_atr":                 5.0,

    # ── Sizing / partials ──
    "tp1_qty":                 0.75,

    # ── Min RR (scalar fallback — see MIN_RR dict for per-strategy) ──
    "min_rr":                  1.5,

    # ── Confidence floor ──
    # Priority: CC_MIN_CONFIDENCE > .env MIN_CONFIDENCE > default 60.
    "min_confidence":          60,

    # ── Entry guards ──
    "signal_drift_pct":        0.5,
    "min_sl_pct":              0.0075,
    "min_dist_atr":            0.4,
    "max_dist_atr":            1.4,
    "pullback_dist_atr":       0.8,

    # ── Distance filters (percent-based) ──
    "min_dist_pct":            0.0025,

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

    # ── TIME_EXIT TOGGLE (REV 4.5) ──
    "time_exit_enabled":       False,

    # ── R-multiple thresholds ──
    "be_stop_r":               0.15,
    "lock1_stop_r":            0.55,
    "lock2_stop_r":            0.90,
    "partial_stop_r":          0.30,

    # ── Legacy % SL fallback ──
    "be_factor":               0.6,
    "lock1_pct":               0.9,
    "lock2_pct":               2.2,

    # ── Trailing SL (consumed by orders/manage.py) ──
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
    "counter_trend_adx_cutoff": 35.0,

    # ── Late guard for TD_FADE ──
    "td_fade_rsi_sell":        65.0,
    "td_fade_rsi_buy":         32.0,
    "td_fade_sl_atr":          0.9,
    "td_fade_adx_max":         55.0,
    "td_fade_ema_tol":         1.02,
    "td_fade_min_adx":         20.0,

    # ── Sentiment cutoffs ──
    "funding_extreme":         0.0008,

    # ═══════════════════════════════════════════════════════════
    #  ENTRY/EXIT SAFETY & SIZING CONSTANTS (REV 5.3)
    # ═══════════════════════════════════════════════════════════
    # ── Margin / position sizing ──
    "margin_buffer_pct":           0.80,
    "counter_trend_size_mult":     0.60,
    "min_notional_bump_mult":      1.02,
    "corrected_qty_close_ratio":   0.50,

    # ── SL / TP sanity ratios ──
    "sl_tight_ratio":              0.70,
    "sl_wide_ratio":               1.05,
    "risk_oversize_warn_mult":     1.20,

    # ── RR / close classification ──
    "rr_collapse_tol":             0.02,
    "tiny_loss_r":                 0.30,
}


# ═══════════════════════════════════════════════════════════════
#  REGIME OVERRIDES
# ═══════════════════════════════════════════════════════════════
REGIME: dict = {
    "TREND_UP": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "rsi_period":      14,
        "hold_minutes":    300,
        "late_guard_adx":  38.0,
    },
    "TREND_DOWN": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "rsi_period":      14,
        "hold_minutes":    300,
        "late_guard_adx":  38.0,
    },
    "VOLATILE": {
        "sl_mult":         1.4,
        "tp_mult":         1.3,
        "rsi_period":      14,
        "hold_minutes":    120,
        "late_guard_adx":  35.0,
    },
    "QUIET": {
        "sl_mult":         0.7,
        "tp_mult":         0.9,
        "rsi_period":      14,
        "hold_minutes":    180,
    },
    "CHOP": {
        "sl_mult":         1.1,
        "tp_mult":         1.0,
        "rsi_period":      21,
        "hold_minutes":    90,
        "late_guard_adx":  32.0,
    },
    "UNKNOWN": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "rsi_period":      14,
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
#  REV 5.5 — Removed 7 keys from each family's FILTERS that were
#            identical to GLOBAL (redundant shadows). They inherit
#            via get_config() merge. Only genuine family-specific
#            tuning remains.
# ═══════════════════════════════════════════════════════════════
FAMILY: dict = {
    "momentum_coins": {
        "ST_PARAMS": {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 7.5},
        "CAPS": {"sl": 0.060, "tp1": 0.090, "tp2": 0.150},
        "FILTERS": {
            # family-specific tuning
            "min_adx_st": 22.0, "min_flips": 2, "max_flips": 10,
            "min_dist_atr": 0.4,
            "rsi_buy_min": 36.0, "rsi_buy_max": 74.0,
            "rsi_sell_min": 26.0, "rsi_sell_max": 66.0,
            "st_rsi_sell_floor": 25.0,
            "rsi_buy_overbought": 86.0,
            "rsi_sell_oversold": 20.0,
            # REV 5.5 — removed (inherit from GLOBAL):
            #   late_guard_adx, late_guard_rsi, late_guard_dist,
            #   max_dist_atr, pullback_dist_atr,
            #   top_chase_rsi, top_chase_flips
        },
    },
    "volatility_coins": {
        "ST_PARAMS": {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 7.5},
        "CAPS": {"sl": 0.065, "tp1": 0.0975, "tp2": 0.1625},
        "FILTERS": {
            # family-specific tuning
            "min_adx_st": 22.0, "min_flips": 3, "max_flips": 12,
            "min_dist_atr": 0.6,
            "rsi_buy_min": 34.0, "rsi_buy_max": 72.0,
            "rsi_sell_min": 28.0, "rsi_sell_max": 66.0,
            "st_rsi_sell_floor": 25.0,
            "rsi_buy_overbought": 84.0,
            "rsi_sell_oversold": 22.0,
            # REV 5.5 — removed (inherit from GLOBAL): same 7 keys
        },
    },
    "trend_coins": {
        "ST_PARAMS": {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 7.0},
        "CAPS": {"sl": 0.045, "tp1": 0.0675, "tp2": 0.125},
        "FILTERS": {
            # family-specific tuning
            "min_adx_st": 22.0, "min_flips": 3, "max_flips": 15,
            "min_dist_atr": 0.5,
            "rsi_buy_min": 34.0, "rsi_buy_max": 70.0,
            "rsi_sell_min": 30.0, "rsi_sell_max": 65.0,
            "st_rsi_sell_floor": 28.0,
            "rsi_buy_overbought": 82.0,
            "rsi_sell_oversold": 25.0,
            # REV 5.5 — removed (inherit from GLOBAL): same 7 keys
        },
    },
    "range_coins": {
        "ST_PARAMS": {"sl_atr": 1.8, "tp1_atr": 2.7, "tp2_atr": 4.5},
        "CAPS": {"sl": 0.040, "tp1": 0.060, "tp2": 0.100},
        "FILTERS": {
            # family-specific tuning
            "min_adx_st": 24.0, "min_flips": 4, "max_flips": 10,
            "min_dist_atr": 0.2,
            "rsi_buy_min": 40.0, "rsi_buy_max": 68.0,
            "rsi_sell_min": 33.0, "rsi_sell_max": 60.0,
            "st_rsi_sell_floor": 30.0,
            "rsi_buy_overbought": 78.0,
            "rsi_sell_oversold": 22.0,
            # REV 5.5 — removed (inherit from GLOBAL): same 7 keys
        },
    },
}

DEFAULT_FAMILY = "trend_coins"


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════
def get(key: str, default=None):
    """Direct scalar GLOBAL read for lightweight consumers."""
    return GLOBAL.get(key, default)


def get_baseline(family: str) -> dict:
    return FAMILY.get(family, FAMILY[DEFAULT_FAMILY])


def get_regime_cfg(regime: str) -> dict:
    return REGIME.get(regime or "UNKNOWN", REGIME["UNKNOWN"])


def get_min_rr(strategy: str) -> float:
    return float(MIN_RR.get(strategy, 1.5))


def get_decision_cfg() -> dict:
    """Returns DECISION dict (copy) with high_risk_coins parsed as frozenset."""
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


def get_vol_class_qty_mult(vol_class: str) -> float:
    """Position-size scaling per vol class (not SL/TP caps)."""
    return float(VOL_CLASS_QTY_MULT.get(vol_class, 0.70))


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

    REV 5.5 — FAMILY.FILTERS only contains genuine family-specific
    overrides. Keys not present inherit from GLOBAL (present in
    GLOBAL via deepcopy at step 1).
    """
    merged = deepcopy(GLOBAL)

    merged.update(get_regime_cfg(regime))

    if strategy:
        sr = STRATEGY_REGIME.get(strategy, {}).get(regime, {})
        merged.update(sr)

    family = DEFAULT_FAMILY
    coin_vol_class = vol_class or "MED"

    if include_family:
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
        merged["CAPS"] = {}

    merged["_meta"] = {
        "symbol": symbol, "strategy": strategy, "regime": regime,
        "family": family, "vol_class": coin_vol_class,
        "include_family": include_family,
    }
    return merged


# ═══════════════════════════════════════════════════════════════
#  RUNTIME UPDATE (UI TOGGLE SUPPORT)
# ═══════════════════════════════════════════════════════════════
def update_runtime(**kwargs) -> dict:
    """
    Runtime update of GLOBAL config keys (in-memory only).

    Sirf woh keys allow hain jo GLOBAL dict me already exist karti hain.
    Type coerce hota hai original type ke hisaab se.

    Bot restart par default pe wapas — jab tak .env me
    CC_<KEY>=value set na ho.
    """
    changes: dict = {}
    for k, v in kwargs.items():
        if k not in GLOBAL:
            print(f"[config_center] runtime update IGNORED: {k!r} "
                  f"not in GLOBAL")
            continue

        old = GLOBAL[k]
        try:
            if isinstance(old, bool) and not isinstance(v, bool):
                v = str(v).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int) and not isinstance(old, bool):
                v = int(v)
            elif isinstance(old, float):
                v = float(v)
        except (TypeError, ValueError) as e:
            print(f"[config_center] runtime update failed {k}={v!r}: {e}")
            continue

        GLOBAL[k] = v
        changes[k] = (old, v)
        print(f"[config_center] runtime update: {k} = {v} (was {old})")
    return changes


def is_time_exit_enabled() -> bool:
    """Quick helper for TIME_EXIT gate."""
    return bool(GLOBAL.get("time_exit_enabled", False))


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
#
#  Priority (highest wins):
#    1. CC_<KEY> env var            (native config_center override)
#    2. Legacy .env name            (LEVERAGE, MIN_CONFIDENCE, ...)
#    3. Default in GLOBAL / DECISION
#
#  Legacy names are honored ONLY if CC_<KEY> is not set.
#  Special case: USE_1M_TREND_FILTER is a fallback for
#  USE_5M_TREND_FILTER (REV 1.5.1 rename).
# ═══════════════════════════════════════════════════════════════
_LEGACY_ENV_MAP: dict[str, str] = {
    # GLOBAL key              → legacy .env var name
    "leverage":                    "LEVERAGE",
    "risk_percent":                "RISK_PERCENT",
    "max_open_positions":          "MAX_OPEN_POSITIONS",
    "max_same_side_positions":     "MAX_SAME_SIDE_POSITIONS",
    "max_total_margin_pct":        "MAX_TOTAL_MARGIN_PCT",
    "max_daily_loss_trades":       "MAX_DAILY_LOSS_TRADES",
    "max_daily_drawdown_percent":  "MAX_DAILY_DRAWDOWN_PERCENT",
    "max_account_drawdown":        "MAX_ACCOUNT_DRAWDOWN",
    "hold_minutes":                "MAX_HOLD_MINUTES",
    "cooldown_after_sl_min":       "COOLDOWN_AFTER_SL_MIN",
    "cooldown_after_tp_min":       "COOLDOWN_AFTER_TP_MIN",
    "partial_close_usdt":          "PARTIAL_CLOSE_USDT",
    "min_confidence":              "MIN_CONFIDENCE",
    "min_adx":                     "MIN_ADX",
    "require_htf_agreement":       "REQUIRE_HTF_AGREEMENT",
    "use_5m_trend_filter":         "USE_5M_TREND_FILTER",
    "fg_enabled":                  "FG_ENABLED",
    "max_trades_per_coin_per_day": "MAX_TRADES_PER_COIN_PER_DAY",
    "htf_align_relaxed":           "HTF_ALIGN_RELAXED",
    "use_taker_volume":            "USE_TAKER_VOLUME",
    "use_volume_profile":          "USE_VOLUME_PROFILE",
    "use_anchored_vwap":           "USE_ANCHORED_VWAP",
    "use_funding_z":               "USE_FUNDING_Z",
    "use_mtf_confluence":          "USE_MTF_CONFLUENCE",
    "kz_bypass":                   "KZ_BYPASS",
    "use_spread_filter":           "USE_SPREAD_FILTER",
    "max_spread_pct":              "MAX_SPREAD_PCT",
    "max_spread_trend":            "MAX_SPREAD_TREND",
    "max_spread_range":            "MAX_SPREAD_RANGE",
    "max_spread_volatility":       "MAX_SPREAD_VOLATILITY",
}


def _coerce_and_set(target: dict, key: str, raw_val: str, source: str) -> bool:
    """Type-coerce raw_val based on current value in target, and set it."""
    if key not in target:
        print(f"[config_center] ⚠️  env override IGNORED: "
              f"{source}={raw_val!r} — key {key!r} not in target dict")
        return False
    original = target[key]
    try:
        if isinstance(original, bool):
            target[key] = raw_val.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(original, int):
            target[key] = int(raw_val.strip())
        elif isinstance(original, float):
            target[key] = float(raw_val.strip())
        else:
            target[key] = raw_val
        print(f"[config_center] env override: {source} = {target[key]}")
        return True
    except Exception as e:
        print(f"[config_center] env override failed {source}={raw_val}: {e}")
        return False


def _apply_env_overrides() -> None:
    # ── Pass 1: CC_* overrides (highest priority) ──
    for env_key, raw_val in os.environ.items():
        if not env_key.startswith("CC_"):
            continue
        if env_key.startswith("CC_DEC_"):
            _coerce_and_set(DECISION, env_key[7:].lower(), raw_val, env_key)
        else:
            _coerce_and_set(GLOBAL, env_key[3:].lower(), raw_val, env_key)

    # ── Pass 2: legacy .env names (only if CC_<KEY> not already set) ──
    for cfg_key, legacy_env in _LEGACY_ENV_MAP.items():
        # Skip if CC_<KEY> already overrode this key
        if f"CC_{cfg_key.upper()}" in os.environ:
            continue

        raw = os.getenv(legacy_env)

        # Special case: use_5m_trend_filter honors USE_1M_TREND_FILTER
        # (REV 1.5.1 rename — backward compat).
        if (raw is None or raw.strip() == "") and cfg_key == "use_5m_trend_filter":
            raw = os.getenv("USE_1M_TREND_FILTER")

        if raw is None or raw.strip() == "":
            continue

        _coerce_and_set(GLOBAL, cfg_key, raw, legacy_env)


# ═══════════════════════════════════════════════════════════════
#  VALIDATION
# ═══════════════════════════════════════════════════════════════
def _validate() -> None:
    errors: list[str] = []
    g = GLOBAL

    if not 1 <= g["leverage"] <= 125:
        errors.append(f"leverage={g['leverage']} — must be 1–125")
    if not 0 < g["risk_percent"] <= 100:
        errors.append(f"risk_percent={g['risk_percent']} — must be >0 and ≤100")
    if g["max_open_positions"] < 1:
        errors.append(f"max_open_positions={g['max_open_positions']} — must be ≥1")
    if g["max_same_side_positions"] < 1:
        errors.append(
            f"max_same_side_positions={g['max_same_side_positions']} — must be ≥1"
        )
    if g["max_same_side_positions"] > g["max_open_positions"]:
        errors.append(
            f"max_same_side_positions={g['max_same_side_positions']} must be "
            f"≤ max_open_positions={g['max_open_positions']}"
        )
    if not 0 < g["max_total_margin_pct"] <= 1:
        errors.append(
            f"max_total_margin_pct={g['max_total_margin_pct']} — must be in (0, 1]"
        )
    if g["max_daily_loss_trades"] < 1:
        errors.append("max_daily_loss_trades must be ≥1")
    if not 0 < g["max_daily_drawdown_percent"] <= 100:
        errors.append("max_daily_drawdown_percent must be in (0, 100]")
    if not 0 < g["max_account_drawdown"] <= 100:
        errors.append("max_account_drawdown must be in (0, 100]")
    if g["hold_minutes"] < 1:
        errors.append(f"hold_minutes={g['hold_minutes']} — must be ≥1")
    if g["cooldown_after_sl_min"] < 0:
        errors.append("cooldown_after_sl_min must be ≥0")
    if g["cooldown_after_tp_min"] < 0:
        errors.append("cooldown_after_tp_min must be ≥0")
    if g["partial_close_usdt"] < 0:
        errors.append("partial_close_usdt must be ≥0 (0 = disabled)")
    if not 0 <= g["min_confidence"] <= 100:
        errors.append(f"min_confidence={g['min_confidence']} — must be 0–100")
    if not 0 <= g["min_adx"] <= 100:
        errors.append(f"min_adx={g['min_adx']} — must be 0–100")
    if g["max_trades_per_coin_per_day"] < 1:
        errors.append("max_trades_per_coin_per_day must be ≥1")

    # Spread caps
    for k in ("max_spread_pct", "max_spread_trend",
              "max_spread_range", "max_spread_volatility"):
        if not 0 <= g[k] <= 5.0:
            errors.append(f"{k}={g[k]} — must be in [0, 5.0]")

    # Entry/exit safety constant bounds
    if not 0 < g["margin_buffer_pct"] <= 1:
        errors.append(f"margin_buffer_pct={g['margin_buffer_pct']} — must be in (0, 1]")
    if not 0 < g["counter_trend_size_mult"] <= 1:
        errors.append(f"counter_trend_size_mult={g['counter_trend_size_mult']} — must be in (0, 1]")
    if g["min_notional_bump_mult"] < 1.0:
        errors.append(f"min_notional_bump_mult={g['min_notional_bump_mult']} — must be ≥1.0")
    if not 0 < g["corrected_qty_close_ratio"] < 1:
        errors.append(f"corrected_qty_close_ratio={g['corrected_qty_close_ratio']} — must be in (0, 1)")
    if not 0 < g["sl_tight_ratio"] < 1:
        errors.append(f"sl_tight_ratio={g['sl_tight_ratio']} — must be in (0, 1)")
    if g["sl_wide_ratio"] <= 1.0:
        errors.append(f"sl_wide_ratio={g['sl_wide_ratio']} — must be >1.0")
    if g["risk_oversize_warn_mult"] <= 1.0:
        errors.append(f"risk_oversize_warn_mult={g['risk_oversize_warn_mult']} — must be >1.0")
    if g["rr_collapse_tol"] < 0:
        errors.append(f"rr_collapse_tol={g['rr_collapse_tol']} — must be ≥0")
    if g["tiny_loss_r"] < 0:
        errors.append(f"tiny_loss_r={g['tiny_loss_r']} — must be ≥0")

    if errors:
        raise ValueError(
            "config_center validation failed:\n  • " + "\n  • ".join(errors)
        )


# ═══════════════════════════════════════════════════════════════
#  BOOTSTRAP
# ═══════════════════════════════════════════════════════════════
_apply_env_overrides()
_validate()


# ═══════════════════════════════════════════════════════════════
#  DIAGNOSTIC
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  CONFIG CENTER DIAGNOSTIC — REV 5.5 (unified, single source)")
    print("=" * 70)

    print()
    print("  Migrated trading params (was in core/config.py):")
    for k in (
        "leverage", "risk_percent", "max_open_positions",
        "max_same_side_positions", "max_total_margin_pct",
        "max_daily_loss_trades", "max_daily_drawdown_percent",
        "max_account_drawdown", "hold_minutes",
        "cooldown_after_sl_min", "cooldown_after_tp_min",
        "partial_close_usdt", "min_confidence", "min_adx",
        "require_htf_agreement", "use_5m_trend_filter",
        "fg_enabled", "max_trades_per_coin_per_day", "htf_align_relaxed",
        "use_taker_volume", "use_volume_profile", "use_anchored_vwap",
        "use_funding_z", "use_mtf_confluence", "kz_bypass",
        "use_spread_filter", "max_spread_pct",
        "max_spread_trend", "max_spread_range", "max_spread_volatility",
    ):
        print(f"    {k:<32} = {GLOBAL[k]}")

    print()
    print("  Entry/exit safety & sizing constants:")
    for k in (
        "margin_buffer_pct", "counter_trend_size_mult",
        "min_notional_bump_mult", "corrected_qty_close_ratio",
        "sl_tight_ratio", "sl_wide_ratio", "risk_oversize_warn_mult",
        "rr_collapse_tol", "tiny_loss_r",
        "trail_distance_r", "trail_hysteresis_r", "trail_min_level",
    ):
        print(f"    {k:<32} = {GLOBAL[k]}")

    print()
    print("  VOL_CLASS_QTY_MULT:")
    for _vc, _mv in VOL_CLASS_QTY_MULT.items():
        print(f"    {_vc:<6} = {_mv}")

    print()
    print("  TIME_EXIT state:")
    print(f"    time_exit_enabled     = {GLOBAL.get('time_exit_enabled')}")
    print(f"    hold_minutes          = {GLOBAL.get('hold_minutes')}")
    print(f"    is_time_exit_enabled()= {is_time_exit_enabled()}")

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
    print("  FAMILY FILTERS (post-cleanup):")
    for _fam in ("momentum_coins", "volatility_coins", "trend_coins", "range_coins"):
        _f = FAMILY[_fam]["FILTERS"]
        print(f"  {_fam:20} {len(_f)} keys")
    print("=" * 70)