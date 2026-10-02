"""
core/family_baselines.py — Canonical tuning for all 4 coin families.

Single source of truth for:
  • ST_PARAMS (sl_atr, tp1_atr, tp2_atr)
  • CAPS (max sl/tp1/tp2 pct)
  • FILTERS (late_guard, top_chase, flips, RSI bands, etc.)

Coin files only carry IDENTITY (SYMBOL, FAMILY, VOL_CLASS, ENABLED).
All tuning comes from this file — family-level, uniform, no drift.

VOL_CLASS_MULT adjusts caps up/down based on coin volatility class
(LOW = stable majors, HIGH = wild altcoins).
"""
from __future__ import annotations


# ═══════════════════════════════════════════════════════════
#  VOL CLASS CAP MULTIPLIERS
#  Applied at runtime in signals/base.py::_mk() to caps.
# ═══════════════════════════════════════════════════════════
VOL_CLASS_CAP_MULT: dict[str, float] = {
    "LOW":  0.80,   # majors (BTC, ETH, BNB) — tight caps
    "MED":  1.00,   # mid-cap (APT, SOL, ADA) — baseline
    "HIGH": 1.40,   # high-beta (HYPE, MORPHO, WLD) — wider caps
}


# ═══════════════════════════════════════════════════════════
#  FAMILY BASELINES
# ═══════════════════════════════════════════════════════════
BASELINES: dict[str, dict] = {

    # ── MOMENTUM COINS (breakout movers) ──
    "momentum_coins": {
        "ST_PARAMS": {
            "sl_atr":  2.5,
            "tp1_atr": 3.75,   # RR 1.5
            "tp2_atr": 7.5,    # RR 3.0
        },
        "CAPS": {
            "sl":  0.060,      # 6% max SL
            "tp1": 0.090,      # 9% max TP1
            "tp2": 0.150,      # 15% max TP2
        },
        "FILTERS": {
            # Distance
            "max_dist_atr":      1.4,
            "pullback_dist_atr": 0.8,
            "min_dist_atr":      0.4,
            # ADX + flips
            "min_adx_st":        22.0,
            "min_flips":         2,
            "max_flips":         10,
            # Late-entry guard
            "late_guard_adx":    35.0,
            "late_guard_rsi":    58.0,
            "late_guard_dist":   1.0,
            # Top-chase guard
            "top_chase_rsi":     68.0,
            "top_chase_flips":   4,
            # RSI bands
            "rsi_buy_min":       36.0,
            "rsi_buy_max":       74.0,
            "rsi_sell_min":      26.0,
            "rsi_sell_max":      66.0,
        },
    },

    # ── VOLATILITY COINS (wild alts) ──
    "volatility_coins": {
        "ST_PARAMS": {
            "sl_atr":  2.5,
            "tp1_atr": 3.75,
            "tp2_atr": 7.5,
        },
        "CAPS": {
            "sl":  0.065,
            "tp1": 0.0975,
            "tp2": 0.1625,
        },
        "FILTERS": {
            "max_dist_atr":      1.4,
            "pullback_dist_atr": 0.8,
            "min_dist_atr":      0.6,
            "min_adx_st":        22.0,
            "min_flips":         3,
            "max_flips":         12,
            "late_guard_adx":    35.0,
            "late_guard_rsi":    58.0,
            "late_guard_dist":   1.0,
            "top_chase_rsi":     68.0,
            "top_chase_flips":   4,
            "rsi_buy_min":       34.0,
            "rsi_buy_max":       72.0,
            "rsi_sell_min":      28.0,
            "rsi_sell_max":      66.0,
        },
    },

    # ── TREND COINS (smooth majors) ──
    "trend_coins": {
        "ST_PARAMS": {
            "sl_atr":  2.5,
            "tp1_atr": 3.75,
            "tp2_atr": 7.0,
        },
        "CAPS": {
            "sl":  0.045,
            "tp1": 0.0675,
            "tp2": 0.125,
        },
        "FILTERS": {
            "max_dist_atr":      1.4,
            "pullback_dist_atr": 0.8,
            "min_dist_atr":      0.5,
            "min_adx_st":        22.0,
            "min_flips":         3,
            "max_flips":         15,
            "late_guard_adx":    35.0,
            "late_guard_rsi":    58.0,
            "late_guard_dist":   1.0,
            "top_chase_rsi":     68.0,
            "top_chase_flips":   4,
            "rsi_buy_min":       34.0,
            "rsi_buy_max":       70.0,
            "rsi_sell_min":      30.0,
            "rsi_sell_max":      65.0,
        },
    },

    # ── RANGE COINS (mean-reverting) ──
    "range_coins": {
        "ST_PARAMS": {
            "sl_atr":  1.8,
            "tp1_atr": 2.7,
            "tp2_atr": 4.5,
        },
        "CAPS": {
            "sl":  0.040,
            "tp1": 0.060,
            "tp2": 0.100,
        },
        "FILTERS": {
            "max_dist_atr":      1.4,
            "pullback_dist_atr": 0.8,
            "min_dist_atr":      0.2,
            "min_adx_st":        24.0,
            "min_flips":         4,
            "max_flips":         10,
            "late_guard_adx":    35.0,
            "late_guard_rsi":    58.0,
            "late_guard_dist":   1.0,
            "top_chase_rsi":     68.0,
            "top_chase_flips":   4,
            "rsi_buy_min":       40.0,
            "rsi_buy_max":       68.0,
            "rsi_sell_min":      33.0,
            "rsi_sell_max":      60.0,
        },
    },
}

DEFAULT_FAMILY = "trend_coins"


# ═══════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════
def get_baseline(family: str) -> dict:
    """Return family baseline dict. Falls back to trend_coins."""
    return BASELINES.get(family, BASELINES[DEFAULT_FAMILY])


def get_vol_class_mult(vol_class: str) -> float:
    """Return cap multiplier for vol class. Falls back to 1.0."""
    return float(VOL_CLASS_CAP_MULT.get(vol_class, 1.0))


def describe(family: str) -> str:
    """Human-readable dump for debugging."""
    b = get_baseline(family)
    lines = [f"Family: {family}"]
    lines.append(f"  ST_PARAMS: {b['ST_PARAMS']}")
    lines.append(f"  CAPS:      {b['CAPS']}")
    lines.append(f"  FILTERS:   {len(b['FILTERS'])} keys")
    for k in sorted(b["FILTERS"]):
        lines.append(f"    {k:<22} = {b['FILTERS'][k]}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTIC
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  FAMILY BASELINES DIAGNOSTIC")
    print("=" * 70)
    for fam in BASELINES:
        print()
        print(describe(fam))
    print()
    print("=" * 70)
    print("  VOL CLASS MULTIPLIERS")
    print("=" * 70)
    for vcls, mult in VOL_CLASS_CAP_MULT.items():
        print(f"  {vcls:<6} → caps × {mult}")
    print("=" * 70)
    print("  If you see this, family_baselines.py is WORKING ✅")
    print("=" * 70)