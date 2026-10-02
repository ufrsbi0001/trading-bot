"""
coins/trend/eigen.py — EIGENUSDT per-coin config.

2026-09-29 — INITIAL: EigenCloud (prev. EigenLayer). Trend family, MED tier.
  Restaking narrative. Active daily moves.
"""

SYMBOL     = "EIGENUSDT"
BASE       = "EIGEN"
FAMILY     = "trend_coins"
PROFILE    = "TREND"
ENABLED    = True
VOL_CLASS  = "MED"

CAPS = {
    "sl":  0.050,
    "tp1": 0.075,
    "tp2": 0.125,
}

ST_PARAMS = {
    "sl_atr":  2.4,
    "tp1_atr": 3.6,   # AUTO-FIX RR 1.250 → 1.5
    "tp2_atr": 6.0,
}

FILTERS = {
    "rsi_buy_min":  38.0,
    "rsi_buy_max":  70.0,
    "rsi_sell_min": 30.0,
    "rsi_sell_max": 65.0,
    "min_flips":    3,
    "max_flips":    10,
    "min_dist_atr": 0.2,
    "max_dist_atr": 1.4,
    "min_adx_st":   22.0,
    "mr_rsi_buy_max":    32.0,
    "mr_rsi_sell_min":   68.0,
    "mr_stoch_buy_max":  25.0,
    "mr_stoch_sell_min": 75.0,
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
    "enabled_buy":  False,
}