"""
config_center.py — SINGLE SOURCE OF TRUTH for all trading tunables.

REV 1.0 (2026-10-02) — CENTRALIZED CONFIG:
  Consolidates configuration previously scattered across:
    - config.py (env-mapped values)
    - indicators.py (_REGIME_CFG, _BASE_CFG)
    - families/base.py (FamilySpec defaults)
    - family modules (MIN_ADX, FALLBACK_CAPS)
    - orders/utils.py (_STRATEGY_HOLD_MIN, _PER_CLASS_CFG)
    - coins/*/coin.py (per-coin overrides)
    - entry.py (_MAX_SIGNAL_DRIFT_PCT)
    - manage.py (TRAIL_* constants)

  Resolution hierarchy (most specific wins):
     COIN > FAMILY > STRATEGY_REGIME > REGIME > GLOBAL

  Public API:
     get_config(symbol, strategy, regime, vol_class) -> dict
     describe(symbol, strategy, regime, vol_class) -> str
     dump_all() -> dict
"""
from __future__ import annotations

import os
import threading
from copy import deepcopy


# ═════════════════════════════════════════════════════════════
#  LAYER 1 — GLOBAL DEFAULTS
# ═════════════════════════════════════════════════════════════
GLOBAL: dict = {
    # Entry guards
    "signal_drift_pct":        0.5,
    "min_sl_pct":              0.0075,
    "min_dist_atr":            0.3,
    "max_dist_atr":            1.4,
    "pullback_dist_atr":       0.8,

    # Late-entry guard (was DEAD, now calibrated)
    "late_guard_adx":          35.0,
    "late_guard_rsi":          58.0,
    "late_guard_dist":         1.0,

    # Top-chase guard
    "top_chase_rsi":           68.0,
    "top_chase_flips":         4,

    # Supertrend params
    "sl_atr":                  2.5,
    "tp1_atr":                 2.5,
    "tp2_atr":                 5.0,

    # Hold time
    "hold_minutes":            180,

    # R-multiple thresholds
    "be_r_min":                0.75,
    "be_stop_r":               0.15,
    "lock1_r_min":             1.10,
    "lock1_stop_r":            0.55,
    "lock2_r_min":             1.70,
    "lock2_stop_r":            0.90,
    "partial_stop_r":          0.30,

    # Trailing SL
    "trail_distance_r":        0.50,
    "trail_hysteresis_r":      0.10,
    "trail_min_level":         1,

    # Supertrend flips
    "st_flips_min":            3,
    "st_flips_max":            10,

    # RSI bands
    "rsi_buy_min":             35.0,
    "rsi_buy_max":             72.0,
    "rsi_sell_min":            28.0,
    "rsi_sell_max":            66.0,

    # RR floors
    "min_rr_supertrend":       1.5,
    "min_rr_mean_reversion":   1.8,
    "min_rr_range":            1.8,
    "min_rr_td_fade":          1.5,
}


# ═════════════════════════════════════════════════════════════
#  LAYER 2 — REGIME OVERRIDES
# ═════════════════════════════════════════════════════════════
REGIME: dict = {
    "TREND_UP": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "hold_minutes":    300,
        "late_guard_adx":  38.0,
    },
    "TREND_DOWN": {
        "sl_mult":         1.0,
        "tp_mult":         1.0,
        "hold_minutes":    300,
        "late_guard_adx":  38.0,
    },
    "VOLATILE": {
        "sl_mult":         1.3,
        "tp_mult":         0.70,
        "hold_minutes":    120,
        "late_guard_adx":  35.0,
    },
    "QUIET": {
        "sl_mult":         0.7,
        "tp_mult":         0.60,
        "hold_minutes":    180,
    },
    "CHOP": {
        "sl_mult":         1.0,
        "tp_mult":         0.45,
        "hold_minutes":    90,
        "late_guard_adx":  32.0,
    },
    "UNKNOWN": {
        "sl_mult":         1.0,
        "tp_mult":         0.55,
        "hold_minutes":    120,
    },
}


# ═════════════════════════════════════════════════════════════
#  LAYER 3 — STRATEGY × REGIME
# ═════════════════════════════════════════════════════════════
STRATEGY_REGIME: dict = {
    "SUPERTREND_RIDE": {
        "TREND_UP":    {"hold_minutes": 300, "tp1_atr": 2.8},
        "TREND_DOWN":  {"hold_minutes": 300, "tp1_atr": 2.8},
        "VOLATILE":    {"hold_minutes": 120, "tp1_atr": 1.8},
        "CHOP":        {"hold_minutes": 90,  "tp1_atr": 1.2},
    },
    "TREND_DOWN_FADE": {
        "TREND_DOWN":  {"hold_minutes": 180, "sl_atr": 1.8},
        "CHOP":        {"hold_minutes": 90,  "sl_atr": 1.5},
    },
    "MEAN_REVERSION": {
        "CHOP":        {"hold_minutes": 120, "tp1_atr": 1.5},
        "QUIET":       {"hold_minutes": 180, "tp1_atr": 1.8},
    },
    "RANGE_SCALPER": {
        "CHOP":        {"hold_minutes": 90,  "sl_atr": 1.5},
        "QUIET":       {"hold_minutes": 120, "sl_atr": 1.5},
        "VOLATILE":    {"hold_minutes": 60,  "sl_atr": 1.8},
    },
}


# ═════════════════════════════════════════════════════════════
#  LAYER 4 — FAMILY
# ═════════════════════════════════════════════════════════════
FAMILY: dict = {
    "momentum_coins": {
        "st_flips_min": 4, "st_flips_max": 10,
        "max_dist_atr": 1.4,
        "rsi_buy_min": 36.0, "rsi_buy_max": 74.0,
        "rsi_sell_min": 26.0, "rsi_sell_max": 66.0,
    },
    "volatility_coins": {
        "st_flips_min": 5, "st_flips_max": 12,
        "max_dist_atr": 1.4,
        "rsi_buy_min": 34.0, "rsi_buy_max": 72.0,
        "rsi_sell_min": 28.0, "rsi_sell_max": 66.0,
    },
    "trend_coins": {
        "st_flips_min": 4, "st_flips_max": 10,
        "max_dist_atr": 1.4,
        "rsi_buy_min": 38.0, "rsi_buy_max": 70.0,
        "rsi_sell_min": 30.0, "rsi_sell_max": 65.0,
    },
    "range_coins": {
        "st_flips_min": 5, "st_flips_max": 10,
        "max_dist_atr": 1.4,
        "min_sl_pct": 0.005,
        "rsi_buy_min": 40.0, "rsi_buy_max": 68.0,
        "rsi_sell_min": 33.0, "rsi_sell_max": 60.0,
    },
}


# ═════════════════════════════════════════════════════════════
#  LAYER 5 — COIN (lazy from coins/)
# ═════════════════════════════════════════════════════════════
_COIN_CACHE: dict = {}
_COIN_LOCK = threading.Lock()


def _load_coin_overrides(symbol: str) -> dict:
    """Safely fetch per-coin overrides. Never raises."""
    if not symbol:
        return {}
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    with _COIN_LOCK:
        if sym in _COIN_CACHE:
            return _COIN_CACHE[sym]

    overrides: dict = {}
    try:
        from core.coins_config import (
            get_family, get_caps, get_coin_st_params,
            get_coin_filters, get_coin_td_fade, get_coin_vol_class,
        )
        try:
            overrides["_family"] = get_family(sym) or "trend_coins"
        except Exception:
            overrides["_family"] = "trend_coins"
        try:
            overrides["_vol_class"] = get_coin_vol_class(sym) or "MED"
        except Exception:
            overrides["_vol_class"] = "MED"
        try:
            overrides["_caps"] = dict(get_caps(sym) or {})
        except Exception:
            overrides["_caps"] = {}
        try:
            overrides["_st_params"] = dict(get_coin_st_params(sym) or {})
        except Exception:
            overrides["_st_params"] = {}
        try:
            overrides["_filters"] = dict(get_coin_filters(sym) or {})
        except Exception:
            overrides["_filters"] = {}
        try:
            overrides["_td_fade"] = dict(get_coin_td_fade(sym) or {})
        except Exception:
            overrides["_td_fade"] = {}

        for k in ("sl_atr", "tp1_atr", "tp2_atr"):
            if k in overrides["_st_params"]:
                overrides[k] = overrides["_st_params"][k]

        for k in ("max_dist_atr", "pullback_dist_atr", "min_dist_atr",
                  "min_adx_st", "min_flips", "max_flips",
                  "late_guard_adx", "late_guard_rsi", "late_guard_dist",
                  "top_chase_rsi", "top_chase_flips",
                  "rsi_buy_min", "rsi_buy_max",
                  "rsi_sell_min", "rsi_sell_max"):
            if k in overrides["_filters"]:
                overrides[k] = overrides["_filters"][k]
    except Exception as e:
        print(f"[config_center] coin override load failed for {sym}: {e}")

    with _COIN_LOCK:
        _COIN_CACHE[sym] = overrides
    return overrides


def clear_coin_cache() -> None:
    """Clear cached coin overrides (useful for testing)."""
    with _COIN_LOCK:
        _COIN_CACHE.clear()


# ═════════════════════════════════════════════════════════════
#  RESOLVER
# ═════════════════════════════════════════════════════════════
def get_config(symbol: str = "", strategy: str = "",
               regime: str = "UNKNOWN",
               vol_class: str = "") -> dict:
    """
    Resolve effective config.

    Hierarchy (later layers override earlier):
      1. GLOBAL
      2. REGIME[regime]
      3. STRATEGY_REGIME[strategy][regime]
      4. FAMILY[family]
      5. COIN overrides
    """
    merged = deepcopy(GLOBAL)

    reg = REGIME.get(regime or "UNKNOWN", {})
    merged.update(reg)

    if strategy:
        sr = STRATEGY_REGIME.get(strategy, {}).get(regime, {})
        merged.update(sr)

    coin_ov = _load_coin_overrides(symbol) if symbol else {}
    family = coin_ov.get("_family") or "trend_coins"
    merged.update(FAMILY.get(family, {}))

    for k, v in coin_ov.items():
        if not k.startswith("_"):
            merged[k] = v

    merged["_meta"] = {
        "symbol": symbol,
        "strategy": strategy,
        "regime": regime,
        "family": family,
        "vol_class": vol_class or coin_ov.get("_vol_class", "MED"),
    }
    return merged


def describe(symbol: str, strategy: str, regime: str,
             vol_class: str = "") -> str:
    """Human-readable dump of effective config."""
    cfg = get_config(symbol, strategy, regime, vol_class)
    meta = cfg.pop("_meta", {})
    lines = [
        "=" * 70,
        f"  CONFIG: {meta.get('symbol','?')} "
        f"[{meta.get('strategy','?')} @ {meta.get('regime','?')}]",
        f"  family={meta.get('family','?')} "
        f"vol_class={meta.get('vol_class','?')}",
        "=" * 70,
    ]
    for k in sorted(cfg.keys()):
        v = cfg[k]
        if isinstance(v, (int, float, str, bool)):
            lines.append(f"  {k:<24} = {v}")
    return "\n".join(lines)


def dump_all() -> dict:
    """Introspection: return all layers."""
    return {
        "global":          deepcopy(GLOBAL),
        "regime":          deepcopy(REGIME),
        "strategy_regime": deepcopy(STRATEGY_REGIME),
        "family":          deepcopy(FAMILY),
        "coin_cache_keys": list(_COIN_CACHE.keys()),
    }


# ═════════════════════════════════════════════════════════════
#  ENV OVERRIDES — Hot-tunable via CC_<KEY>=value
# ═════════════════════════════════════════════════════════════
def _apply_env_overrides() -> None:
    """
    Env vars named CC_<KEY> override GLOBAL[<key>].

    Examples:
      CC_HOLD_MINUTES=240
      CC_LATE_GUARD_ADX=32
      CC_SIGNAL_DRIFT_PCT=0.3
    """
    prefix = "CC_"
    for env_key, raw_val in os.environ.items():
        if not env_key.startswith(prefix):
            continue
        cfg_key = env_key[len(prefix):].lower()
        if cfg_key not in GLOBAL:
            continue
        try:
            original = GLOBAL[cfg_key]
            if isinstance(original, bool):
                GLOBAL[cfg_key] = raw_val.strip().lower() in (
                    "1", "true", "yes", "on"
                )
            elif isinstance(original, int):
                GLOBAL[cfg_key] = int(raw_val)
            elif isinstance(original, float):
                GLOBAL[cfg_key] = float(raw_val)
            else:
                GLOBAL[cfg_key] = raw_val
            print(f"[config_center] env override: {cfg_key} = {GLOBAL[cfg_key]}")
        except Exception as e:
            print(f"[config_center] env override failed "
                  f"{env_key}={raw_val}: {e}")


_apply_env_overrides()


# ═════════════════════════════════════════════════════════════
#  CONVENIENCE HELPERS
# ═════════════════════════════════════════════════════════════
def get_sl_atr(symbol, strategy, regime) -> float:
    return float(get_config(symbol, strategy, regime)["sl_atr"])


def get_tp1_atr(symbol, strategy, regime) -> float:
    return float(get_config(symbol, strategy, regime)["tp1_atr"])


def get_hold_minutes(symbol, strategy, regime) -> int:
    return int(get_config(symbol, strategy, regime)["hold_minutes"])


def get_min_rr(strategy: str) -> float:
    key = {
        "SUPERTREND_RIDE":  "min_rr_supertrend",
        "MEAN_REVERSION":   "min_rr_mean_reversion",
        "RANGE_SCALPER":    "min_rr_range",
        "TREND_DOWN_FADE":  "min_rr_td_fade",
    }.get(strategy, "min_rr_supertrend")
    return float(GLOBAL.get(key, 1.5))


# ═════════════════════════════════════════════════════════════
#  DIAGNOSTIC
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  CONFIG CENTER DIAGNOSTIC")
    print("=" * 70)
    print()
    samples = [
        ("POLUSDT",  "SUPERTREND_RIDE",  "TREND_DOWN"),
        ("POLUSDT",  "TREND_DOWN_FADE",  "TREND_DOWN"),
        ("POLUSDT",  "RANGE_SCALPER",    "CHOP"),
        ("NEARUSDT", "SUPERTREND_RIDE",  "VOLATILE"),
        ("APTUSDT",  "SUPERTREND_RIDE",  "TREND_UP"),
        ("WIFUSDT",  "SUPERTREND_RIDE",  "CHOP"),
    ]
    for sym, strat, reg in samples:
        print(describe(sym, strat, reg))
        print()
    print("=" * 70)
    print("  If you see this, config_center.py is WORKING ✅")
    print("=" * 70)