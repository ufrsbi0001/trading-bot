"""
coins/trend/ada.py — ADAUSDT per-coin config.

REV 20.1 (2026-09-28) — RSI BANDS WIDENED (MED tier):
  ✅ BUY  min/max 38/65 → 34/70
  ✅ SELL min/max 35/60 → 30/65
  Same delta as btc.py — see btc.py header for rationale.

REV 19.15 (2026-09-26) — COMMENT ACCURACY PASS.
REV 19.14 (2026-09-25) — MED TIER ALIGNMENT.
REV 19.7 (2026-09-23) — BUG FIXES.
REV 19.6 (2026-09-23) — PER-COIN RSI.
REV 19.1 (2026-09-22) — RR FIX.
REV 19.0 (2026-09-22) — auto-generated.
"""

SYMBOL     = "ADAUSDT"
BASE       = "ADA"
FAMILY     = "trend_coins"
PROFILE    = "TREND"
ENABLED    = True
VOL_CLASS  = "MED"

CAPS = {
    "sl":  0.0479,
    "tp1": 0.0719,       # was 0.0720 — ~1.5x SL (0.0479 * 1.5 = 0.07185 → 0.0719)
    "tp2": 0.1341,       # was 0.1345 — ~2.8x SL (0.0479 * 2.8 = 0.13412 → 0.1341)
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 3.75,
    "tp2_atr": 7.0,
}

FILTERS = {
    "rsi_buy_min":  34.0,   # was 38.0 (REV 20.1)
    "rsi_buy_max":  70.0,   # was 65.0
    "rsi_sell_min": 30.0,   # was 35.0
    "rsi_sell_max": 65.0,   # was 60.0
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
    "adx_max":      55.0,
    "ema_tol":      1.02,
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  True,
}