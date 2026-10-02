"""
coins/volatility/wif.py — WIFUSDT per-coin config.

REV 19.16 (2026-09-29) — REV 20.2 RSI WIDENING ALIGNMENT (LAG FIX):
  ✅ st_rsi_buy_min  38.0 → 34.0  (REV 20.2 BUY band: 38/68 → 34/72)
  ✅ st_rsi_buy_max  68.0 → 72.0  (REV 20.2 BUY band: 38/68 → 34/72)
  ✅ st_rsi_sell_min 32.0 → 28.0  (REV 20.2 SELL band: 32/62 → 28/66)
  ✅ st_rsi_sell_max 62.0 → 66.0  (REV 20.2 SELL band: 32/62 → 28/66)

  Root cause: WIF was the only volatility.HIGH coin whose RSI bands
  were not bumped in the 2026-09-28 REV 20.2 sweep. Its 5 peers
  (bome, ena, pepe, pump, wld) all received the widened values; WIF
  kept the pre-REV 20.2 settings. verify_tiers.py REV 3.1 flagged
  the rsi_buy_max drift (68.0 vs majority 72.0, 5/6), and the same
  miss affected the other three RSI band edges.

  No other fields changed — CAPS, ST_PARAMS, and the MEAN_REVERSION
  / RANGE_SCALPER / CHOP_FADE tunings (mr_*, rs_*, cf_*) are coin-
  specific and were already consistent with EXPECTED["volatility"]["HIGH"].

REV 19.15 (2026-09-26) — COMMENT ACCURACY PASS:
  ✅ CAPS.tp2 comment corrected: "ratio 2.5x SL" → "~2.5x SL (rounded 4dp)".
     (0.0801 * 2.5 = 0.20025; tp2 = 0.2000 is 4dp-rounded.)
  ✅ Tier note added: HIGH near-canonical (sl 8.01% — 1bp above 8.00%).
  ✅ Docstring REV header updated.

REV 19.14 (2026-09-25) — HIGH TIER ALIGNMENT:
  ✅ max_flips: 14.0 (canonical HIGH value) — already correct.
  ✅ CAPS.tp2 0.2000 (~2.5x SL, rounded) — confirmed.
  ✅ Family-wide consistency pass confirmed.

REV 19.12 (2026-09-25) — DIRECTORY-ALIGNED + CLEANUP.
REV 19.8  (2026-09-24) — VOLATILITY FAMILY: Memecoin (2023) — HIGH vol.
"""

SYMBOL     = "WIFUSDT"
BASE       = "WIF"
FAMILY     = "volatility_coins"
PROFILE    = "VOLATILE"
ENABLED    = True
VOL_CLASS  = "HIGH"

# ─── CAPS — HIGH near-canonical (1bp above 8.00% canonical) ───
CAPS = {
    "sl":  0.065,        # 8.01% — 1bp above HIGH canonical 8.00%
    "tp1": 0.0975,       # ~1.5x SL (0.0801 * 1.5 = 0.12015 → 0.1202)
    "tp2": 0.1625,       # ~2.5x SL (0.0801 * 2.5 = 0.20025 → 0.2000, rounded)
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 4.0,
    "tp2_atr": 8.0,
}

FILTERS = {
    # ── REV 19.16: RSI bands aligned to REV 20.2 (volatility family) ──
    "rsi_buy_min":  34.0,    # was 38.0
    "rsi_buy_max":  72.0,    # was 68.0  ← flagged by verify_tiers as LAG
    "rsi_sell_min": 28.0,    # was 32.0
    "rsi_sell_max": 66.0,    # was 62.0
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