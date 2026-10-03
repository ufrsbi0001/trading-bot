"""
audit_legacy_config_reads.py — CI-ready audit for legacy CONFIG reads
and dead symbols.

USAGE
─────
    # Report-only (writes file, exits 0 always) — for humans
    python scripts/audit_legacy_config_reads.py --report

    # Strict (exits 1 on any non-whitelisted hit) — for CI
    python scripts/audit_legacy_config_reads.py --strict

    # JSON output for CI parsing
    python scripts/audit_legacy_config_reads.py --strict --format json

    # Quiet mode (only exit code matters)
    python scripts/audit_legacy_config_reads.py --strict --quiet

    # Custom output file
    python scripts/audit_legacy_config_reads.py --report --output /tmp/audit.txt

WHAT IT CHECKS
──────────────
1. Legacy `CONFIG.<trading_param>` reads — should use CC.get() instead.
2. Dead symbols (removed functions, unused constants) — should be ZERO.

WHITELISTED (won't fail in --strict)
────────────────────────────────────
• Path whitelist (path-suffix):
    core/config.py — this file DEFINES the proxy; its
      `_PROXIED_TRADING_ATTRS` contains trading param names as
      strings, not actual reads.
    scripts/audit_*.py / fix_*.py / verify_*.py — self-references.
• (file, symbol) pair whitelist:
    core/config_center.py + {COOLDOWN_AFTER_SL_MIN, COOLDOWN_AFTER_TP_MIN}
      — these are legacy env-var NAMES exposed as string literals in
      `_LEGACY_ENV_MAP` for backward compat. Not dead code.
• Any line inside a docstring (triple-quoted string).
• Any code after an inline `#` comment.

EXIT CODES
──────────
  0 — clean (or --report mode regardless)
  1 — findings in --strict mode
  2 — setup error (run from wrong directory, unreadable path, etc.)

REV 2.1 (2026-10-03) — (file, symbol) PAIR WHITELIST:
  ✅ Added `_WHITELIST_FILE_SYMBOL_PAIRS` for intentional string
     literals that would otherwise trip `DEAD_SYMBOLS`.
     First entries: `core/config_center.py`'s `_LEGACY_ENV_MAP`
     exposes `COOLDOWN_AFTER_SL_MIN` / `COOLDOWN_AFTER_TP_MIN` as
     legacy env-var names — these are NOT dead code, they are the
     backward-compat contract that keeps old `.env` files working.
  ✅ New `_is_symbol_whitelisted_in_file()` helper — checks
     (resolved_path_tail, symbol) against the pair whitelist.
  ✅ `_scan_file()` now consults the pair whitelist before recording
     a dead-symbol hit. Zero behaviour change for real dead symbols.
  ✅ --strict now exits 0 on a clean repo (was 1 due to the two
     false positives).

REV 2.0 (2026-10-03) — CI-READY:
  ✅ Exit codes 0/1/2 for CI integration.
  ✅ --strict vs --report mode.
  ✅ --format text|json.
  ✅ --quiet for shell pipelines.
  ✅ --output PATH (default: no file written).
  ✅ Docstring-aware scanning (triple-quote state tracking).
  ✅ Inline-comment-aware (strips trailing # ... before matching).
  ✅ Path-based whitelist (config.py proxy, scripts/audit_*.py).
  ✅ Absolute-path self-skip (was: name-based — collision prone).
  ✅ JSON output with full structured findings.

REV 1.0 — initial release.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from collections import defaultdict


# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════

# Trading params that MUST come from config_center, not CONFIG.
LEGACY_TRADING_PARAMS = [
    "leverage",
    "risk_percent",
    "max_open_positions",
    "max_same_side_positions",
    "max_daily_loss_trades",
    "max_daily_drawdown_percent",
    "max_account_drawdown",
    "max_hold_minutes",
    "min_confidence",
    "min_adx",
    "require_htf_agreement",
    "use_5m_trend_filter",
    "use_1m_trend_filter",          # dead rename — should be zero hits
    "max_trades_per_coin_per_day",
    "htf_align_relaxed",
    "cooldown_after_sl_min",
    "cooldown_after_tp_min",
    "partial_close_usdt",
    "use_taker_volume",
    "use_volume_profile",
    "use_anchored_vwap",
    "use_funding_z",
    "use_mtf_confluence",
    "kz_bypass",
    "use_spread_filter",
    "max_spread_pct",
    "max_spread_trend",
    "max_spread_range",
    "max_spread_volatility",
]

# Dead symbols to hunt (should be ZERO hits).
DEAD_SYMBOLS = [
    "get_futures_sentiment",
    "get_liquidation_pressure",
    "COOLDOWN_AFTER_SL_MIN",
    "COOLDOWN_AFTER_TP_MIN",
    "use_1m_trend_filter",
]

# Directories to skip entirely.
SKIP_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", ".env",
    "node_modules", ".pytest_cache", ".mypy_cache", "build", "dist",
    ".idea", ".vscode", "migrations",
}

# Files that are EXPECTED to reference the trading-param names as
# strings (the proxy contract itself, audit scripts, etc.).
# Matched by absolute resolved path suffix.
WHITELIST_PATH_SUFFIXES = (
    "core/config.py",                       # proxy contract definition
    "scripts/audit_legacy_config_reads.py",
    "scripts/audit_config_overlaps.py",
    "scripts/audit_config_consumers.py",
    "scripts/fix_config_consumers.py",
    "scripts/verify_dead_code.py",
)

# (relative_path_suffix, symbol) pairs that are INTENTIONALLY present.
# These are legacy env-var NAMES or backward-compat shims, not dead
# code reads. Matched against the resolved absolute path's tail so it
# works regardless of the repo root location.
#
# REV 2.1 — config_center._LEGACY_ENV_MAP exposes these names as
# string literals so old `.env` files keep working after the
# `MAX_HOLD_MINUTES` → `hold_minutes` style renames and the
# cooldown-constant migration to config_center.
_WHITELIST_FILE_SYMBOL_PAIRS = frozenset({
    ("core/config_center.py", "COOLDOWN_AFTER_SL_MIN"),
    ("core/config_center.py", "COOLDOWN_AFTER_TP_MIN"),
})


# ═══════════════════════════════════════════════════════════════
#  REGEX PRECOMPILE
# ═══════════════════════════════════════════════════════════════
_param_alt = "|".join(re.escape(p) for p in LEGACY_TRADING_PARAMS)
CONFIG_READ_RE = re.compile(rf"\bCONFIG\.({_param_alt})\b")

DEAD_RE = re.compile(
    r"\b(" + "|".join(re.escape(s) for s in DEAD_SYMBOLS) + r")\b"
)

# Strip trailing inline comments before matching: `x = 1  # comment`
_INLINE_COMMENT_RE = re.compile(r"#.*$")

# Triple-quote markers for docstring tracking.
_TRIPLE_QUOTE_RE = re.compile(r'("""|\'\'\')')


# ═══════════════════════════════════════════════════════════════
#  SCANNER
# ═══════════════════════════════════════════════════════════════
def _resolved_posix(p: Path) -> str:
    """Return the resolved path as a forward-slash string."""
    try:
        resolved = p.resolve()
    except OSError:
        resolved = p
    return str(resolved).replace("\\", "/")


def _is_whitelisted(p: Path) -> bool:
    """Path-suffix whitelist (absolute path, not name)."""
    rel = _resolved_posix(p)
    return any(rel.endswith(sfx) for sfx in WHITELIST_PATH_SUFFIXES)


def _is_symbol_whitelisted_in_file(p: Path, symbol: str) -> bool:
    """
    Check if (file, symbol) is an intentional whitelisted pair
    (e.g., legacy env-var names exposed as string literals).

    REV 2.1 — compares the resolved path's tail so it works regardless
    of the absolute repo root.
    """
    rel = _resolved_posix(p)
    for rel_path, whitelisted_sym in _WHITELIST_FILE_SYMBOL_PAIRS:
        if whitelisted_sym == symbol and rel.endswith(rel_path):
            return True
    return False


def _should_skip_dir(p: Path) -> bool:
    return any(part in SKIP_DIRS for part in p.parts)


def _strip_inline_comment(line: str) -> str:
    """Remove everything after `#` — but NOT inside string literals.

    Simple heuristic: if the stripped line contains an odd number of
    quotes before the `#`, the `#` is likely inside a string. We bail
    out and return the line as-is in that case (defensive — better to
    scan a comment than miss a real hit).
    """
    idx = line.find("#")
    if idx < 0:
        return line
    head = line[:idx]
    # Count quote chars — if odd, we're likely inside a string.
    if (head.count('"') + head.count("'")) % 2 == 1:
        return line  # bail — treat as code
    return head


def _scan_file(p: Path) -> tuple[list, list]:
    """Return (legacy_hits, dead_hits) for one file."""
    legacy_hits: list[tuple[int, str, str]] = []
    dead_hits:   list[tuple[int, str, str]] = []

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"  [skip] {p}: {e}", file=sys.stderr)
        return legacy_hits, dead_hits

    in_docstring = False
    docstring_delim = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        # ── Track triple-quote state ──
        # A line can both close and re-open a docstring; process all
        # occurrences on the line to keep state correct.
        for m in _TRIPLE_QUOTE_RE.finditer(raw):
            if not in_docstring:
                in_docstring = True
                docstring_delim = m.group(1)
            elif m.group(1) == docstring_delim:
                in_docstring = False
                docstring_delim = None

        if in_docstring:
            continue

        stripped = raw.strip()
        if not stripped:
            continue

        # Skip pure-comment lines.
        if stripped.startswith("#"):
            continue

        # Strip trailing inline comments before matching.
        code_part = _strip_inline_comment(raw)
        if not code_part.strip():
            continue

        m = CONFIG_READ_RE.search(code_part)
        if m:
            legacy_hits.append((lineno, m.group(1), stripped))

        d = DEAD_RE.search(code_part)
        if d and not _is_symbol_whitelisted_in_file(p, d.group(1)):
            dead_hits.append((lineno, d.group(1), stripped))

    return legacy_hits, dead_hits


def scan(root: Path) -> tuple[dict, dict, int]:
    """Scan all .py files under root.

    Returns (legacy_hits, dead_hits, file_count).
    legacy_hits / dead_hits: {Path: [(lineno, symbol, line), ...]}
    """
    legacy_hits: dict[Path, list] = defaultdict(list)
    dead_hits:   dict[Path, list] = defaultdict(list)

    py_files = [
        p for p in root.rglob("*.py")
        if not _should_skip_dir(p) and not _is_whitelisted(p)
    ]

    for p in py_files:
        lh, dh = _scan_file(p)
        if lh:
            legacy_hits[p] = lh
        if dh:
            dead_hits[p] = dh

    return legacy_hits, dead_hits, len(py_files)


# ═══════════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════════
def _build_summary(legacy_hits: dict, dead_hits: dict, file_count: int) -> dict:
    return {
        "file_count": file_count,
        "legacy_reads": {
            "total": sum(len(v) for v in legacy_hits.values()),
            "files_affected": len(legacy_hits),
            "by_file": {
                str(p): [
                    {"line": ln, "param": param, "text": txt}
                    for ln, param, txt in rows
                ]
                for p, rows in legacy_hits.items()
            },
        },
        "dead_symbols": {
            "total": sum(len(v) for v in dead_hits.values()),
            "files_affected": len(dead_hits),
            "by_file": {
                str(p): [
                    {"line": ln, "symbol": sym, "text": txt}
                    for ln, sym, txt in rows
                ]
                for p, rows in dead_hits.items()
            },
        },
    }


def _write_text_report(out_path: Path, summary: dict) -> None:
    with out_path.open("w", encoding="utf-8") as f:
        f.write("=" * 78 + "\n")
        f.write("  LEGACY CONFIG.<trading_param> READS\n")
        f.write("  → migrate to: from core import config_center as CC; CC.get('<key>')\n")
        f.write("=" * 78 + "\n\n")
        lr = summary["legacy_reads"]
        f.write(f"Total matches: {lr['total']}  |  "
                f"Files affected: {lr['files_affected']}\n\n")

        for p_str in sorted(lr["by_file"].keys()):
            rows = lr["by_file"][p_str]
            f.write(f"\n── {p_str}  ({len(rows)} hits)\n")
            for r in rows:
                f.write(f"   L{r['line']:<5}  CONFIG.{r['param']:<28}  "
                        f"|  {r['text']}\n")

        f.write("\n\n" + "=" * 78 + "\n")
        f.write("  DEAD SYMBOLS (should be ZERO hits)\n")
        f.write("=" * 78 + "\n\n")
        ds = summary["dead_symbols"]
        f.write(f"Total matches: {ds['total']}  |  "
                f"Files affected: {ds['files_affected']}\n\n")

        for p_str in sorted(ds["by_file"].keys()):
            rows = ds["by_file"][p_str]
            f.write(f"\n── {p_str}  ({len(rows)} hits)\n")
            for r in rows:
                f.write(f"   L{r['line']:<5}  {r['symbol']:<32}  "
                        f"|  {r['text']}\n")


def _print_console_summary(summary: dict, quiet: bool) -> None:
    if quiet:
        return
    lr = summary["legacy_reads"]
    ds = summary["dead_symbols"]
    print(f"\nScanned {summary['file_count']} Python files.")
    print(f"  Legacy CONFIG reads : {lr['total']} "
          f"({lr['files_affected']} files)")
    print(f"  Dead symbols        : {ds['total']} "
          f"({ds['files_affected']} files)")

    if lr["total"]:
        print("\nLegacy CONFIG reads found:")
        top = sorted(lr["by_file"].items(), key=lambda kv: -len(kv[1]))[:10]
        for p_str, rows in top:
            print(f"   {len(rows):>4}  {p_str}")

    if ds["total"]:
        print("\nDead symbols found:")
        top = sorted(ds["by_file"].items(), key=lambda kv: -len(kv[1]))[:10]
        for p_str, rows in top:
            print(f"   {len(rows):>4}  {p_str}")


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="audit_legacy_config_reads",
        description="Scan for legacy CONFIG.<trading_param> reads and "
                    "dead symbols.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--strict", action="store_true",
        help="Exit 1 if any non-whitelisted hit is found (CI mode).",
    )
    mode.add_argument(
        "--report", action="store_true",
        help="Always exit 0; write findings to --output (human mode).",
    )
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text).",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Write report to this path (default: no file written).",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress console summary (useful in CI pipelines).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    root = Path.cwd()
    if not (root / "core").exists():
        print("❌ Run this from the repo root (folder containing 'core/').",
              file=sys.stderr)
        return 2

    legacy_hits, dead_hits, file_count = scan(root)
    summary = _build_summary(legacy_hits, dead_hits, file_count)

    total_hits = summary["legacy_reads"]["total"] + summary["dead_symbols"]["total"]

    # ── Output ──
    if args.format == "json":
        payload = json.dumps(summary, indent=2, default=str)
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
            if not args.quiet:
                print(f"JSON report written: {args.output}")
        else:
            print(payload)
    else:
        if args.output:
            _write_text_report(args.output, summary)
            if not args.quiet:
                print(f"Report written: {args.output}")
        _print_console_summary(summary, args.quiet)

    # ── Exit code ──
    if args.report:
        return 0
    # default is strict semantics for CI safety
    return 1 if total_hits > 0 else 0


if __name__ == "__main__":
    sys.exit(main())