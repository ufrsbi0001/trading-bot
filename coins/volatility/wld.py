"""
coins/volatility/wld.py — WLDUSDT per-coin config.

REV 20.1 (2026-09-28) — RSI BANDS WIDENED (HIGH tier):
  ✅ BUY  min/max 38/68 → 34/72
  ✅ SELL min/max 32/62 → 28/66
  Aligns with volatility_coins.py family-wide REV 20.2 widening.

REV 19.18 (2026-09-27) — NEW COIN.
REV 19.0 (2026-09-22) — auto-generated.
"""

SYMBOL     = "WLDUSDT"
BASE       = "WLD"
FAMILY     = "volatility_coins"
PROFILE    = "VOLATILE"
ENABLED    = True
VOL_CLASS  = "HIGH"

CAPS = {
    "sl":  0.065,
    "tp1": 0.0975,       # exactly 1.5x SL
    "tp2": 0.1625,       # exactly 2.5x SL
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 4.0,
    "tp2_atr": 8.0,
}

FILTERS = {
    "rsi_buy_min":  34.0,   # was 38.0 (REV 20.1)
    "rsi_buy_max":  72.0,   # was 68.0
    "rsi_sell_min": 28.0,   # was 32.0
    "rsi_sell_max": 66.0,   # was 62.0
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
    "mr_rsi_buy_max":    35.0,
    "mr_rsi_sell_min":   65.0,
    "mr_stoch_buy_max":  32.0,
    "mr_stoch_sell_min": 68.0,
    "cf_rsi_buy_max":    35.0,
    "cf_rsi_sell_min":   65.0,
    "rs_rsi_buy_max":    40.0,
    "rs_rsi_sell_min":   60.0,
}

TD_FADE = {
    "rsi_sell":     65.0,
    "rsi_buy":      32.0,
    "sl_atr":       0.9,
    "adx_max":      65.0,
    "ema_tol":      1.05,
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  True,
}