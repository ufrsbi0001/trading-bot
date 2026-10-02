"""
config.py — Single source of truth for environment configuration.

REV 1.6.0 (2026-10-02) — MIN_CONFIDENCE SYNC TO CONFIG_CENTER:
  ✅ After CONFIG is loaded, syncs `min_confidence` into
     core.config_center.GLOBAL["min_confidence"] so it becomes the
     single source of truth for the signal-side confidence floor.
     Used by signals/base._mk().
  ✅ Backward compat preserved:
       • .env MIN_CONFIDENCE still works (synced here)
       • CC_MIN_CONFIDENCE env var (native config_center override)
         takes priority if set — sync is skipped in that case.

REV 1.5.1 (2026-09-30) — USE_5M_TREND_FILTER RENAME:
  ✅ Renamed `use_1m_trend_filter` → `use_5m_trend_filter`. The check
     actually uses 5m klines (indicators.check_short_term_trend), so
     the old name was misleading.
  ✅ Env var: USE_1M_TREND_FILTER is now a fallback. USE_5M_TREND_FILTER
     takes priority; if unset, the old name is still honored (backward
     compatible with existing .env files).
  ✅ public_dict() exposes the renamed field.

REV 1.5.0 (2026-09-29) — MAX_SAME_SIDE_POSITIONS CONFIGURABLE:
  ✅ Added `max_same_side_positions` field (default 2, from
     .env MAX_SAME_SIDE_POSITIONS). Previously this was hardcoded in
     decision_engine.py as `MAX_SAME_SIDE_POSITIONS = 2` — any change
     required editing code. Now tune via .env only.
  ✅ Validation: must be ≥1 and ≤ MAX_OPEN_POSITIONS (can't cap a
     side higher than the total slot limit).
  ✅ Banner: shows "Max open: N (per-side: M)" so the effective
     ceiling is visible at startup.
  ✅ public_dict(): exposes the field for /api/config.

REV 1.4.9 (2026-09-29) — DEAD NEWS/SENTIMENT REMOVED.
REV 1.4.8 (2026-09-29) — PER-FAMILY SPREAD CAPS.
REV 1.4.7 (2026-09-29) — SPREAD FILTER CONFIG.
REV 1.4.6 (2026-09-28) — BANNER MIN-ADX CLARITY.
REV 1.4.5 (2026-09-28) — HTF ALIGN RELAXED FLAG.
REV 1.4.4 (2026-09-26) — REMOVED DEAD GLOBAL SL/TP CAPS.
REV 1.4.3 (2026-09-26) — BOOL WARNING + BASE-DIR GUARD.
REV 1.4.2 (2026-09-25) — KZ_BYPASS SINGLE SOURCE OF TRUTH.
REV 1.4.1 (2026-09-24) — FAIL-FAST COIN RESOLUTION.
REV 1.4.0 (2026-09-24) — MODERN INDICATORS FLAGS (Phase 1).
REV 1.3.8 (2026-09-23) — TIGHTENED MAX_SL_DIST_PCT BOUND.
REV 1.3.3 (2026-09-22) — VALIDATION MESSAGE FIX.

Design goals:
  • Frozen dataclass — immutable after load, thread-safe reads
  • Fail-fast validation at import
  • public_dict() for /api/config (never leaks secrets)
  • All file paths anchored to DATA_DIR
  • Coin universe sourced EXCLUSIVELY from coins_config.py or .env
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Tuple

from dotenv import load_dotenv

load_dotenv()

__all__ = [
    "Config",
    "ConfigError",
    "CONFIG",
    "load_config",
    "print_config_banner",
]


# ─────────────────────────────────────────────────────────────
# ENV PARSING HELPERS
# ─────────────────────────────────────────────────────────────
class ConfigError(ValueError):
    """Raised when an env var is present but malformed, or fails sanity checks."""


_TRUTHY = frozenset({"true", "1", "yes", "on", "y"})
_FALSY  = frozenset({"false", "0", "no", "off", "n"})


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    print(
        f"[config] WARNING: {key}={raw!r} is not a recognized boolean; "
        f"using default {default}. Accepted: "
        f"true/1/yes/on/y or false/0/no/off/n.",
        file=sys.stderr,
    )
    return default


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{key}={raw!r} is not a valid integer") from e


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{key}={raw!r} is not a valid number") from e


def _str(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip()


def _csv_upper(key: str) -> Tuple[str, ...]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return ()
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


def _csv_str(key: str, default: str) -> Tuple[str, ...]:
    raw = os.getenv(key, default).strip()
    if not raw:
        return (default,)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _clean_key(s: str) -> str:
    if not s:
        return ""
    return s.strip().strip('"').strip("'")


def _strip_usdt(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


# ─────────────────────────────────────────────────────────────
# CONFIG DATACLASS
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    # ── Credentials ─────────────────────────────────────────
    api_key: str
    api_secret: str
    web_token: str
    flask_secret_key: str

    # ── Trading environment ─────────────────────────────────
    demo_mode: bool
    testnet: bool
    dry_run: bool

    # ── Symbols ─────────────────────────────────────────────
    bot_coins: Tuple[str, ...]
    blacklisted_coins: FrozenSet[str]

    # ── Risk ────────────────────────────────────────────────
    risk_percent: float
    leverage: int
    max_open_positions: int
    max_same_side_positions: int        # ← REV 1.5.0
    max_total_margin_pct: float
    max_daily_loss_trades: int
    max_daily_drawdown_percent: float
    max_account_drawdown: float

    # NOTE: SL/TP distance caps are PER-COIN (coins/*.py CAPS),
    # enforced in orders.py via coins_config.get_caps(symbol).
    # No global cap field exists here by design.
    max_hold_minutes: int

    cooldown_after_sl_min: int
    cooldown_after_tp_min: int
    partial_close_usdt: float

    # ── Strategy filters ────────────────────────────────────
    min_confidence: float
    min_adx: float
    require_htf_agreement: bool
    use_5m_trend_filter: bool          # ← REV 1.5.1 (renamed from use_1m_trend_filter)
    fg_enabled: bool
    max_trades_per_coin_per_day: int
    htf_align_relaxed: bool          # ← REV 1.4.5

    # REV 1.4.0 — Modern indicator flags (Phase 1: default OFF)
    use_taker_volume: bool
    use_volume_profile: bool
    use_anchored_vwap: bool
    use_funding_z: bool
    use_mtf_confluence: bool

    # REV 1.4.2 — Killzone bypass (single source of truth)
    kz_bypass: bool

    # REV 1.4.7 — Spread filter (pre-entry gate)
    use_spread_filter: bool
    max_spread_pct: float
    # REV 1.4.8 — Per-family spread caps
    max_spread_trend: float
    max_spread_range: float
    max_spread_volatility: float

    # ── Telegram ────────────────────────────────────────────
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

    # ── Web server ──────────────────────────────────────────
    host: str
    port: int
    allowed_origins: Tuple[str, ...]

    # ── API pagination ──────────────────────────────────────
    history_lookback_days: int
    history_limit: int
    income_page_size: int
    max_income_pages: int
    fills_page_size: int
    max_fills_pages: int

    # ── File paths ──────────────────────────────────────────
    csv_file: str
    csv_exit_file: str
    loss_file: str
    active_trades_file: str
    cooldown_file: str
    coin_rotation_file: str
    pause_file: str

    log_level: str

    # ── Derived ─────────────────────────────────────────────
    @property
    def log_dir(self) -> str:
        return str(Path(self.csv_file).parent / "Logs")

    @property
    def data_dir(self) -> Path:
        return Path(self.csv_file).parent

    @property
    def keys_present(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def token_present(self) -> bool:
        return bool(self.web_token)

    @property
    def trading_mode_label(self) -> str:
        if self.demo_mode:
            return "DEMO (demo.binance.com)"
        if self.testnet:
            return "TESTNET (testnet.binance.vision)"
        return "MAINNET — REAL MONEY"

    def public_dict(self) -> dict:
        return {
            "demo_mode": self.demo_mode,
            "testnet": self.testnet,
            "dry_run": self.dry_run,
            "trading_mode": self.trading_mode_label,
            "leverage": self.leverage,
            "risk_percent": self.risk_percent,
            "max_open_positions": self.max_open_positions,
            "max_same_side_positions": self.max_same_side_positions,   # ← REV 1.5.0
            "max_daily_loss_trades": self.max_daily_loss_trades,
            "max_daily_drawdown_percent": self.max_daily_drawdown_percent,
            "max_account_drawdown": self.max_account_drawdown,
            "max_hold_minutes": self.max_hold_minutes,
            "min_confidence": self.min_confidence,
            "min_adx": self.min_adx,
            "require_htf_agreement": self.require_htf_agreement,
            "use_5m_trend_filter": self.use_5m_trend_filter,   # ← REV 1.5.1
            "max_trades_per_coin_per_day": self.max_trades_per_coin_per_day,
            "htf_align_relaxed": self.htf_align_relaxed,
            "cooldown_after_sl_min": self.cooldown_after_sl_min,
            "cooldown_after_tp_min": self.cooldown_after_tp_min,
            "partial_close_usdt": self.partial_close_usdt,
            # REV 1.4.0 — Modern indicator flags
            "use_taker_volume": self.use_taker_volume,
            "use_volume_profile": self.use_volume_profile,
            "use_anchored_vwap": self.use_anchored_vwap,
            "use_funding_z": self.use_funding_z,
            "use_mtf_confluence": self.use_mtf_confluence,
            # REV 1.4.2 — Killzone bypass
            "kz_bypass": self.kz_bypass,
            # REV 1.4.7 — Spread filter
            "use_spread_filter": self.use_spread_filter,
            "max_spread_pct": self.max_spread_pct,
            "max_spread_trend": self.max_spread_trend,
            "max_spread_range": self.max_spread_range,
            "max_spread_volatility": self.max_spread_volatility,
            "coins": list(self.bot_coins),
            "coin_count": len(self.bot_coins),
            "keys_present": self.keys_present,
            "token_present": self.token_present,
            "history_lookback_days": self.history_lookback_days,
        }


# ─────────────────────────────────────────────────────────────
# LOADER
# ─────────────────────────────────────────────────────────────
def _resolve_base_dir() -> Path:
    raw = os.getenv("DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else (Path.cwd() / "data")
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "Logs").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigError(
            f"Failed to prepare DATA_DIR at {base!s}: "
            f"{type(e).__name__}: {e}"
        ) from e
    return base


def _resolve_coins(blacklist: FrozenSet[str]) -> Tuple[str, ...]:
    """
    REV 1.4.1 — Fail-fast coin resolution. NO silent hardcoded fallback.

    Priority:
      1. coins_config.enabled_coins()   ← primary source of truth
      2. .env BOT_COINS                  ← explicit override / fallback

    If BOTH fail:
      → raises ConfigError with clear remediation guidance.
    """
    # ── Priority 1: coins_config ──
    try:
        from core.coins_config import enabled_coins
        enabled = tuple(
            sym for sym in enabled_coins()
            if _strip_usdt(sym) not in blacklist
        )
        if enabled:
            return enabled
        print(
            "[config] coins_config loaded but produced no eligible coins "
            "(all disabled or blacklisted) — falling back to BOT_COINS.",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"[config] coins_config load failed: {e}",
            file=sys.stderr,
        )

    # ── Priority 2: .env BOT_COINS ──
    raw = os.getenv("BOT_COINS", "").strip()
    if raw:
        parsed = tuple(
            (c if c.endswith("USDT") else c + "USDT")
            for c in (x.strip().upper() for x in raw.split(",") if x.strip())
        )
        filtered = tuple(c for c in parsed if _strip_usdt(c) not in blacklist)
        if filtered:
            return filtered
        print(
            "[config] BOT_COINS set in .env but all entries blacklisted/empty.",
            file=sys.stderr,
        )

    # ── No silent fallback — fail loud ──
    raise ConfigError(
        "No coins resolved. Fix ONE of the following:\n"
        "  • Ensure coins_config.py loads correctly and at least one\n"
        "    coins/*.py has ENABLED = True, OR\n"
        "  • Set BOT_COINS in .env (comma-separated, e.g.\n"
        "    BOT_COINS=BTCUSDT,ETHUSDT,SOLUSDT)\n"
        "\n"
        "Note: REV 1.4.1 removed the hardcoded _DEFAULT_COINS fallback\n"
        "because it silently traded wrong per-coin SL/TP values when\n"
        "coins_config failed to load."
    )


def load_config() -> Config:
    base = _resolve_base_dir()

    blacklist = frozenset(_csv_upper("BLACKLISTED_COINS"))
    coins = _resolve_coins(blacklist)

    cfg = Config(
        api_key=_clean_key(os.getenv("BINANCE_API_KEY") or os.getenv("API_KEY") or ""),
        api_secret=_clean_key(
            os.getenv("BINANCE_API_SECRET") or os.getenv("SECRET_KEY") or ""
        ),
        web_token=_str("WEB_TOKEN"),
        flask_secret_key=_str("FLASK_SECRET_KEY") or os.urandom(24).hex(),

        demo_mode=_bool("DEMO_MODE", True),
        testnet=_bool("TESTNET", False),
        dry_run=_bool("DRY_RUN", False),

        bot_coins=coins,
        blacklisted_coins=blacklist,

        risk_percent=_float("RISK_PERCENT", 0.5),
        leverage=_int("LEVERAGE", 5),
        max_open_positions=_int("MAX_OPEN_POSITIONS", 3),
        # REV 1.5.0 — per-side cap (tunable via .env)
        max_same_side_positions=_int("MAX_SAME_SIDE_POSITIONS", 2),
        max_total_margin_pct=_float("MAX_TOTAL_MARGIN_PCT", 0.60),
        max_daily_loss_trades=_int("MAX_DAILY_LOSS_TRADES", 3),
        max_daily_drawdown_percent=_float("MAX_DAILY_DRAWDOWN_PERCENT", 4.0),
        max_account_drawdown=_float("MAX_ACCOUNT_DRAWDOWN", 10.0),
        max_hold_minutes=_int("MAX_HOLD_MINUTES", 60),

        cooldown_after_sl_min=_int("COOLDOWN_AFTER_SL_MIN", 20),
        cooldown_after_tp_min=_int("COOLDOWN_AFTER_TP_MIN", 15),
        partial_close_usdt=_float("PARTIAL_CLOSE_USDT", 15.0),

        min_confidence=_float("MIN_CONFIDENCE", 60),
        min_adx=_float("MIN_ADX", 22),
        require_htf_agreement=_bool("REQUIRE_HTF_AGREEMENT", True),
        # REV 1.5.1 — renamed USE_1M_TREND_FILTER → USE_5M_TREND_FILTER.
        # Legacy env var name is honored as fallback so existing .env
        # files continue to work without changes.
        use_5m_trend_filter=_bool(
            "USE_5M_TREND_FILTER",
            _bool("USE_1M_TREND_FILTER", True),
        ),
        fg_enabled=_bool("FG_ENABLED", False),
        max_trades_per_coin_per_day=_int("MAX_TRADES_PER_COIN_PER_DAY", 2),
        htf_align_relaxed=_bool("HTF_ALIGN_RELAXED", False),

        # REV 1.4.0 — Modern indicator flags (all default OFF)
        use_taker_volume=_bool("USE_TAKER_VOLUME", False),
        use_volume_profile=_bool("USE_VOLUME_PROFILE", False),
        use_anchored_vwap=_bool("USE_ANCHORED_VWAP", False),
        use_funding_z=_bool("USE_FUNDING_Z", False),
        use_mtf_confluence=_bool("USE_MTF_CONFLUENCE", False),

        # REV 1.4.2 — Killzone bypass (single source of truth)
        kz_bypass=_bool("KZ_BYPASS", False),

        # REV 1.4.7 — Spread filter
        use_spread_filter=_bool("USE_SPREAD_FILTER", True),
        max_spread_pct=_float("MAX_SPREAD_PCT", 0.15),
        # REV 1.4.8 — Per-family spread caps
        max_spread_trend=_float("MAX_SPREAD_TREND", 0.08),
        max_spread_range=_float("MAX_SPREAD_RANGE", 0.12),
        max_spread_volatility=_float("MAX_SPREAD_VOLATILITY", 0.25),

        telegram_enabled=_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),

        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 5000),
        allowed_origins=_csv_str("ALLOWED_ORIGINS", "*"),

        history_lookback_days=_int("HISTORY_LOOKBACK_DAYS", 30),
        history_limit=_int("HISTORY_LIMIT", 500),
        income_page_size=_int("INCOME_PAGE_SIZE", 1000),
        max_income_pages=_int("MAX_INCOME_PAGES", 20),
        fills_page_size=_int("FILLS_PAGE_SIZE", 1000),
        max_fills_pages=_int("MAX_FILLS_PAGES", 10),

        csv_file=str(base / "trading_log.csv"),
        csv_exit_file=str(base / "trades_exit.csv"),
        loss_file=str(base / "daily_tracker.json"),
        active_trades_file=str(base / "active_trades.json"),
        cooldown_file=str(base / "cooldowns.json"),
        coin_rotation_file=str(base / "coin_rotation.json"),
        pause_file=str(base / "pause.txt"),

        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    errors: list[str] = []

    if not 1 <= cfg.leverage <= 125:
        errors.append(f"LEVERAGE={cfg.leverage} — must be 1–125")
    if not 0 < cfg.risk_percent <= 100:
        errors.append(f"RISK_PERCENT={cfg.risk_percent} — must be >0 and ≤100")
    if cfg.max_open_positions < 1:
        errors.append(f"MAX_OPEN_POSITIONS={cfg.max_open_positions} — must be ≥1")

    # REV 1.5.0 — per-side cap validation
    if cfg.max_same_side_positions < 1:
        errors.append(
            f"MAX_SAME_SIDE_POSITIONS={cfg.max_same_side_positions} — must be ≥1"
        )
    if cfg.max_same_side_positions > cfg.max_open_positions:
        errors.append(
            f"MAX_SAME_SIDE_POSITIONS={cfg.max_same_side_positions} must be "
            f"≤ MAX_OPEN_POSITIONS={cfg.max_open_positions}"
        )

    if not 0 < cfg.max_total_margin_pct <= 1:
        errors.append(
            f"MAX_TOTAL_MARGIN_PCT={cfg.max_total_margin_pct} — must be in (0, 1]"
        )
    if cfg.max_daily_loss_trades < 1:
        errors.append("MAX_DAILY_LOSS_TRADES must be ≥1")
    if not 0 < cfg.max_daily_drawdown_percent <= 100:
        errors.append("MAX_DAILY_DRAWDOWN_PERCENT must be in (0, 100]")
    if not 0 < cfg.max_account_drawdown <= 100:
        errors.append("MAX_ACCOUNT_DRAWDOWN must be in (0, 100]")
    if cfg.max_hold_minutes < 1:
        errors.append(f"MAX_HOLD_MINUTES={cfg.max_hold_minutes} — must be ≥1")

    if cfg.cooldown_after_sl_min < 0:
        errors.append("COOLDOWN_AFTER_SL_MIN must be ≥0")
    if cfg.cooldown_after_tp_min < 0:
        errors.append("COOLDOWN_AFTER_TP_MIN must be ≥0")
    if cfg.partial_close_usdt < 0:
        errors.append("PARTIAL_CLOSE_USDT must be ≥0 (0 = disabled)")
    if not 0 <= cfg.min_confidence <= 100:
        errors.append(f"MIN_CONFIDENCE={cfg.min_confidence} — must be 0–100")
    if not 0 <= cfg.min_adx <= 100:
        errors.append(f"MIN_ADX={cfg.min_adx} — must be 0–100")
    if cfg.max_trades_per_coin_per_day < 1:
        errors.append("MAX_TRADES_PER_COIN_PER_DAY must be ≥1")
    if not 1 <= cfg.port <= 65535:
        errors.append(f"PORT={cfg.port} — must be 1–65535")
    if cfg.history_lookback_days < 1:
        errors.append("HISTORY_LOOKBACK_DAYS must be ≥1")

    # REV 1.4.7 — Spread filter validation
    if not 0 <= cfg.max_spread_pct <= 5.0:
        errors.append(
            f"MAX_SPREAD_PCT={cfg.max_spread_pct} — must be in [0, 5.0]"
        )
    # REV 1.4.8 — Per-family spread caps validation
    for _k in ("max_spread_trend", "max_spread_range", "max_spread_volatility"):
        _v = getattr(cfg, _k, 0.15)
        if not 0 <= _v <= 5.0:
            errors.append(f"{_k.upper()}={_v} — must be in [0, 5.0]")

    if errors:
        raise ConfigError(
            "Configuration validation failed:\n  • " + "\n  • ".join(errors)
        )


# ─────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────
CONFIG: Config = load_config()


# ─────────────────────────────────────────────────────────────
# REV 1.6.0 — SYNC min_confidence INTO config_center
# ─────────────────────────────────────────────────────────────
# config_center.GLOBAL["min_confidence"] is the new single source of
# truth for the signal-side confidence floor (used by signals/base._mk).
#
# Backward compat: .env MIN_CONFIDENCE still works — we sync CONFIG's
# value into config_center at import time.
#
# Priority (highest wins):
#   1. CC_MIN_CONFIDENCE env var (native config_center override)
#   2. .env MIN_CONFIDENCE (synced here)
#   3. config_center.GLOBAL["min_confidence"] default (60)
#
# Note: guarded so a config_center import failure never crashes
# config.py's own load. If it fails, base._mk() falls back to
# spec.min_confidence (which is CONFIG.min_confidence via FamilySpec).
# ─────────────────────────────────────────────────────────────
try:
    if not os.getenv("CC_MIN_CONFIDENCE"):
        from core import config_center as _cc
        _cc.GLOBAL["min_confidence"] = CONFIG.min_confidence
except Exception as _sync_err:
    # Never crash config load over a sync issue
    print(
        f"[config] min_confidence sync to config_center failed: {_sync_err}",
        file=sys.stderr,
    )


def _partial_close_label() -> str:
    if CONFIG.partial_close_usdt == 0:
        return "DISABLED (TP1/TP2 mode)"
    return f"${CONFIG.partial_close_usdt:.2f} profit (70% lock)"


def _spread_label() -> str:
    """REV 1.4.8 — per-family spread caps."""
    if not CONFIG.use_spread_filter:
        return "OFF"
    return (f"ON (global {CONFIG.max_spread_pct:.2f}% | "
            f"trend {CONFIG.max_spread_trend:.2f}% | "
            f"range {CONFIG.max_spread_range:.2f}% | "
            f"vol {CONFIG.max_spread_volatility:.2f}%)")


def _trend_filter_label() -> str:
    """REV 1.5.1 — 5m trend filter state."""
    if not CONFIG.use_5m_trend_filter:
        return "OFF"
    return "ON (trend-following only — counter-trend bypass)"


def print_config_banner() -> None:
    sep = "=" * 60
    coins_str = (
        f"({', '.join(CONFIG.bot_coins)})" if CONFIG.bot_coins else ""
    )
    lines = [
        sep,
        "  TRADING DESK · CONFIG LOADED",
        sep,
        f"  Mode:           {CONFIG.trading_mode_label}",
        f"  Dry run:        {'YES (no orders sent)' if CONFIG.dry_run else 'no'}",
        f"  Coins:          {len(CONFIG.bot_coins)} {coins_str}",
        f"  Leverage:       {CONFIG.leverage}×",
        f"  Risk / trade:   {CONFIG.risk_percent}%",
        f"  Max open:       {CONFIG.max_open_positions} "
        f"(per-side: {CONFIG.max_same_side_positions})",     # ← REV 1.5.0
        f"  Daily cap:      {CONFIG.max_daily_loss_trades} losses / "
        f"{CONFIG.max_daily_drawdown_percent}% DD",
        f"  Partial close:  {_partial_close_label()}",
        f"  Min ADX:        {CONFIG.min_adx} (fallback — runtime per-family floors printed by future.py)",
        f"  Min confidence: {CONFIG.min_confidence}",          # ← REV 1.6.0
        f"  Max hold:       {CONFIG.max_hold_minutes} min",
        f"  History window: {CONFIG.history_lookback_days} days",
        f"  Modern flags:   taker={CONFIG.use_taker_volume} "
        f"vp={CONFIG.use_volume_profile} "
        f"avwap={CONFIG.use_anchored_vwap} "
        f"fz={CONFIG.use_funding_z} "
        f"mtf={CONFIG.use_mtf_confluence}",
        f"  5m trend filter: {_trend_filter_label()}",   # ← REV 1.5.1
        f"  HTF align:      "
        f"{'RELAXED (e20 OR price)' if CONFIG.htf_align_relaxed else 'STRICT (e20 AND price)'}",
        f"  Killzone mode:  "
        f"{'BYPASS (24/7)' if CONFIG.kz_bypass else 'ENFORCED (NY sessions only)'}",
        f"  Spread filter:  {_spread_label()}",
        f"  API keys:       {'set' if CONFIG.keys_present else 'MISSING'}",
        f"  WEB_TOKEN:      {'set' if CONFIG.token_present else 'MISSING'}",
        f"  Data dir:       {Path(CONFIG.csv_file).parent}",
        sep,
    ]
    for line in lines:
        print(line, file=sys.stdout)


if __name__ == "__main__":
    import json
    print_config_banner()
    print("\nPublic config (safe to expose to UI):")
    print(json.dumps(CONFIG.public_dict(), indent=2))