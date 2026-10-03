"""
audit_legacy_config_reads.py — Find all CONFIG.<trading_param> reads
that should be migrated to config_center.CC.get().

Run from repo root:
    python scripts/audit_legacy_config_reads.py

Output:
    config_legacy_reads.txt   (grouped by file, with line numbers)
    (also prints summary to console)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from collections import defaultdict

# ── Trading params that MUST come from config_center, not CONFIG ──
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

# ── Dead symbols to hunt (should be ZERO hits) ──
DEAD_SYMBOLS = [
    "get_futures_sentiment",
    "get_liquidation_pressure",
    "COOLDOWN_AFTER_SL_MIN",
    "COOLDOWN_AFTER_TP_MIN",
    "use_1m_trend_filter",
]

# ── Directories to skip ──
SKIP_DIRS = {
    ".git", "__pycache__", "venv", ".venv", "env", ".env",
    "node_modules", ".pytest_cache", ".mypy_cache", "build", "dist",
}

# ── Build one big regex: CONFIG.<param> ──
_param_alt = "|".join(re.escape(p) for p in LEGACY_TRADING_PARAMS)
CONFIG_READ_RE = re.compile(rf"\bCONFIG\.({_param_alt})\b")

# ── Dead symbol regex ──
DEAD_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in DEAD_SYMBOLS) + r")\b")


def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)


def scan(root: Path):
    legacy_hits: dict[Path, list[tuple[int, str, str]]] = defaultdict(list)
    dead_hits:   dict[Path, list[tuple[int, str, str]]] = defaultdict(list)

    py_files = [p for p in root.rglob("*.py") if not should_skip(p)]
    print(f"Scanning {len(py_files)} Python files under {root} ...\n")

    for p in py_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  [skip] {p}: {e}", file=sys.stderr)
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # skip comment-only lines
            if stripped.startswith("#"):
                continue

            m = CONFIG_READ_RE.search(line)
            if m:
                legacy_hits[p].append((lineno, m.group(1), stripped))

            d = DEAD_RE.search(line)
            if d and not stripped.startswith("#"):
                # avoid flagging the audit script itself
                if p.name == Path(__file__).name:
                    continue
                dead_hits[p].append((lineno, d.group(1), stripped))

    return legacy_hits, dead_hits


def write_report(out_path: Path,
                 legacy_hits: dict,
                 dead_hits: dict) -> None:
    with out_path.open("w", encoding="utf-8") as f:
        f.write("=" * 78 + "\n")
        f.write("  LEGACY CONFIG.<trading_param> READS\n")
        f.write("  → migrate to: from core import config_center as CC; CC.get('<key>')\n")
        f.write("=" * 78 + "\n\n")

        total_legacy = sum(len(v) for v in legacy_hits.values())
        f.write(f"Total matches: {total_legacy}  |  "
                f"Files affected: {len(legacy_hits)}\n\n")

        for p in sorted(legacy_hits.keys(), key=lambda x: str(x)):
            rows = legacy_hits[p]
            f.write(f"\n── {p}  ({len(rows)} hits)\n")
            for lineno, param, line in rows:
                f.write(f"   L{lineno:<5}  CONFIG.{param:<28}  |  {line}\n")

        f.write("\n\n")
        f.write("=" * 78 + "\n")
        f.write("  DEAD SYMBOLS (should be ZERO hits)\n")
        f.write("=" * 78 + "\n\n")

        total_dead = sum(len(v) for v in dead_hits.values())
        f.write(f"Total matches: {total_dead}  |  "
                f"Files affected: {len(dead_hits)}\n\n")

        for p in sorted(dead_hits.keys(), key=lambda x: str(x)):
            rows = dead_hits[p]
            f.write(f"\n── {p}  ({len(rows)} hits)\n")
            for lineno, sym, line in rows:
                f.write(f"   L{lineno:<5}  {sym:<32}  |  {line}\n")

    print(f"\n✅ Report written: {out_path}")
    print(f"   Legacy CONFIG reads : {total_legacy}")
    print(f"   Dead symbols        : {total_dead}")


def main():
    root = Path.cwd()
    if not (root / "core").exists():
        print("❌ Run this from the repo root (folder containing 'core/').",
              file=sys.stderr)
        sys.exit(1)

    out = root / "config_legacy_reads.txt"
    legacy_hits, dead_hits = scan(root)
    write_report(out, legacy_hits, dead_hits)

    # Also print top offenders to console
    if legacy_hits:
        print("\nTop offenders (legacy CONFIG reads):")
        for p, rows in sorted(legacy_hits.items(),
                              key=lambda kv: -len(kv[1]))[:10]:
            print(f"   {len(rows):>4}  {p}")
    if dead_hits:
        print("\nDead symbols found (FIX THESE):")
        for p, rows in sorted(dead_hits.items(),
                              key=lambda kv: -len(kv[1]))[:10]:
            print(f"   {len(rows):>4}  {p}")


if __name__ == "__main__":
    main()