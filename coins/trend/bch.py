"""
coins/trend/bch.py — BCHUSDT per-coin config.

REV 20.1 (2026-09-28) — RSI BANDS WIDENED (LOW tier):
  ✅ BUY  min/max 42/62 → 38/67
  ✅ SELL min/max 40/58 → 35/63
  Live log evidence: BCH hit `rsi_sell_low_39` on a valid ST_SELL
  setup that would have passed with the new 35 floor.
  Same delta as btc.py — see btc.py header for rationale.

REV 19.17 (2026-09-27) — LOW-TIER TP1 RATIO FIX.
REV 19.16 (2026-09-26) — NEW COIN.
REV 19.15 (2026-09-26) — COMMENT ACCURACY PASS.
REV 19.14 (2026-09-25) — LOW TIER ALIGNMENT.
REV 19.7 (2026-09-23) — BUG FIXES (min_flips, block_ny_am, MR RSI).
REV 19.6 (2026-09-23) — PER-COIN RSI.
REV 19.1 (2026-09-22) — RR FIX.
REV 19.0 (2026-09-22) — auto-generated.
"""

SYMBOL     = "BCHUSDT"
BASE       = "BCH"
FAMILY     = "trend_coins"
PROFILE    = "TREND"
ENABLED    = True
VOL_CLASS  = "LOW"

CAPS = {
    "sl":  0.0345,
    "tp1": 0.0518,       # ~1.5x SL (0.0345 * 1.5 = 0.05175 → 0.0518)
    "tp2": 0.0863,       # ~2.5x SL (0.0345 * 2.5 = 0.08625 → 0.0863)
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 3.75,     # was 3.5 (1.4x) — now 1.5x SL, matches CAPS
    "tp2_atr": 6.25,
}

FILTERS = {
    "rsi_buy_min":  38.0,   # was 42.0 (REV 20.1)
    "rsi_buy_max":  67.0,   # was 62.0
    "rsi_sell_min": 35.0,   # was 40.0
    "rsi_sell_max": 63.0,   # was 58.0
    "min_flips":    2,
    "max_flips":    10,
    "min_dist_atr": 0.2,
    "max_dist_atr": 1.4,
    "min_adx_st":   25.0,
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