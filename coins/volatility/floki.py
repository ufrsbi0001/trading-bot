"""
coins/volatility/floki.py — 1000FLOKIUSDT per-coin config.

REV 20.1 (2026-09-28) — RSI BANDS WIDENED (MED tier):
  ✅ BUY  min/max 40/67 → 36/71
  ✅ SELL min/max 33/60 → 29/64
  Aligns with volatility_coins.py family-wide REV 20.2 widening.

REV 19.15 (2026-09-26) — COMMENT ACCURACY PASS.
REV 19.14 (2026-09-25) — MED TIER ALIGNMENT.
REV 19.13 (2026-09-25) — ENABLED.
REV 19.12 (2026-09-25) — DIRECTORY-ALIGNED + CLEANUP.
REV 19.8  (2026-09-24) — VOLATILITY FAMILY.
"""

SYMBOL     = "1000FLOKIUSDT"
BASE       = "1000FLOKI"
FAMILY     = "volatility_coins"
PROFILE    = "VOLATILE"
ENABLED    = True
VOL_CLASS  = "MED"

CAPS = {
    "sl":  0.0626,
    "tp1": 0.0939,       # exactly 1.5x SL
    "tp2": 0.1565,       # exactly 2.5x SL
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 3.75,
    "tp2_atr": 7.5,
}

FILTERS = {
    "rsi_buy_min":  36.0,   # was 40.0 (REV 20.1)
    "rsi_buy_max":  71.0,   # was 67.0
    "rsi_sell_min": 29.0,   # was 33.0
    "rsi_sell_max": 64.0,   # was 60.0
    "min_flips":    3,
    "max_flips":    10,
    "min_dist_atr": 0.3,
    "max_dist_atr": 1.4,
    "min_adx_st":   22.0,
    "late_guard_adx":  35.0,
    "late_guard_dist": 1.0,
    "late_guard_rsi":  58.0,
    "top_chase_rsi":   68.0,
    "top_chase_flips": 4,
    "block_ny_am":  False,
    "block_ny_pm":  False,
    "mr_rsi_buy_max":    32.0,
    "mr_rsi_sell_min":   68.0,
    "mr_stoch_buy_max":  28.0,
    "mr_stoch_sell_min": 72.0,
    "cf_rsi_buy_max":    32.0,
    "cf_rsi_sell_min":   68.0,
    "rs_rsi_buy_max":    35.0,
    "rs_rsi_sell_min":   65.0,
}

TD_FADE = {
    "rsi_sell":     65.0,
    "rsi_buy":      32.0,
    "sl_atr":       0.9,
    "adx_max":      65.0,
    "ema_tol":      1.02,
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  True,
}