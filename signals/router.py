"""
family_router.py — Routes a symbol to its correct family module.

REV 19.17 (2026-09-28) — DYNAMIC ROUTING REVERTED (CRITICAL FIX):
  ✅ Reverted _resolve() to static get_family() routing.
     REV 19.16's get_family_dynamic() re-routed coins by live regime,
     but family modules' is_family_coin() guard rejects any symbol
     not in that family's FAMILY_COINS registry. Result: every coin
     whose live regime didn't match its home family was routed away
     and then immediately rejected with "not_X_coin" before any
     strategy could run.
     Live evidence (screenshot, 2026-09-28 23:41): every momentum
     coin (APT, JUP, RENDER, RUNE, VIRTUAL, ZRO) showed
     `SKIP (not_volatility_coin)` because its live regime was
     VOLATILE. Every momentum coin with regime TREND_DOWN showed
     `SKIP (not_trend_coin)`. Signal count stuck at Sig:1-2 across
     all 44 coins.
     Static routing is consistent with the is_family_coin guard —
     it was working correctly before REV 19.16.

     get_family_dynamic() remains available in coins_config.py for
     future use, but is NOT consumed by the router. To re-enable,
     ALSO fix the is_family_coin guard in families/base.py to
     accept dynamically-routed symbols (or remove the guard).

REV 19.16 (2026-09-28) — DYNAMIC FAMILY ROUTING. [superseded]
REV 19.15 (2026-09-26) — EAGER-FALLBACK FIX + CONSOLIDATION.
REV 19.14 (2026-09-25) — IDIOMATIC IMPORT.
REV 19.12 (2026-09-25) — HARDENING PASS.

ARCHITECTURE:
  family_router only maps family-STRING → family-MODULE.
  Coin → family mapping lives in coins/*.py and is read at runtime
  via coins_config.get_family(symbol).

  Four families are registered by default:
    trend_coins       — default fallback for unregistered symbols
    volatility_coins  — high-beta / high-ATR instruments
    momentum_coins    — breakout / continuation instruments
    range_coins       — mean-reverting / range-bound instruments

  To add a coin:
    1. Create coins/<name>.py
    2. Set FAMILY = "<one of the registered family strings>"
    3. Set ENABLED = True
    No changes needed in this file.

  To add a new family:
    1. Create <family>.py with the same interface
    2. Add one _safe_import() call below
    3. Add it to the FAMILIES registry loop

⚠️  INTERFACE CONTRACT — every family module MUST expose:
      FAMILY_COINS                                   → set[str]
      generate_signal_live(ind_1h, ind_4h, ind_1d,
                           symbol, mode)             → (sig, conf, reasons, lvl, div)
      route_signal(ind_1h, ind_4h, ind_1d,
                   profile, symbol, mode)            → dict | None
      is_family_coin(symbol)                         → bool

⚠️  REGIME COVERAGE — every family module must explicitly handle ALL
    regime strings emitted by the regime classifier (including
    VOLATILE). A missing regime branch silently produces zero signals
    with no rejection logged.
"""
from __future__ import annotations

import importlib

from core.coins_config import get_family, is_known

__all__ = [
    "FAMILIES", "DEFAULT_FAMILY",
    "get_family_module", "get_family_name", "is_registered",
    "family_summary", "format_family_summary",
    "generate_signal_live", "route_signal", "is_family_coin",
]


# ═════════════════════════════════════════════════════════════
#  IMPORT ALL FAMILY MODULES — each import is defensive
# ═════════════════════════════════════════════════════════════
_IMPORT_ERRORS: dict[str, str] = {}


def _safe_import(modname: str):
    """Import a family module. Returns module or None on failure."""
    try:
        return importlib.import_module(modname)
    except Exception as e:
        _IMPORT_ERRORS[modname] = f"{type(e).__name__}: {e}"
        print(f"[family_router] WARNING: failed to import {modname}: {e}")
        return None


_tc = _safe_import("signals.trend")
_vc = _safe_import("signals.volatility")
_mc = _safe_import("signals.momentum")
_rc = _safe_import("signals.range")


# ═════════════════════════════════════════════════════════════
#  FAMILY REGISTRY — maps family name → module
#  Keys must match the `family` string used in coins/*.py.
# ═════════════════════════════════════════════════════════════
FAMILIES: dict[str, object] = {}
for _key, _mod in (
    ("trend_coins",      _tc),
    ("volatility_coins", _vc),
    ("momentum_coins",   _mc),
    ("range_coins",      _rc),
):
    if _mod is not None:
        FAMILIES[_key] = _mod

DEFAULT_FAMILY = "trend_coins"


def _fallback_module():
    """
    Return the fallback family module.

    Prefers trend_coins; degrades to any registered family if
    trend_coins failed to import; returns None if ALL failed.
    """
    if DEFAULT_FAMILY in FAMILIES:
        return FAMILIES[DEFAULT_FAMILY]
    if FAMILIES:
        return next(iter(FAMILIES.values()))
    return None


# ═════════════════════════════════════════════════════════════
#  RESOLUTION HELPER — single source of truth
# ═════════════════════════════════════════════════════════════
def _resolve(symbol: str) -> tuple[str, object | None]:
    """
    Resolve a symbol to (family_name, module_or_None).

    REV 19.17 — STATIC routing (dynamic routing reverted).
      Every symbol is mapped to its home family via
      coins_config.get_family(), which reads the static FAMILY
      field from coins/*.py. This is consistent with the
      is_family_coin() guard inside each family module (which only
      accepts coins listed in that family's FAMILY_COINS set).

    Steps:
      1. coins_config.get_family(symbol) → family string
         (exceptions → fall back to DEFAULT_FAMILY)
      2. If family string is registered → use its module.
      3. Otherwise → fall back to _fallback_module().

    Returns:
      (family_name_string, module_or_None)
      family_name is always a non-empty string.
    """
    # ── Step 1: family string (STATIC) ──
    if symbol:
        try:
            family_name = get_family(symbol)
        except Exception as e:
            print(f"[family_router] get_family({symbol!r}) raised: "
                  f"{type(e).__name__}: {e}")
            family_name = DEFAULT_FAMILY
    else:
        family_name = DEFAULT_FAMILY

    # ── Step 2 + 3: module lookup with explicit fallback ──
    # (avoids eager _fallback_module() evaluation in dict.get)
    module = FAMILIES.get(family_name)
    if module is None:
        module = _fallback_module()
        # Report canonical fallback name, not the unregistered lookup key
        family_name = DEFAULT_FAMILY if DEFAULT_FAMILY in FAMILIES else family_name

    return family_name, module


# ═════════════════════════════════════════════════════════════
#  PUBLIC HELPERS
# ═════════════════════════════════════════════════════════════
def get_family_module(symbol: str):
    """
    Return the family module OBJECT for a symbol.

    Never raises. Returns None only when NO family module could be
    imported at all (in which case FAMILIES is empty).
    """
    _, module = _resolve(symbol)
    return module


def get_family_name(symbol: str) -> str:
    """
    Return the family NAME (string) for a symbol.

    Always returns a registered family name, or DEFAULT_FAMILY if
    even trend_coins failed to import.
    """
    family_name, _ = _resolve(symbol)
    return family_name


def is_registered(symbol: str) -> bool:
    """True if symbol is in coins_config (known universe)."""
    if not symbol:
        return False
    try:
        return bool(is_known(symbol))
    except Exception:
        return False


def family_summary() -> dict:
    """
    Return summary of all families + coin counts.

    FAMILY_COINS only contains ENABLED coins. A family with count 0 may
    still have disabled coins registered in coins/.
    """
    out = {}
    for name, module in FAMILIES.items():
        try:
            coins = getattr(module, "FAMILY_COINS", set()) or set()
        except Exception:
            coins = set()
        try:
            coins_sorted = sorted(coins)
        except Exception:
            coins_sorted = list(coins)
        out[name] = {
            "coins":  coins_sorted,
            "count":  len(coins),
            "active": len(coins) > 0,
        }
    return out


def format_family_summary() -> str:
    """Pretty-print family breakdown for logs."""
    summary = family_summary()
    lines = []
    total = 0
    for fam, info in summary.items():
        total += info["count"]
        marker = "" if info["active"] else "  (unused)"
        lines.append(f"  {fam:<20} {info['count']:>2} coins{marker}")
    lines.append(f"  {'TOTAL':<20} {total:>2} coins")
    if _IMPORT_ERRORS:
        lines.append("")
        lines.append("  ⚠️  Import errors:")
        for k, v in _IMPORT_ERRORS.items():
            lines.append(f"     {k}: {v}")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════
#  MAIN ROUTER — same interface as any family module
# ═════════════════════════════════════════════════════════════
def generate_signal_live(ind_1h, ind_4h, ind_1d=None,
                         symbol="", mode="MULTI"):
    """
    Route to correct family module and return signal.

    Args:
      mode: strategy mode hint. "MULTI" (default) or None — both
            accepted; family modules treat None as default.

    Returns (sig, conf, reasons, lvl, div).
    """
    empty_lvl = {"SL": 0.0, "TP1": 0.0, "TP2": 0.0, "Qty": 0.0, "RR": 0.0}

    module = get_family_module(symbol)

    if module is None:
        return "NEUTRAL", 0.0, ["no_family_module_available"], empty_lvl, "NONE"

    mod_name = getattr(module, "__name__", "unknown")
    try:
        return module.generate_signal_live(
            ind_1h, ind_4h, ind_1d,
            symbol=symbol,
            mode=mode,
        )
    except AttributeError as e:
        print(f"[ROUTER] {mod_name} missing generate_signal_live: {e}")
        return "NEUTRAL", 0.0, [f"router_error_{mod_name}"], empty_lvl, "NONE"
    except Exception as e:
        print(f"[ROUTER] {mod_name} raised {type(e).__name__}: {e}")
        return "NEUTRAL", 0.0, [f"router_exception_{type(e).__name__}"], empty_lvl, "NONE"


def route_signal(ind_1h, ind_4h, ind_1d=None,
                 profile: str = "MIXED", symbol: str = "",
                 mode: str = None):
    """
    Same shape as family modules' route_signal() — returns dict or None.

    Args:
      mode: strategy mode hint. None (default) is passed through to
            the family module, which treats it as "use defaults".
    """
    module = get_family_module(symbol)
    if module is None:
        return None
    try:
        return module.route_signal(
            ind_1h, ind_4h, ind_1d,
            profile=profile, symbol=symbol, mode=mode,
        )
    except Exception as e:
        print(f"[ROUTER] route_signal error for {symbol}: "
              f"{type(e).__name__}: {e}")
        return None


def is_family_coin(symbol: str) -> bool:
    """True if symbol is registered AND its family has it as an active coin."""
    if not symbol:
        return False
    module = get_family_module(symbol)
    if module is None:
        return False
    try:
        return bool(module.is_family_coin(symbol))
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════
#  DIAGNOSTIC — fully dynamic, no hardcoded coins
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    from core.coins_config import enabled_coins, all_coins

    print("=" * 70)
    print("  FAMILY ROUTER DIAGNOSTIC  (static — no hardcoded coins)")
    print("=" * 70)

    # ── 0. Import errors ──
    if _IMPORT_ERRORS:
        print("\n⚠️  Family import errors:")
        for k, v in _IMPORT_ERRORS.items():
            print(f"     {k}: {v}")
    else:
        print("\n✅ All family modules imported cleanly.")

    # ── 1. Family breakdown ──
    print("\nFamily summary:")
    print(format_family_summary())

    # ── 2. Every ENABLED coin ──
    print("\nEnabled coins  (source: coins_config.enabled_coins()):")
    try:
        registered = list(enabled_coins())
    except Exception as e:
        print(f"  ⚠️  enabled_coins() failed: {e}")
        registered = []
    if not registered:
        print("  ⚠️  No enabled coins found in coins/")
    else:
        for sym in registered:
            fam = get_family_name(sym)
            print(f"  ✅ {sym:<16} → {fam}")

    # ── 3. Edge cases ──
    print("\nEdge cases (derived from coins_config):")

    try:
        all_syms = set(all_coins().keys())
    except Exception as e:
        print(f"  ⚠️  all_coins() failed: {e}")
        all_syms = set()

    enabled_set = set(registered)
    disabled_syms = sorted(all_syms - enabled_set)

    if disabled_syms:
        for sym in disabled_syms:
            fam = get_family_name(sym)
            print(f"  ⚠️  {sym:<16} → {fam:<20} registered=True, DISABLED")
    else:
        print("  (no disabled coins registered)")

    # 3b. Unregistered coin (guaranteed non-existent)
    fake = "ZZZFAKEUSDT"
    while fake in all_syms:
        fake = "Z" + fake
    fake_fam = get_family_name(fake)
    print(f"  ❌ {fake:<16} → (fallback: {fake_fam})  "
          f"registered={is_registered(fake)}")

    # 3c. Empty symbol — actually CALL the router
    empty_fam = get_family_name("")
    empty_mod = get_family_module("")
    empty_mod_name = getattr(empty_mod, "__name__", None)
    print(f"  ❓ {'(empty)':<16} → family={empty_fam}, module={empty_mod_name}")

    # 3d. Normalization sanity check (coins_config REV 19.13)
    print("\nNormalization sanity check (coins_config REV 19.13):")
    if all_syms:
        sample = sorted(all_syms)[0]
        bare = sample[:-4] if sample.endswith("USDT") else sample
        lower = sample.lower()
        for probe, label in ((sample, "canonical"),
                             (bare,  "bare"),
                             (lower, "lowercase")):
            fam = get_family_name(probe)
            reg = is_registered(probe)
            print(f"  {label:<10} {probe:<12} → family={fam:<20} "
                  f"registered={reg}")
    else:
        print("  (no coins loaded — skipped)")

    # ── 4. Family module interface check ──
    print("\nFamily module interface check:")
    for fam_name, module in FAMILIES.items():
        has_gen   = hasattr(module, "generate_signal_live")
        has_route = hasattr(module, "route_signal")
        has_is_fc = hasattr(module, "is_family_coin")
        has_fc    = hasattr(module, "FAMILY_COINS")
        ok = has_gen and has_route and has_is_fc and has_fc
        marker = "✅" if ok else "❌"
        print(f"  {marker} {fam_name:<20} "
              f"gen={has_gen}  route={has_route}  "
              f"is_fc={has_is_fc}  FAMILY_COINS={has_fc}")

    print("\n" + "=" * 70)