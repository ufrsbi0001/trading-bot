"""
core/coins_config.py — Wrapper around coins/ directory.

REV 23.1 (2026-10-02) — FALLBACK VALUE ALIGNMENT:
  ✅ Emergency fallback values (used only when `config_center` import
     fails — practically never) now match `config_center.GLOBAL`
     defaults:
       • ST_PARAMS fallback: sl_atr=1.8, tp1_atr=2.5, tp2_atr=5.0
         (was stale 2.5 / 3.75 / 6.25)
       • CAPS fallback: sl=0.045, tp1=0.0675, tp2=0.125
         (matches trend_coins, the DEFAULT_FAMILY)
       • `get_caps()` fallback aligned to same values
       • `get_coin_st_params()` fallback aligned to same values
     Prevents silent behavior drift in the pathological case where
     config_center fails to import.
  ✅ Zero behaviour change for the normal path (config_center available).

REV 23.0 (2026-10-02) — UNIFIED CONFIG SOURCE:
  ✅ Migrated from core/family_baselines.py → core/config_center.py.
     All family baselines (ST_PARAMS, CAPS, FILTERS) and vol class
     multipliers now come from config_center.py — the single source
     of truth for the entire system.

REV 22.0 (2026-10-02) — FAMILY-LEVEL BASELINES.
REV 21.7 (2026-09-30) — FALLBACK RR FIX.
REV 19.16 (2026-09-29) — DEAD DYNAMIC ROUTING REMOVED.
REV 19.14 (2026-09-26) — GET_CAPS() FALLBACK RATIO FIX.
REV 19.13 (2026-09-26) — NORMALIZE WIRING + PUBLIC SURFACE.
REV 19.0  (2026-09-22) — FILE-PER-COIN ARCHITECTURE.
"""
from __future__ import annotations

from coins import get_coin, all_coins, load_errors as _coin_load_errors

# ── REV 23.0 — Unified config source (single source of truth) ──
# ── REV 23.1 — Fallback values aligned to config_center.GLOBAL defaults ──
try:
    from core.config_center import (
        get_baseline as _get_family_baseline,
        get_vol_class_mult as _get_vol_class_mult,
        VOL_CLASS_CAP_MULT as _VOL_CLASS_CAP_MULT,
    )
except ImportError:
    # ── EMERGENCY FALLBACK ONLY ──
    # Values mirror core/config_center.GLOBAL / FAMILY[trend_coins].
    # This block should NEVER execute in practice — config_center is
    # a core dependency. Kept for defensive import safety.
    _FALLBACK_ST_PARAMS = {"sl_atr": 1.8, "tp1_atr": 2.5, "tp2_atr": 5.0}
    _FALLBACK_CAPS      = {"sl": 0.045, "tp1": 0.0675, "tp2": 0.125}

    def _get_family_baseline(family):  # pragma: no cover
        return {
            "ST_PARAMS": dict(_FALLBACK_ST_PARAMS),
            "CAPS":      dict(_FALLBACK_CAPS),
            "FILTERS":   {},
        }

    def _get_vol_class_mult(vcls):  # pragma: no cover
        return 1.0

    _VOL_CLASS_CAP_MULT = {"LOW": 1.0, "MED": 1.0, "HIGH": 1.0}


__all__ = [
    # ── Symbol-taking accessors ──
    "get_profile", "get_family",
    "is_enabled", "is_known",
    "get_caps",
    "get_coin_filters", "get_coin_st_params", "get_coin_vol_class",
    "get_coin_td_fade",
    # ── Registry-level accessors ──
    "get_family_coins", "enabled_coins",
    # ── Diagnostics ──
    "load_errors", "known_families",
    # ── Re-exports (used by family_router.py) ──
    "get_coin", "all_coins",
    # ── REV 22.0 — Direct baseline access ──
    "get_vol_class_mult",
]


# ═══════════════════════════════════════════════════════════
#  SYMBOL NORMALIZATION
# ═══════════════════════════════════════════════════════════
def _normalize(symbol: str) -> str:
    """
    Canonical form for coin lookups: uppercase + USDT suffix.

    Handles:
      "btc"       → "BTCUSDT"
      "btcusdt"   → "BTCUSDT"
      "BTCUSDT"   → "BTCUSDT"
      ""          → ""    (get_coin returns None → defaults)
    """
    if not symbol:
        return ""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    return sym


# ═══════════════════════════════════════════════════════════
#  CORE ACCESSORS — all wrapped defensively
# ═══════════════════════════════════════════════════════════
def get_profile(symbol: str) -> str:
    try:
        c = get_coin(_normalize(symbol))
        return c["profile"] if c else "MIXED"
    except Exception:
        return "MIXED"


def get_family(symbol: str) -> str:
    """Static family assignment from the coins/*.py registry."""
    try:
        c = get_coin(_normalize(symbol))
        return c["family"] if c else "trend_coins"
    except Exception:
        return "trend_coins"


def is_enabled(symbol: str) -> bool:
    try:
        c = get_coin(_normalize(symbol))
        return bool(c["enabled"]) if c else False
    except Exception:
        return False


def is_known(symbol: str) -> bool:
    try:
        return get_coin(_normalize(symbol)) is not None
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
#  FAMILY BASELINE ACCESSORS (REV 23.0 — from config_center)
# ═══════════════════════════════════════════════════════════
def get_caps(symbol: str) -> dict:
    """
    Return family baseline caps dict: {"sl": pct, "tp1": pct, "tp2": pct}.

    REV 23.0 — Reads from core/config_center.py.

    Note: caps are the MAX allowed distances. Actual SL/TP are
    computed by strategy functions (using ATR × st_params), then
    clamped to these caps.

    REV 23.1 — Emergency fallback aligned to trend_coins CAPS
    (matches config_center.FAMILY[DEFAULT_FAMILY]["CAPS"]).
    """
    try:
        family = get_family(symbol)
        baseline = _get_family_baseline(family)
        return dict(baseline["CAPS"])
    except Exception:
        return {"sl": 0.045, "tp1": 0.0675, "tp2": 0.125}


def get_coin_filters(symbol: str) -> dict:
    """
    Return family baseline FILTERS dict.

    REV 23.0 — Reads from core/config_center.py.

    This includes: late_guard_*, top_chase_*, max_flips, min_flips,
    min_adx_st, min_dist_atr, max_dist_atr, pullback_dist_atr,
    rsi_buy_min/max, rsi_sell_min/max.
    """
    try:
        family = get_family(symbol)
        baseline = _get_family_baseline(family)
        return dict(baseline["FILTERS"])
    except Exception:
        return {}


def get_coin_st_params(symbol: str) -> dict:
    """
    Return family baseline ST_PARAMS dict.

    REV 23.0 — Reads from core/config_center.py.

    Returns {sl_atr, tp1_atr, tp2_atr} — the ATR multipliers for
    SL and TP distances. Actual distances computed as ATR × multiplier.

    REV 23.1 — Emergency fallback aligned to config_center.GLOBAL
    defaults (sl_atr=1.8, tp1_atr=2.5, tp2_atr=5.0).
    """
    try:
        family = get_family(symbol)
        baseline = _get_family_baseline(family)
        return dict(baseline["ST_PARAMS"])
    except Exception:
        return {"sl_atr": 1.8, "tp1_atr": 2.5, "tp2_atr": 5.0}


def get_vol_class_mult(vol_class: str) -> float:
    """
    Return cap multiplier for a vol class.

    REV 23.0 — Reads from core/config_center.py.

    Used by signals/base.py::_mk() to adjust caps:
      LOW  → 0.80 (majors need tighter caps)
      MED  → 1.00 (baseline)
      HIGH → 1.40 (wild alts need wider caps)
    """
    return _get_vol_class_mult(vol_class)


# ═══════════════════════════════════════════════════════════
#  REGISTRY-LEVEL ACCESSORS
# ═══════════════════════════════════════════════════════════
def get_family_coins(family: str) -> set[str]:
    """Return {symbol, ...} of ENABLED coins belonging to `family`."""
    try:
        return {
            sym for sym, c in all_coins().items()
            if c["family"] == family and c["enabled"]
        }
    except Exception:
        return set()


def enabled_coins() -> tuple[str, ...]:
    """Return tuple of all ENABLED symbols, in registry order."""
    try:
        return tuple(sym for sym, c in all_coins().items() if c["enabled"])
    except Exception:
        return tuple()


# ═══════════════════════════════════════════════════════════
#  PER-COIN EXTRAS
# ═══════════════════════════════════════════════════════════
def get_coin_vol_class(symbol: str) -> str:
    """Return volatility class label, or 'MED' if unknown."""
    try:
        c = get_coin(_normalize(symbol))
        return c["vol_class"] if c else "MED"
    except Exception:
        return "MED"


def get_coin_td_fade(symbol: str) -> dict:
    """
    Return per-coin TD_FADE params, or {} if unknown.

    NOTE: TD_FADE is strategy-specific (falling-knife BUY logic),
    so it stays per-coin. All OTHER tuning comes from config_center.
    """
    try:
        c = get_coin(_normalize(symbol))
        return dict(c["td_fade"]) if c else {}
    except Exception:
        return {}


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTIC HELPERS
# ═══════════════════════════════════════════════════════════
def load_errors() -> dict[str, str]:
    """Return {coin_module_name: error_message} for files that failed
    to load during registry initialization."""
    try:
        return dict(_coin_load_errors())
    except Exception:
        return {}


def known_families() -> list[str]:
    """Return sorted list of distinct family NAMES present in the
    loaded coin registry."""
    try:
        return sorted({c["family"] for c in all_coins().values()})
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTIC — REV 23.1
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  COINS_CONFIG DIAGNOSTIC — REV 23.1 (unified config)")
    print("=" * 70)

    errs = load_errors()
    if errs:
        print(f"\n⚠️  {len(errs)} load error(s):")
        for k, v in sorted(errs.items()):
            print(f"    {k}: {v}")
    else:
        print("\n✅ No coin-file load errors.")

    all_c = all_coins()
    print(f"\nLoaded {len(all_c)} coins:")
    for sym in sorted(all_c):
        c = all_c[sym]
        status = "✅" if c["enabled"] else "❌"
        print(f"  {status} {sym:<16} {c['family']:<18} "
              f"{c['profile']:<9} vol={c['vol_class']:<5}")

    print(f"\nEnabled: {len(enabled_coins())}")

    fams = known_families()
    if not fams:
        print("\n⚠️  No families found — check coins/__init__.py loader.")
    else:
        print("\nFamily breakdown (dynamic):")
        for fam in fams:
            coins = get_family_coins(fam)
            print(f"  {fam:<20} ({len(coins):>2}) → {sorted(coins)}")

    # ── REV 23.0 — Baseline preview ──
    print("\n" + "=" * 70)
    print("  FAMILY BASELINE PREVIEW (REV 23.1 — from config_center)")
    print("=" * 70)
    for fam in fams:
        baseline = _get_family_baseline(fam)
        print(f"\n  {fam}:")
        print(f"    ST_PARAMS: {baseline['ST_PARAMS']}")
        print(f"    CAPS:      {baseline['CAPS']}")
        print(f"    FILTERS:   {len(baseline['FILTERS'])} keys")

    # ── Sample: Verify baseline is served ──
    print("\n" + "=" * 70)
    print("  SAMPLE LOOKUP (should serve from config_center)")
    print("=" * 70)
    for sym in ("APTUSDT", "BTCUSDT", "HYPEUSDT"):
        if sym in all_c:
            print(f"\n  {sym}:")
            print(f"    family:     {get_family(sym)}")
            print(f"    vol_class:  {get_coin_vol_class(sym)}")
            print(f"    ST_PARAMS:  {get_coin_st_params(sym)}")
            print(f"    CAPS:       {get_caps(sym)}")
            print(f"    FILTERS:    {len(get_coin_filters(sym))} keys")

    # ── Normalization sanity check ──
    print("\n" + "=" * 70)
    print("  NORMALIZATION SANITY CHECK")
    print("=" * 70)
    if all_c:
        sample = sorted(all_c)[0]
        bare = sample[:-4] if sample.endswith("USDT") else sample
        lower = sample.lower()
        for probe, label in ((sample, "canonical"),
                             (bare,  "bare"),
                             (lower, "lowercase")):
            fam = get_family(probe)
            prof = get_profile(probe)
            print(f"  {label:<10} {probe:<12} → family={fam:<18} "
                  f"profile={prof}")
    else:
        print("  (no coins loaded — skipped)")