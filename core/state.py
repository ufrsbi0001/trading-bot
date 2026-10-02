"""
state.py — Runtime state for the trading engine.

REV 1.3.16 (2026-10-02) — DAILY TRACKER DATE-ROLLOVER FIX:
  ✅ BUG FIX: DailyTracker was saving losses with STALE date.
     If the bot ran across midnight (e.g. 10-01 → 10-02) and a
     loss occurred BEFORE reset_if_new_day() was ever called,
     add_loss() would save the file with the OLD date. On the
     next restart, _load() would see a date mismatch and reset
     loss_count to 0 — losing all pre-restart losses.

     Symptoms: dashboard showed 0/10 even though SL hits had
     happened earlier in the day.

     Fix:
       • New `_check_date_rollover()` — called at the START of
         add_loss() AND is_limit_reached(). Detects date change
         WITHOUT needing balance, resets loss_count, and re-saves
         with the CURRENT date.
       • `_load()` now logs when it skips a stale-date file so
         future debugging is easier.
       • New `get_loss_count()` public getter (with rollover check)
         for web/app.py to use.

  ✅ Both SL and RR_COLLAPSE trigger add_loss() (verified in
     orders/exit.py — any negative PnL with reason != TIME_EXIT,
     or TIME_EXIT with R <= -0.30, counts as a real loss).

REV 1.3.15 (2026-09-29) — DEAD HTF COUNTER REMOVED.
REV 1.3.14 (2026-09-29) — DEAD COUNTER CLEANUP.
REV 1.3.13 (2026-09-28) — PRE-GATE SIGNAL COUNTERS.
REV 1.3.12 (2026-09-28) — DECISION COUNTER ADDED.
REV 1.3.11 (2026-09-26) — DEAD IMPORT CLEANUP.
REV 1.3.10 (2026-09-26) — MTF COUNTER ADDED.
REV 1.3.9  (2026-09-24) — WRITE-LOCK RACE + TEMP-FILE UNIQUENESS FIX.
REV 1.3.8  (2026-09-23) — WRITE LOCK ADDED.
REV 1.3.5  (2026-09-23) — ATOMIC WRITES + PEAK FIX.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config import CONFIG
from core.client import logger

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
PKT = timezone(timedelta(hours=5))

CSV_FILE            = CONFIG.csv_file
CSV_EXIT_FILE       = CONFIG.csv_exit_file
LOSS_FILE           = CONFIG.loss_file
ACTIVE_TRADES_FILE  = CONFIG.active_trades_file
COOLDOWN_FILE       = CONFIG.cooldown_file
COIN_ROTATION_FILE  = CONFIG.coin_rotation_file
PAUSE_FILE          = CONFIG.pause_file

MAX_TRADES_PER_COIN_PER_DAY = CONFIG.max_trades_per_coin_per_day
MAX_DAILY_LOSS_TRADES       = CONFIG.max_daily_loss_trades
MAX_DAILY_DRAWDOWN_PERCENT  = CONFIG.max_daily_drawdown_percent
MAX_ACCOUNT_DRAWDOWN        = CONFIG.max_account_drawdown
COOLDOWN_AFTER_SL_MIN       = CONFIG.cooldown_after_sl_min
COOLDOWN_AFTER_TP_MIN       = CONFIG.cooldown_after_tp_min
BLACKLISTED_COINS           = set(CONFIG.blacklisted_coins)


# ─────────────────────────────────────────────────────────────
# ATOMIC JSON WRITE — REV 1.3.5, hardened REV 1.3.9
# ─────────────────────────────────────────────────────────────
def _atomic_json_write(path: str, data: Any) -> bool:
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"Atomic write failed for {path}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────
# STOP EVENT
# ─────────────────────────────────────────────────────────────
_stop_event = threading.Event()

def request_stop() -> None:
    _stop_event.set()

def clear_stop() -> None:
    _stop_event.clear()

def is_stopped() -> bool:
    return _stop_event.is_set()


# ─────────────────────────────────────────────────────────────
# ACTIVE TRADES
# ─────────────────────────────────────────────────────────────
active_trades: dict[str, dict] = {}
ACTIVE_TRADES_LOCK = threading.Lock()
_ACTIVE_WRITE_LOCK = threading.Lock()


def flush_active_trades() -> None:
    with _ACTIVE_WRITE_LOCK:
        with ACTIVE_TRADES_LOCK:
            data = dict(active_trades)
        _atomic_json_write(ACTIVE_TRADES_FILE, data)


def load_active_trades() -> None:
    try:
        if os.path.exists(ACTIVE_TRADES_FILE):
            with open(ACTIVE_TRADES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with ACTIVE_TRADES_LOCK:
                active_trades.clear()
                for sym, trade in data.items():
                    if 'sl_level' in trade:
                        lvl = trade['sl_level']
                        try:
                            lvl_int = int(float(lvl))
                            trade['sl_level'] = lvl_int if lvl_int in (0, 1, 2, 3) else 0
                        except Exception:
                            trade['sl_level'] = 0
                    active_trades[sym] = trade
        else:
            with ACTIVE_TRADES_LOCK:
                active_trades.clear()
    except Exception as e:
        logger.warning(f"Load active trades error: {e}")


def add_active_trade(symbol: str, data: dict) -> None:
    with ACTIVE_TRADES_LOCK:
        active_trades[symbol] = data
    flush_active_trades()


def remove_active_trade(symbol: str) -> None:
    with ACTIVE_TRADES_LOCK:
        if symbol in active_trades:
            del active_trades[symbol]
    flush_active_trades()


def get_active_trade(symbol: str) -> dict:
    with ACTIVE_TRADES_LOCK:
        return active_trades.get(symbol, {})


# ─────────────────────────────────────────────────────────────
# TRACKED SYMBOLS
# ─────────────────────────────────────────────────────────────
bot_tracked_symbols: set[str] = set()
BOT_TRACKED_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────
# COOLDOWNS
# ─────────────────────────────────────────────────────────────
cooldown_until: dict[str, datetime] = {}
COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_WRITE_LOCK = threading.Lock()


def load_cooldowns() -> None:
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with COOLDOWN_LOCK:
                for sym, ts_str in data.items():
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts > datetime.now(PKT):
                            cooldown_until[sym] = ts
                    except Exception as e:
                        logger.warning(f"Skipping malformed cooldown {sym}={ts_str}: {e}")
        logger.info(f" Cooldowns loaded ({len(cooldown_until)} active)")
    except Exception as e:
        logger.warning(f"Could not load cooldowns: {e}")


def save_cooldowns() -> None:
    try:
        with _COOLDOWN_WRITE_LOCK:
            with COOLDOWN_LOCK:
                data = {sym: ts.isoformat() for sym, ts in cooldown_until.items()
                        if ts > datetime.now(PKT)}
            _atomic_json_write(COOLDOWN_FILE, data)
    except Exception as e:
        logger.warning(f"Could not save cooldowns: {e}")


# ─────────────────────────────────────────────────────────────
# ENTRY-CANDLE DEDUP
# ─────────────────────────────────────────────────────────────
last_entry_candle: dict[str, Any] = {}
ENTRY_CANDLE_LOCK = threading.Lock()


def cleanup_entry_candles() -> None:
    try:
        import pandas as pd
        now_naive = pd.Timestamp.now(tz='UTC').tz_convert(None)
        cutoff = now_naive - timedelta(hours=2)
        with ENTRY_CANDLE_LOCK:
            stale = []
            for k, v in last_entry_candle.items():
                try:
                    vv = v.tz_convert(None) if getattr(v, 'tzinfo', None) is not None else v
                    if vv < cutoff:
                        stale.append(k)
                except Exception:
                    continue
            for k in stale:
                del last_entry_candle[k]
    except Exception as e:
        logger.debug(f"entry candle cleanup: {e}")


# ─────────────────────────────────────────────────────────────
# SCAN RESULTS
# ─────────────────────────────────────────────────────────────
scan_results: list[dict] = []
SCAN_RESULTS_LOCK = threading.Lock()
scan_timestamp: Optional[str] = None


def set_scan_results(rows: list[dict], ts: str) -> None:
    global scan_timestamp
    with SCAN_RESULTS_LOCK:
        scan_results.clear()
        scan_results.extend(rows)
        scan_timestamp = ts


def get_scan_results() -> tuple[list[dict], Optional[str]]:
    with SCAN_RESULTS_LOCK:
        return list(scan_results), scan_timestamp


# ─────────────────────────────────────────────────────────────
# FAIL COUNTS + ATR CACHE
# ─────────────────────────────────────────────────────────────
fail_counts: dict[str, int] = {}
FAIL_COUNTS_LOCK = threading.Lock()

ATR_CACHE: dict[str, dict] = {}
ATR_CACHE_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────
# V2 STATS
# ─────────────────────────────────────────────────────────────
V2_STATS = {
    'signals':  0,      # directional signals that PASSED pre-gate
    'low_conf': 0,      # pre-gate rejects
    'low_rr':   0,
    'low_adx':  0,
    'trend': 0,
    'rotation': 0,
    'decision': 0,
    'total': 0,         # sum of all BLOCKED events (not 'signals')
}
V2_STATS_LOCK = threading.Lock()


def increment_v2_stat(rule: str) -> None:
    with V2_STATS_LOCK:
        if rule in V2_STATS:
            V2_STATS[rule] += 1
            # 'signals' is a candidate count, NOT a block — exclude from total
            if rule != 'signals':
                V2_STATS['total'] += 1


def reset_v2_stats() -> None:
    with V2_STATS_LOCK:
        for k in V2_STATS:
            V2_STATS[k] = 0
    logger.info(" V2 Stats Daily Reset")


def get_v2_stats_str() -> str:
    with V2_STATS_LOCK:
        return (f"V2 Stats - Sig:{V2_STATS['signals']} "
                f"PreGate[c:{V2_STATS['low_conf']} "
                f"r:{V2_STATS['low_rr']} "
                f"a:{V2_STATS['low_adx']}] "
                f"Trend:{V2_STATS['trend']} "
                f"Rot:{V2_STATS['rotation']} "
                f"Dec:{V2_STATS['decision']} "
                f"Blocked:{V2_STATS['total']}")


# ─────────────────────────────────────────────────────────────
# DAILY TRACKER
#   REV 1.3.16 — date-rollover fix + public getter
# ─────────────────────────────────────────────────────────────
class DailyTracker:
    """
    Tracks daily loss count + balance peaks.

    Persistence: JSON file (LOSS_FILE from CONFIG).
    Reset trigger: date change in PKT timezone (UTC+5).

    REV 1.3.16:
      • `_check_date_rollover()` runs at START of add_loss() and
        is_limit_reached() — prevents stale-date saves.
      • `get_loss_count()` public getter — for web/app.py.
    """

    def __init__(self):
        self.file = LOSS_FILE
        self.today = datetime.now(PKT).date()
        self.loss_count = 0
        self.start_balance = 0
        self.peak_balance = 0
        self._load()

    # ── Load from disk (with date guard) ──
    def _load(self):
        try:
            if not os.path.exists(self.file):
                logger.info(f" DailyTracker: no file yet ({self.file})")
                return
            with open(self.file, 'r', encoding='utf-8') as f:
                d = json.load(f)
            saved_date = d.get('date')
            if saved_date == str(self.today):
                self.loss_count = int(d.get('loss_count', 0))
                self.start_balance = float(d.get('start_balance', 0))
                self.peak_balance = float(d.get('peak_balance', 0))
                if d.get('peak_date') != str(self.today):
                    self.peak_balance = self.start_balance
                logger.info(
                    f" DailyTracker loaded: date={saved_date} "
                    f"loss_count={self.loss_count} "
                    f"start_bal=${self.start_balance:.2f}"
                )
            else:
                logger.info(
                    f" DailyTracker: stale file date={saved_date} "
                    f"(today={self.today}) — starting fresh"
                )
        except Exception as e:
            logger.warning(f"Tracker load error: {e}")

    # ── Save to disk ──
    def _save(self):
        _atomic_json_write(self.file, {
            'date': str(self.today),
            'loss_count': self.loss_count,
            'start_balance': self.start_balance,
            'peak_balance': self.peak_balance,
            'peak_date': str(self.today),
        })

    # ── REV 1.3.16: date rollover check (no balance required) ──
    def _check_date_rollover(self):
        """
        Detect date change WITHOUT needing current balance.
        Called from add_loss() so a loss logged just after midnight
        doesn't get saved with yesterday's date.
        """
        today = datetime.now(PKT).date()
        if today != self.today:
            logger.info(
                f" DailyTracker: date rollover "
                f"{self.today} → {today} — resetting loss_count"
            )
            self.today = today
            self.loss_count = 0
            # start_balance will be re-set on next reset_if_new_day()
            # with a real balance reading.
            self._save()
            reset_v2_stats()

    # ── Public getter for web/app.py ──
    def get_loss_count(self) -> int:
        """Return the current day's loss count (with rollover check)."""
        self._check_date_rollover()
        return self.loss_count

    # ── Reset if new day (needs balance to record start) ──
    def reset_if_new_day(self, current_balance: float):
        today = datetime.now(PKT).date()
        if today != self.today or self.start_balance == 0:
            self.today = today
            self.loss_count = 0
            self.start_balance = current_balance
            self.peak_balance = current_balance
            self._save()
            reset_v2_stats()
            logger.info(f" New day! Start Bal: ${current_balance:.2f}")

    def update_peak(self, current_balance: float):
        if current_balance > self.peak_balance:
            self.peak_balance = current_balance
            self._save()

    def add_loss(self):
        # REV 1.3.16: date rollover check BEFORE incrementing
        self._check_date_rollover()
        self.loss_count += 1
        self._save()
        logger.info(
            f" Daily Loss Count: {self.loss_count}/{MAX_DAILY_LOSS_TRADES}"
        )

    def is_limit_reached(self, current_equity: float) -> tuple[bool, str]:
        if current_equity < 100:
            return False, ""
        # REV 1.3.16: date rollover check at start
        self._check_date_rollover()
        self.reset_if_new_day(current_equity)
        if self.loss_count >= MAX_DAILY_LOSS_TRADES:
            return True, f"Loss count {self.loss_count}"
        if self.start_balance > 100:
            dd = (self.start_balance - current_equity) / self.start_balance * 100
            if dd >= MAX_DAILY_DRAWDOWN_PERCENT:
                return True, f"Daily DD {dd:.2f}%"
        if self.peak_balance > 100:
            acc_dd = (self.peak_balance - current_equity) / self.peak_balance * 100
            if acc_dd >= MAX_ACCOUNT_DRAWDOWN:
                return True, f"Account DD {acc_dd:.2f}%"
        return False, ""


daily_tracker = DailyTracker()


# ─────────────────────────────────────────────────────────────
# COIN ROTATION
# ─────────────────────────────────────────────────────────────
_ROTATION_WRITE_LOCK = threading.Lock()


def load_coin_rotation() -> dict:
    try:
        if os.path.exists(COIN_ROTATION_FILE):
            with open(COIN_ROTATION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"coin_rotation load error: {e}")
    return {}


def save_coin_rotation(data: dict) -> None:
    try:
        with _ROTATION_WRITE_LOCK:
            today = datetime.now(PKT).date()
            keys_to_del = []
            for k in data.keys():
                try:
                    d = datetime.strptime(k, '%Y-%m-%d').date()
                    if (today - d).days > 3:
                        keys_to_del.append(k)
                except Exception:
                    pass
            for k in keys_to_del:
                del data[k]
            _atomic_json_write(COIN_ROTATION_FILE, data)
    except Exception as e:
        logger.warning(f"coin_rotation save error: {e}")


def can_trade_coin(symbol: str) -> tuple[bool, str]:
    if symbol in BLACKLISTED_COINS:
        return False, f"{symbol} blacklisted"
    data = load_coin_rotation()
    today = str(datetime.now(PKT).date())
    count = data.get(today, {}).get(symbol, 0)
    if count >= MAX_TRADES_PER_COIN_PER_DAY:
        return False, f"{symbol} {count} trades today"
    return True, "OK"


def record_coin_trade(symbol: str) -> None:
    data = load_coin_rotation()
    today = str(datetime.now(PKT).date())
    if today not in data:
        data[today] = {}
    data[today][symbol] = data[today].get(symbol, 0) + 1
    save_coin_rotation(data)
    logger.info(f" [{symbol}] Daily trade: {data[today][symbol]}/{MAX_TRADES_PER_COIN_PER_DAY}")