"""
coins/trend/sol.py — SOLUSDT per-coin config.

REV 20.1 (2026-09-28) — RSI BANDS WIDENED (HIGH tier):
  ✅ BUY  min/max 38/68 → 32/73
  ✅ SELL min/max 32/62 → 27/67
  Same delta as btc.py — see btc.py header for rationale.
  NOTE: SOL's CAPS/ST_PARAMS are the HIGH-tier canonical that
  the other HIGH coins (DOGE, XRP, HYPE) copy.

REV 19.15 (2026-09-26) — COMMENT ACCURACY PASS.
REV 19.14 (2026-09-25) — HIGH TIER.
REV 19.7 (2026-09-23) — BUG FIXES.
REV 19.6 (2026-09-23) — PER-COIN RSI.
REV 19.1 (2026-09-22) — RR FIX.
REV 19.0 (2026-09-22) — auto-generated.
"""

SYMBOL     = "SOLUSDT"
BASE       = "SOL"
FAMILY     = "trend_coins"
PROFILE    = "TREND"
ENABLED    = True
VOL_CLASS  = "HIGH"

CAPS = {
    "sl":  0.0542,
    "tp1": 0.0870,       # 4dp rounded; ST_PARAMS imply 1.6x SL
    "tp2": 0.1735,       # 4dp rounded; ST_PARAMS imply 3.2x SL
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 4.0,
    "tp2_atr": 8.0,
}

FILTERS = {
    "rsi_buy_min":  32.0,   # was 38.0 (REV 20.1)
    "rsi_buy_max":  73.0,   # was 68.0
    "rsi_sell_min": 27.0,   # was 32.0
    "rsi_sell_max": 67.0,   # was 62.0
    "min_flips":    4,
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
    "adx_max":      55.0,
    "ema_tol":      1.05,  # wider tolerance for HIGH tier (others use 1.02)
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  True,
}