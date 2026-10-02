"""
coins/momentum/aster.py — ASTERUSDT per-coin config (MOMENTUM family).

REV 20.2 (2026-09-29) — NEW COIN:
  ✅ Aster (ASTER) — new DeFi perp DEX.
     Live snapshot: rank #53, $1.9B mcap, $17.7M 24h volume.
  ✅ MED tier — matches APT/ARB canonical.
  ✅ Momentum rationale: fresh listing with active narrative,
     breakout-prone, moderate-high beta.
"""

SYMBOL     = "ASTERUSDT"
BASE       = "ASTER"
FAMILY     = "momentum_coins"
PROFILE    = "MOMENTUM"
ENABLED    = True
VOL_CLASS  = "MED"

# CAPS — momentum MED canonical (1.5x / 2.5x SL)
CAPS = {
    "sl":  0.0650,
    "tp1": 0.0975,       # exactly 1.5x SL
    "tp2": 0.1625,       # exactly 2.5x SL
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 3.75,
    "tp2_atr": 7.5,
}

FILTERS = {
    "rsi_buy_min":  36.0,
    "rsi_buy_max":  74.0,
    "rsi_sell_min": 26.0,
    "rsi_sell_max": 66.0,

    "min_flips":    2,
    "max_flips":    10,

    "min_dist_atr": 0.25,
    "max_dist_atr": 1.4,

    "min_adx_st":   22.0,

    "late_guard_adx":  35.0,
    "late_guard_dist": 1.0,
    "late_guard_rsi":  58.0,

    "top_chase_rsi":   68.0,
    "top_chase_flips": 4,

    "block_ny_am":  False,
    "block_ny_pm":  False,

    "mr_rsi_buy_max":    30.0,
    "mr_rsi_sell_min":   70.0,
    "mr_stoch_buy_max":  25.0,
    "mr_stoch_sell_min": 75.0,

    "cf_rsi_buy_max":    30.0,
    "cf_rsi_sell_min":   70.0,

    "rs_rsi_buy_max":    35.0,
    "rs_rsi_sell_min":   65.0,
}

TD_FADE = {
    "rsi_sell":     65.0,
    "rsi_buy":      32.0,
    "sl_atr":       0.9,
    "adx_max":      55.0,
    "ema_tol":      1.02,
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  True,
}