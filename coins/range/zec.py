"""
coins/range/zec.py — ZECUSDT per-coin config (RANGE family).

REV 20.1 (2026-09-28) — RSI BANDS WIDENED:
  ✅ BUY  min/max 45/62 → 40/68
  ✅ SELL min/max 38/55 → 33/60
  Aligns with range_coins.py family-wide REV 20.2 widening.

REV 19.16 (2026-09-27) — ST_PARAMS RATIO FIX.
REV 19.15 (2026-09-26) — COMMENT ACCURACY PASS.
REV 19.13 (2026-09-25) — MIGRATED volatility_coins → range_coins.
"""

SYMBOL     = "ZECUSDT"
BASE       = "ZEC"
FAMILY     = "range_coins"
PROFILE    = "RANGE"
ENABLED    = True
VOL_CLASS  = "LOW"

CAPS = {
    "sl":  0.035,
    "tp1": 0.0525,   # AUTO-FIX rr1 1.30 → 1.5
    "tp2": 0.0875,   # AUTO-FIX rr2 2.00 → 2.5
}

ST_PARAMS = {
    "sl_atr":  1.8,
    "tp1_atr": 2.7,   # AUTO-FIX RR 1.000 → 1.5
    "tp2_atr": 4.5,
}

FILTERS = {
    "rsi_buy_min":  40.0,   # was 45.0 (REV 20.1)
    "rsi_buy_max":  68.0,   # was 62.0
    "rsi_sell_min": 33.0,   # was 38.0
    "rsi_sell_max": 60.0,   # was 55.0
    "min_flips":    4,
    "max_flips":    10,
    "min_dist_atr": 0.2,
    "max_dist_atr": 1.4,
    "min_adx_st":   24.0,
    "late_guard_adx":  35.0,
    "late_guard_dist": 1.0,
    "late_guard_rsi":  58.0,
    "top_chase_rsi":   68.0,
    "top_chase_flips": 4,
    "block_ny_am":  False,
    "block_ny_pm":  False,
    "mr_rsi_buy_max":    35.0,
    "mr_rsi_sell_min":   65.0,
    "mr_stoch_buy_max":  30.0,
    "mr_stoch_sell_min": 70.0,
    "cf_rsi_buy_max":    35.0,
    "cf_rsi_sell_min":   65.0,
    "rs_rsi_buy_max":    42.0,
    "rs_rsi_sell_min":   58.0,
}

TD_FADE = {
    "rsi_sell":     63.0,
    "rsi_buy":      37.0,
    "sl_atr":       0.8,
    "adx_max":      45.0,
    "ema_tol":      1.01,
    "min_adx":      18.0,
    "bb_pos_sell":  0.97,
    "bb_pos_buy":   0.03,
    "enabled_sell": True,
    "enabled_buy":  True,
}