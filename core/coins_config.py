"""
coins_config.py — Wrapper around coins/ directory.

REV 21.7 (2026-09-30) — FALLBACK RR FIX:
  ✅ `get_coin_st_params()` fallback now returns tp1_atr = 1.5 × sl_atr
     (was equal to sl_atr → RR 1.0 → every unknown coin silently
     rejected by the _mk() RR floor). New fallback gives RR 1.5.

REV 19.16 (2026-09-29) — DEAD DYNAMIC ROUTING REMOVED:
  ✅ Removed `get_family_dynamic()` and its `_REGIME_TO_FAMILY` table.
     This was added in REV 19.15 but immediately reverted by
     family_router REV 19.17 — the family modules' `is_family_coin()`
     guard rejects any symbol not in that family's FAMILY_COINS
     registry, so dynamic routing caused every re-routed coin to be
     rejected before any strategy could run (signal count dropped to
     1-2 across 44 coins). Since REV 19.17, family_router._resolve()
     uses static get_family() exclusively — get_family_dynamic() has
     had ZERO consumers since. Removing it eliminates ~35 lines of
     dead, confusing code.

REV 19.14 (2026-09-26) — GET_CAPS() FALLBACK RATIO FIX.
REV 19.13 (2026-09-26) — NORMALIZE WIRING + PUBLIC SURFACE.
REV 19.12 (2026-09-25) — DYNAMIC DIAGNOSTIC + LOAD-ERROR SURFACING.
REV 19.0 (2026-09-22) — FILE-PER-COIN ARCHITECTURE.
"""
from __future__ import annotations

from coins import get_coin, all_coins, load_errors as _coin_load_errors

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

    Note: non-USDT quotes (e.g. "BTCUSDC") are not handled by design —
    the coin registry is USDT-only.
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


def get_caps(symbol: str) -> dict:
    """
    Return per-coin caps dict: {"sl": pct, "tp1": pct, "tp2": pct}.

    Fallback fires only on a double miss:
      • coin not in registry / exception, AND
      • family module also supplied no caps.

    Fallback ratios use the MED canonical TP1/SL (1.5x) and TP2/SL
    (2.5x) — see REV 19.14.
    """
    try:
        c = get_coin(_normalize(symbol))
        if c:
            return dict(c["caps"])
    except Exception:
        pass
    # Last-resort safety net — MED canonical ratios (1.5x / 2.5x).
    return {"sl": 0.045, "tp1": 0.0675, "tp2": 0.1125}


# ═══════════════════════════════════════════════════════════
#  REGISTRY-LEVEL ACCESSORS (no symbol arg → no normalize)
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
#  PER-COIN EXTRAS (used by family modules)
# ═══════════════════════════════════════════════════════════
def get_coin_filters(symbol: str) -> dict:
    """Return per-coin FILTERS dict, or {} if unknown."""
    try:
        c = get_coin(_normalize(symbol))
        return dict(c["filters"]) if c else {}
    except Exception:
        return {}


def get_coin_st_params(symbol: str) -> dict:
    """
    Return per-coin ST_PARAMS, or family-agnostic default.

    REV 21.7 — fallback now produces RR 1.5 (was 1.0).
    Old values (2.5 / 2.5) gave tp1_atr == sl_atr → RR 1.0 →
    every unknown coin silently rejected. New values give
    tp1_atr = 1.5 * sl_atr → RR 1.5.
    """
    try:
        c = get_coin(_normalize(symbol))
        if c:
            return dict(c["st_params"])
    except Exception:
        pass
    # REV 21.7 — RR 1.5 fallback.
    return {"sl_atr": 2.5, "tp1_atr": 3.75, "tp2_atr": 6.25}


def get_coin_vol_class(symbol: str) -> str:
    """Return volatility class label, or 'MED' if unknown."""
    try:
        c = get_coin(_normalize(symbol))
        return c["vol_class"] if c else "MED"
    except Exception:
        return "MED"


def get_coin_td_fade(symbol: str) -> dict:
    """Return per-coin TD_FADE params, or {} if unknown."""
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
    loaded coin registry (e.g. ['momentum_coins', 'range_coins', ...])."""
    try:
        return sorted({c["family"] for c in all_coins().values()})
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTIC — fully dynamic
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  COINS_CONFIG DIAGNOSTIC — REV 19.16 (file-per-coin)")
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

    # ── Sanity check: normalization ──
    print("\nNormalization sanity check:")
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