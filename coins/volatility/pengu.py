"""
coins/volatility/pengu.py — PENGUUSDT per-coin config.

2026-09-29 — INITIAL: Pudgy Penguins. Volatility family, HIGH tier.
  Meme leader, good liquidity.
"""

SYMBOL     = "PENGUUSDT"
BASE       = "PENGU"
FAMILY     = "volatility_coins"
PROFILE    = "VOLATILE"
ENABLED    = True
VOL_CLASS  = "HIGH"

CAPS = {
    "sl":  0.065,
    "tp1": 0.0975,
    "tp2": 0.1625,
}

ST_PARAMS = {
    "sl_atr":  2.6,
    "tp1_atr": 3.9,   # AUTO-FIX RR 1.346 → 1.5
    "tp2_atr": 6.5,
}

FILTERS = {
    "rsi_buy_min":  34.0,
    "rsi_buy_max":  72.0,
    "rsi_sell_min": 28.0,
    "rsi_sell_max": 66.0,
    "min_flips":    4,
    "max_flips":    10,
    "min_dist_atr": 0.3,
    "max_dist_atr": 1.4,
    "min_adx_st":   22.0,
    "mr_rsi_buy_max":    35.0,
    "mr_rsi_sell_min":   65.0,
    "mr_stoch_buy_max":  32.0,
    "mr_stoch_sell_min": 68.0,
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