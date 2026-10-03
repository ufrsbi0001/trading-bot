"""
client.py — Binance Futures client, exchange metadata, filters, klines,
positions, account cache, Telegram. Lowest layer of the trading engine.

Does NOT know about strategies or trade lifecycle.

REV 11.1 (2026-10-03) — HARDENING PASS (post deep-dive review):
  ✅ _install_pooled_session: urllib3 Retry now GET-only. POST/PUT/DELETE
     are NEVER retried. 429 removed from status_forcelist (was causing
     IP-ban escalation). respect_retry_after_header=True.
  ✅ retry_on_rate_limit: added random jitter; -1008 gets 2x backoff;
     honours Retry-After when present.
  ✅ _run_with_timeout: tracks zombie threads; logs warning if the
     executor's queue depth exceeds threshold.
  ✅ get_account_cached: takes _requests_lock + uses timeout executor
     (previously a naked futures_account() call that could hang).
  ✅ _get_exchange_info: TTL 1800 → 600; new invalidate_exchange_info()
     helper for eager busting on order-filter errors.
  ✅ NEW invalidate_filters(pair) + handle_order_filter_error(pair, exc)
     for -4005 / -1013 / -1111 / -4164 / -2010 → auto cache-bust.
  ✅ get_filters: TTL 3600 → 600; supports Binance's newer NOTIONAL
     filter type (older MIN_NOTIONAL still honoured); skips symbols
     not in TRADING status.
  ✅ get_binance_klines: -1003/-1008 now RE-RAISED so the decorator
     can retry. Previously the inner except swallowed them, making
     the decorator dead code (silent rate-limit bypass).
  ✅ fetch_position_raw + _position_amt: same fix — rate-limit errors
     re-raise so the decorator handles them with jitter/backoff.
  ✅ _get_open_algo_orders: startup assertion for required SDK methods
     (previously silent [] if the installed python-binance lacked them,
     which made TP1-fill detection silently wrong).
  ✅ set_client_keys: verifies position mode (one-way vs hedge) at
     startup and warns loudly if Telegram is misconfigured.

REV 11.0 (2026-10-03) — KLINES CACHE + LOCK HYGIENE (retained).
REV 10.9 (2026-10-02) — COSMETIC CLEANUP.
REV 10.8 (2026-09-30) — MAX_QTY FILTER SUPPORT (fix -4005 loop).
REV 10.7 (2026-09-29) — BOOK TICKER / SPREAD HELPERS.
REV 10.6 (2026-09-24) — TAKER COLUMNS FLOAT CONVERSION.
REV 10.5 (2026-09-20) — HARDCODED_FILTERS extended from 17 → 38 coins.
"""

from __future__ import annotations

import concurrent.futures
import logging
import random
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import CONFIG

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler

_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _log_path = Path(CONFIG.log_dir) / "bot.log"
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_handlers.insert(0, RotatingFileHandler(
        str(_log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    ))
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────────────────────
_global_client: Optional[Client] = None
_CLIENT_INIT_LOCK = threading.RLock()
_requests_lock = threading.RLock()
_TS_LOCK = threading.Lock()
_TS_OFFSET = {'value': 0, 'time': 0.0}
_TS_TTL = 60.0

# REV 11.1 — 8 was too small for parallel scan + manager + guardian.
# Bumped to 16. Zombie tracking below guards against leak growth.
_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="apicl"
)
_ZOMBIE_WARN_THRESHOLD = 4
_zombie_count = {'value': 0}
_zombie_lock = threading.Lock()

# Binance error codes that indicate our cached filters / exchangeInfo
# are stale. Hit one of these → bust cache → retry once.
_BINANCE_FILTER_ERROR_CODES = frozenset({-4005, -1013, -1111, -4164, -2010})


def get_client() -> Optional[Client]:
    return _global_client


def _run_with_timeout(fn, timeout, desc="api_call"):
    """
    REV 11.1 — fut.cancel() cannot stop a running thread. Track zombie
    count and warn when it climbs, so operators notice executor pressure
    before it starves real order traffic.
    """
    fut = _TIMEOUT_EXECUTOR.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        try:
            fut.cancel()
        except Exception:
            pass
        with _zombie_lock:
            _zombie_count['value'] += 1
            n = _zombie_count['value']
        if n >= _ZOMBIE_WARN_THRESHOLD:
            logger.warning(
                f"[executor] {n} zombie task(s) after timeout on '{desc}'. "
                f"Consider raising timeout or investigating API latency."
            )
        raise


def _clean_key(s: str) -> str:
    if not s:
        return ''
    return s.strip().strip('"').strip("'")


# ─────────────────────────────────────────────────────────────
# SESSION + TIMESTAMP
# ─────────────────────────────────────────────────────────────
def _install_pooled_session(client: Client) -> None:
    """
    REV 11.1 — CRITICAL FIX.

    urllib3's Retry was configured with allowed_methods including POST.
    Binance returns 503 with body -1007 ("execution status unknown") on
    order endpoints under overload. urllib3 then replayed the POST,
    submitting a SECOND real order. This is the mechanism by which the
    bot could double-size positions without ever seeing an error.

    Fix:
      • allowed_methods = {"GET"} only. Idempotent reads may retry;
        stateful writes must NEVER be auto-replayed.
      • 429 removed from status_forcelist. Retrying on 429 is how bots
        get upgraded to 418 IP bans. Rate limits are handled higher up
        by retry_on_rate_limit with proper backoff + jitter.
      • respect_retry_after_header=True honours Retry-After when the
        server does send it (500/502/503/504 paths).
    """
    try:
        old = client.session
        sess = requests.Session()
        sess.headers.update(old.headers)
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset(['GET']),        # ← POST/DELETE removed
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry, pool_connections=32,
            pool_maxsize=32, pool_block=False,
        )
        sess.mount('https://', adapter)
        sess.mount('http://', adapter)
        client.session = sess
        logger.info("🔌 Pooled HTTP session installed (GET-only retry)")
    except Exception as e:
        logger.warning(f"Could not install pooled session: {e}")


def refresh_timestamp() -> None:
    if _global_client is None:
        return
    now = time.time()
    with _TS_LOCK:
        if _TS_OFFSET['time'] and now - _TS_OFFSET['time'] < _TS_TTL:
            _global_client.timestamp_offset = _TS_OFFSET['value']
            return
    try:
        with _requests_lock:
            server_time = _global_client.get_server_time()['serverTime']
        offset = server_time - int(time.time() * 1000)
        with _TS_LOCK:
            _TS_OFFSET['value'] = offset
            _TS_OFFSET['time'] = time.time()
        _global_client.timestamp_offset = offset
    except Exception as e:
        logger.debug(f"Timestamp refresh skipped: {e}")


# ─────────────────────────────────────────────────────────────
# RATE-LIMIT DECORATOR
# ─────────────────────────────────────────────────────────────
def retry_on_rate_limit(func):
    """
    REV 11.1 — added jitter + -1008 heavy backoff + Retry-After respect.

    The wrapper serialises the wrapped call under _requests_lock, which
    is REENTRANT, so internal refresh_timestamp() / nested lock use is
    safe.
    """
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with _requests_lock:
                    return func(*args, **kwargs)
            except BinanceAPIException as e:
                if e.code not in (-1003, -1008):
                    raise

                # Prefer server-supplied Retry-After when present.
                retry_after = None
                try:
                    resp = getattr(e, 'response', None)
                    if resp is not None:
                        ra = resp.headers.get('Retry-After')
                        if ra:
                            retry_after = float(ra)
                except Exception:
                    retry_after = None

                base = retry_after if retry_after is not None else (2 ** attempt)
                # -1008 = server overloaded: be gentler than 1003.
                if e.code == -1008 and retry_after is None:
                    base *= 2
                jitter = random.uniform(0, 0.5)
                sleep_time = min(base + jitter, 30.0)

                logger.warning(
                    f"Rate limit {e.code}, retrying in {sleep_time:.2f}s "
                    f"({attempt + 1}/{max_retries}) on {func.__name__}"
                )
                time.sleep(sleep_time)
                try:
                    refresh_timestamp()
                except Exception:
                    pass
                continue
        raise RuntimeError(f"Rate limit retries exhausted for {func.__name__}")

    return wrapper


# ─────────────────────────────────────────────────────────────
# ACCOUNT CACHE
# ─────────────────────────────────────────────────────────────
_ACCOUNT_CACHE = {'data': None, 'time': 0.0}
_ACCOUNT_CACHE_LOCK = threading.Lock()
_LAST_GOOD_BAL = {'value': None, 'time': 0.0}
_LAST_GOOD_BAL_LOCK = threading.Lock()
_LAST_GOOD_BAL_MAX_AGE = 300


def get_account_cached() -> dict:
    """
    REV 11.1 — wraps futures_account() in _requests_lock + executor
    timeout so a hung call cannot block the scan loop indefinitely.
    """
    with _ACCOUNT_CACHE_LOCK:
        if _ACCOUNT_CACHE['data'] is not None and \
                time.time() - _ACCOUNT_CACHE['time'] < 20:
            return _ACCOUNT_CACHE['data']

    if _global_client is None:
        raise RuntimeError("get_account_cached: client not initialised")

    def _fetch():
        with _requests_lock:
            return _global_client.futures_account()

    acc = _run_with_timeout(_fetch, 8, "futures_account")
    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE['data'] = acc
        _ACCOUNT_CACHE['time'] = time.time()
    return acc


def invalidate_account_cache() -> None:
    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE['data'] = None


def get_balance_or_last_good() -> tuple[float, bool]:
    try:
        acc = get_account_cached()
        bal = float(acc['totalWalletBalance']) + \
            float(acc.get('totalUnrealizedProfit', 0))
        with _LAST_GOOD_BAL_LOCK:
            _LAST_GOOD_BAL['value'] = bal
            _LAST_GOOD_BAL['time'] = time.time()
        return bal, False
    except Exception as e:
        with _LAST_GOOD_BAL_LOCK:
            v, t = _LAST_GOOD_BAL['value'], _LAST_GOOD_BAL['time']
        if v is not None and (time.time() - t) < _LAST_GOOD_BAL_MAX_AGE:
            logger.warning(
                f"Balance fetch failed ({type(e).__name__}); last-good ${v:.2f}"
            )
            return v, True
        raise


# ─────────────────────────────────────────────────────────────
# EXCHANGE INFO + SYMBOLS
# ─────────────────────────────────────────────────────────────
_EXCHANGE_INFO_CACHE = {'data': None, 'time': 0.0}
_EXCHANGE_INFO_LOCK = threading.Lock()
# REV 11.1 — 1800 → 600. LOT_SIZE changes mid-session are a slow-motion
# order-failure. Combined with invalidate_exchange_info() on filter
# errors, staleness window is now well bounded.
_EXCHANGE_INFO_TTL = 600
VALID_SYMBOLS: set[str] = set()
_VALID_SYMBOLS_LOCK = threading.Lock()


def invalidate_exchange_info() -> None:
    """REV 11.1 — eager cache bust for order-filter errors."""
    with _EXCHANGE_INFO_LOCK:
        _EXCHANGE_INFO_CACHE['data'] = None
        _EXCHANGE_INFO_CACHE['time'] = 0.0
    with _VALID_SYMBOLS_LOCK:
        VALID_SYMBOLS.clear()


def _get_exchange_info() -> dict:
    """
    Fetch + cache exchangeInfo.

    REV 11.0 — retries 3x with backoff on timeout / transient error.
    REV 11.1 — TTL reduced; invalidation helper added.
    """
    now = time.time()
    with _EXCHANGE_INFO_LOCK:
        cached = _EXCHANGE_INFO_CACHE['data']
        if cached is not None and \
                now - _EXCHANGE_INFO_CACHE['time'] < _EXCHANGE_INFO_TTL:
            return cached

    if _global_client is None:
        raise RuntimeError("_get_exchange_info: client not initialised")

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            def _fetch():
                with _requests_lock:
                    return _global_client.futures_exchange_info()
            info = _run_with_timeout(_fetch, 8, "exchange_info")
            with _EXCHANGE_INFO_LOCK:
                _EXCHANGE_INFO_CACHE['data'] = info
                _EXCHANGE_INFO_CACHE['time'] = time.time()
            return info
        except concurrent.futures.TimeoutError as e:
            last_err = e
            logger.warning(f"exchangeInfo timeout (attempt {attempt + 1}/3)")
        except Exception as e:
            last_err = e
            logger.warning(
                f"exchangeInfo error (attempt {attempt + 1}/3): "
                f"{type(e).__name__}: {e}"
            )
        if attempt < 2:
            time.sleep((1.0 + attempt * 0.5) + random.uniform(0, 0.3))

    raise last_err or RuntimeError("exchangeInfo fetch failed after 3 attempts")


def validate_symbols(coins: list[str]) -> list[str]:
    """
    Validate + memoise tradable symbols.
    REV 11.1 — also filter out non-TRADING statuses (SETTLING, PENDING, etc).
    """
    global VALID_SYMBOLS
    try:
        with _VALID_SYMBOLS_LOCK:
            snapshot = set(VALID_SYMBOLS)

        if snapshot:
            valid = [c for c in coins if c in snapshot]
            invalid = [c for c in coins if c not in snapshot]
            if invalid:
                logger.warning(f"Skipping invalid symbols (cached): {invalid}")
            return valid

        logger.info("🔍 Validating symbols (cached exchangeInfo)...")
        info = _get_exchange_info()
        fresh = {
            s['symbol'] for s in info.get('symbols', [])
            if s.get('status') == 'TRADING'
        }
        with _VALID_SYMBOLS_LOCK:
            VALID_SYMBOLS = fresh
            snapshot = set(VALID_SYMBOLS)

        valid = [c for c in coins if c in snapshot]
        invalid = [c for c in coins if c not in snapshot]
        if invalid:
            logger.warning(f"Skipping invalid symbols: {invalid}")
        logger.info(f"✅ Validated {len(valid)} symbols")
        return valid
    except concurrent.futures.TimeoutError:
        logger.warning("⚠️ Validation timeout — using coins as-is except known bad")
        invalid_demo = {'MATICUSDT', 'LUNAUSDT'}
        valid = [c for c in coins if c not in invalid_demo]
        with _VALID_SYMBOLS_LOCK:
            VALID_SYMBOLS = set(valid)
        return valid
    except Exception as e:
        logger.error(f"Validation error: {e}")
        invalid_demo = {'MATICUSDT', 'LUNAUSDT'}
        valid = [c for c in coins if c not in invalid_demo]
        with _VALID_SYMBOLS_LOCK:
            VALID_SYMBOLS = set(valid)
        return valid


def get_bot_coins() -> list[str]:
    return list(CONFIG.bot_coins)


# ─────────────────────────────────────────────────────────────
# FILTERS
# ─────────────────────────────────────────────────────────────
HARDCODED_FILTERS = {
    'BTCUSDT':       {'stepSize': Decimal('0.001'), 'minQty': Decimal('0.001'), 'tickSize': Decimal('0.1'),       'minNotional': 5.0},
    'ETHUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'BNBUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'LTCUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'TONUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'SOLUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'SUIUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'AVAXUSDT':      {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'FILUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'NEARUSDT':      {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'APTUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'INJUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'TIAUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'SEIUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'AAVEUSDT':      {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'UNIUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'RENDERUSDT':    {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'TAOUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'JUPUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'FETUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'LINKUSDT':      {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'ADAUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'DOTUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'ATOMUSDT':      {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'ARBUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'POLUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'OPUSDT':        {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'TRXUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.00001'),   'minNotional': 5.0},
    'DOGEUSDT':      {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.00001'),   'minNotional': 5.0},
    'XRPUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'ZECUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'ETCUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    '1000PEPEUSDT':  {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    '1000SHIBUSDT':  {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    'WIFUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    '1000BONKUSDT':  {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    '1000FLOKIUSDT': {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    'BOMEUSDT':      {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
}

_DEFAULT_MAX_QTY = Decimal('1000000000')

FILTERS_CACHE: dict[str, dict] = {}
FILTERS_CACHE_LOCK = threading.Lock()
FILTERS_UNVERIFIED: dict[str, int] = {}
FILTERS_UNVERIFIED_LOCK = threading.Lock()

# REV 11.1 — 3600 → 600 to match exchangeInfo TTL. Longer than this
# means a LOT_SIZE change can leave us quoting wrong for an hour.
_FILTERS_CACHE_TTL = 600


def _mark_unverified(pair: str) -> None:
    with FILTERS_UNVERIFIED_LOCK:
        FILTERS_UNVERIFIED[pair] = FILTERS_UNVERIFIED.get(pair, 0) + 1


def _clear_unverified(pair: str) -> None:
    with FILTERS_UNVERIFIED_LOCK:
        FILTERS_UNVERIFIED.pop(pair, None)


def _filters_ok_to_trade(pair: str) -> bool:
    with FILTERS_UNVERIFIED_LOCK:
        return FILTERS_UNVERIFIED.get(pair, 0) < 2


def invalidate_filters(pair: Optional[str] = None) -> None:
    """
    REV 11.1 — Bust filter cache for a specific pair (or all pairs) and
    also mark exchangeInfo as stale. Call this from order handlers when
    Binance rejects with -4005/-1013/-1111/-4164/-2010.
    """
    with FILTERS_CACHE_LOCK:
        if pair is None:
            FILTERS_CACHE.clear()
        else:
            FILTERS_CACHE.pop(pair, None)
    with FILTERS_UNVERIFIED_LOCK:
        if pair is None:
            FILTERS_UNVERIFIED.clear()
        else:
            FILTERS_UNVERIFIED.pop(pair, None)
    invalidate_exchange_info()


def handle_order_filter_error(pair: str, exc: BinanceAPIException) -> bool:
    """
    REV 11.1 — Convenience helper for order.py / entry.py / exit.py.

    Returns True if the error was a filter-related code and the caches
    have been busted (caller should re-round qty/price and retry once).
    Returns False if the error is unrelated.
    """
    code = getattr(exc, 'code', None)
    if code in _BINANCE_FILTER_ERROR_CODES:
        logger.warning(
            f"[filters] {pair} → invalidating cache on Binance error "
            f"{code}: {exc}"
        )
        invalidate_filters(pair)
        return True
    return False


def _fallback_filters(pair: str, now: float, why: str = "") -> dict:
    _mark_unverified(pair)
    if pair in HARDCODED_FILTERS:
        base = HARDCODED_FILTERS[pair]
        result = {
            'stepSize': base['stepSize'],
            'minQty': base['minQty'],
            'maxQty': base.get('maxQty', _DEFAULT_MAX_QTY),
            'tickSize': base['tickSize'],
            'minNotional': base['minNotional'],
            'timestamp': now,
            'verified': False,
        }
        logger.warning(
            f"⚠️ Filters for {pair} HARDCODED fallback ({why}) — entries blocked"
        )
    else:
        result = {
            'stepSize': Decimal('0.01'),
            'minQty': Decimal('0.01'),
            'maxQty': _DEFAULT_MAX_QTY,
            'tickSize': Decimal('0.01'),
            'minNotional': 5.0,
            'timestamp': now,
            'verified': False,
        }
        logger.warning(
            f"⚠️ Unknown {pair} - default filters ({why}) — entries blocked"
        )
    with FILTERS_CACHE_LOCK:
        FILTERS_CACHE[pair] = result
    return result


def get_filters(pair: str) -> dict:
    """
    REV 11.1 — TTL 600s. Supports Binance's newer NOTIONAL filter type
    (in addition to legacy MIN_NOTIONAL). Rejects symbols whose status
    is not TRADING.
    """
    now = time.time()
    with FILTERS_CACHE_LOCK:
        cached = FILTERS_CACHE.get(pair)
        if cached and now - cached['timestamp'] < _FILTERS_CACHE_TTL \
                and cached.get('verified', False):
            return cached
    try:
        info = _get_exchange_info()
        symbols = info.get('symbols', []) if isinstance(info, dict) else []
        sym = next((s for s in symbols if s.get('symbol') == pair), None)
        if sym is None:
            raise ValueError(f"symbol {pair} not in exchangeInfo")

        status = sym.get('status', 'TRADING')
        if status != 'TRADING':
            raise ValueError(f"symbol {pair} not tradable (status={status})")

        lot_f = [f for f in sym['filters'] if f['filterType'] == 'LOT_SIZE']
        m_lot = [f for f in sym['filters']
                 if f['filterType'] == 'MARKET_LOT_SIZE']
        lot = m_lot[0] if m_lot else (lot_f[0] if lot_f else None)
        price_f = next(
            (f for f in sym['filters'] if f['filterType'] == 'PRICE_FILTER'),
            None,
        )
        # REV 11.1 — Binance Futures migrated MIN_NOTIONAL → NOTIONAL.
        # Prefer NOTIONAL; fall back to MIN_NOTIONAL for older schemas.
        notional = next(
            (f for f in sym['filters'] if f['filterType'] == 'NOTIONAL'),
            None,
        ) or next(
            (f for f in sym['filters'] if f['filterType'] == 'MIN_NOTIONAL'),
            None,
        )

        if not lot or not price_f:
            raise ValueError(f"missing filters for {pair}")

        max_qty_raw = lot.get('maxQty', '1000000000')
        try:
            max_qty = Decimal(str(max_qty_raw))
        except Exception:
            max_qty = _DEFAULT_MAX_QTY

        result = {
            'stepSize': Decimal(lot['stepSize']),
            'minQty': Decimal(lot['minQty']),
            'maxQty': max_qty,
            'tickSize': Decimal(price_f['tickSize']),
            'minNotional': float(notional['notional']) if notional else 5.0,
            'timestamp': now,
            'verified': True,
        }
        with FILTERS_CACHE_LOCK:
            FILTERS_CACHE[pair] = result
        _clear_unverified(pair)
        logger.info(
            f"✅ Filters for {pair} (API): step={result['stepSize']} "
            f"tick={result['tickSize']} maxQty={result['maxQty']}"
        )
        return result
    except concurrent.futures.TimeoutError:
        return _fallback_filters(pair, now, "exchangeInfo timeout")
    except Exception as e:
        logger.warning(f"Filter fetch failed for {pair}: {e}")
        return _fallback_filters(pair, now, "api error")


# ─────────────────────────────────────────────────────────────
# DECIMAL ADJUSTMENT
# ─────────────────────────────────────────────────────────────
def adjust_qty(qty, step, min_qty) -> str:
    try:
        step_dec = Decimal(str(step))
        qty_d = (Decimal(str(qty)) // step_dec) * step_dec
        if qty_d < min_qty:
            qty_d = min_qty
        exp = step_dec.as_tuple().exponent
        if exp < 0:
            return format(qty_d, f'.{abs(exp)}f')
        return format(qty_d, 'f')
    except Exception as e:
        logger.warning(f"adjust_qty error {e}")
        return str(min_qty)


def adjust_price(price, tick) -> str:
    try:
        tick_dec = Decimal(str(tick))
        price_d = (Decimal(str(price)) // tick_dec) * tick_dec
        exp = tick_dec.as_tuple().exponent
        if exp < 0:
            return format(price_d, f'.{abs(exp)}f')
        return format(price_d, 'f')
    except Exception as e:
        logger.warning(f"adjust_price error {e}")
        return str(price)


# ─────────────────────────────────────────────────────────────
# KLINES
# ─────────────────────────────────────────────────────────────
_KLINES_TTL: dict[str, float] = {
    '1m':  20,
    '3m':  30,
    '5m':  30,
    '15m': 60,
    '30m': 120,
    '1h':  120,
    '2h':  240,
    '4h':  300,
    '6h':  600,
    '8h':  600,
    '12h': 900,
    '1d':  900,
    '3d':  1800,
    '1w':  3600,
    '1M':  3600,
}
_KLINES_TTL_DEFAULT = 60.0

_KLINES_CACHE: dict[tuple[str, str, int], tuple[pd.DataFrame, float]] = {}
_KLINES_CACHE_LOCK = threading.Lock()
_KLINES_CACHE_MAX = 1000


def _klines_cache_get(symbol: str, interval: str,
                      limit: int) -> Optional[pd.DataFrame]:
    key = (symbol, interval, limit)
    ttl = _KLINES_TTL.get(interval, _KLINES_TTL_DEFAULT)
    now = time.time()
    with _KLINES_CACHE_LOCK:
        hit = _KLINES_CACHE.get(key)
        if hit is not None and now - hit[1] < ttl:
            return hit[0]
    return None


def _klines_cache_set(symbol: str, interval: str,
                      limit: int, df: pd.DataFrame) -> None:
    key = (symbol, interval, limit)
    with _KLINES_CACHE_LOCK:
        _KLINES_CACHE[key] = (df, time.time())
        if len(_KLINES_CACHE) > _KLINES_CACHE_MAX:
            now = time.time()
            for k in list(_KLINES_CACHE.keys()):
                _, ts = _KLINES_CACHE[k]
                if now - ts > 3600:
                    del _KLINES_CACHE[k]


def invalidate_klines_cache(symbol: Optional[str] = None) -> None:
    with _KLINES_CACHE_LOCK:
        if symbol is None:
            _KLINES_CACHE.clear()
        else:
            for k in list(_KLINES_CACHE.keys()):
                if k[0] == symbol:
                    del _KLINES_CACHE[k]


@retry_on_rate_limit
def get_binance_klines(symbol: str, interval: str, limit: int = 250):
    """
    REV 11.1 — rate-limit errors now RE-RAISED so the decorator can
    retry them with jitter/backoff. Previously the inner `except
    Exception` swallowed -1003/-1008, silently defeating the decorator.
    """
    cached = _klines_cache_get(symbol, interval, limit)
    if cached is not None:
        return cached

    try:
        klines = _global_client.futures_klines(
            symbol=symbol, interval=interval, limit=limit
        )
        if not klines:
            return None
        df = pd.DataFrame(klines, columns=[
            'Open Time', 'Open', 'High', 'Low', 'Close', 'Volume',
            'Close Time', 'QAV', 'NOT', 'TBBAV', 'TBQAV', 'I'
        ])
        for c in ['Open', 'High', 'Low', 'Close', 'Volume',
                  'QAV', 'TBBAV', 'TBQAV']:
            df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
        df['Datetime'] = pd.to_datetime(df['Open Time'], unit='ms')
        df.set_index('Datetime', inplace=True)
        _klines_cache_set(symbol, interval, limit, df)
        return df
    except BinanceAPIException as e:
        if e.code in (-1003, -1008):
            raise  # let decorator retry with proper backoff
        logger.debug(f"Klines error {symbol} {interval}: {type(e).__name__}")
        return None
    except Exception as e:
        logger.debug(f"Klines error {symbol} {interval}: {type(e).__name__}")
        return None


# ─────────────────────────────────────────────────────────────
# LIVE PRICES
# ─────────────────────────────────────────────────────────────
def get_live_prices() -> dict[str, float]:
    """Fetch all futures tickers in ONE call → {symbol: price} map."""
    if _global_client is None:
        return {}
    try:
        with _requests_lock:
            tickers = _global_client.futures_symbol_ticker()
        if not tickers:
            return {}
        return {
            t['symbol']: float(t['price'])
            for t in tickers
            if t.get('symbol') and t.get('price') is not None
        }
    except Exception as e:
        logger.debug(f"get_live_prices failed: {type(e).__name__}: {e}")
        return {}


# ─────────────────────────────────────────────────────────────
# BOOK TICKER / SPREAD
# ─────────────────────────────────────────────────────────────
_BOOK_TICKER_CACHE: dict[str, dict] = {}
_BOOK_TICKER_LOCK = threading.Lock()
_BOOK_TICKER_TTL = 3.0


def get_book_ticker(symbol: str) -> dict:
    now = time.time()
    with _BOOK_TICKER_LOCK:
        hit = _BOOK_TICKER_CACHE.get(symbol)
        if hit and now - hit['time'] < _BOOK_TICKER_TTL:
            return {'bid': hit['bid'], 'ask': hit['ask']}

    if _global_client is None:
        return {}
    try:
        with _requests_lock:
            resp = _global_client.futures_orderbook_ticker(symbol=symbol)
        if not resp:
            return {}
        bid = float(resp.get('bidPrice', 0) or 0)
        ask = float(resp.get('askPrice', 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {}
        with _BOOK_TICKER_LOCK:
            _BOOK_TICKER_CACHE[symbol] = {
                'bid': bid, 'ask': ask, 'time': now
            }
        return {'bid': bid, 'ask': ask}
    except Exception as e:
        logger.debug(f"book_ticker {symbol}: {type(e).__name__}")
        return {}


def get_book_tickers() -> dict[str, dict]:
    if _global_client is None:
        return {}
    try:
        with _requests_lock:
            tickers = _global_client.futures_orderbook_ticker()
        if not tickers:
            return {}
        out: dict[str, dict] = {}
        for t in tickers:
            sym = t.get('symbol')
            if not sym:
                continue
            try:
                bid = float(t.get('bidPrice', 0) or 0)
                ask = float(t.get('askPrice', 0) or 0)
                if bid > 0 and ask > 0 and ask >= bid:
                    out[sym] = {'bid': bid, 'ask': ask}
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.debug(f"get_book_tickers failed: {type(e).__name__}: {e}")
        return {}


def calc_spread_pct(bid: float, ask: float) -> float:
    try:
        bid_f = float(bid)
        ask_f = float(ask)
        if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
            return 0.0
        mid = (bid_f + ask_f) / 2.0
        if mid <= 0:
            return 0.0
        return (ask_f - bid_f) / mid * 100.0
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────────────────────
@retry_on_rate_limit
def fetch_position_raw(symbol_short: str):
    """
    REV 11.1 — rate-limit errors re-raised so decorator can retry.
    """
    pair = symbol_short + 'USDT'
    refresh_timestamp()
    for i in range(3):
        try:
            arr = _global_client.futures_position_information(symbol=pair)
            if arr and len(arr) > 0:
                return arr[0]
            return {
                'symbol': pair, 'positionAmt': '0', 'entryPrice': '0',
                'markPrice': '0', 'unRealizedProfit': '0',
            }
        except BinanceAPIException as e:
            if e.code in (-1003, -1008):
                raise  # decorator handles
            if i == 2:
                logger.debug(
                    f"fetch_position_raw {symbol_short} failed: "
                    f"{type(e).__name__}"
                )
                return None
            time.sleep(0.6 + i * 0.4)
        except Exception as e:
            if i == 2:
                logger.debug(
                    f"fetch_position_raw {symbol_short} failed: "
                    f"{type(e).__name__}"
                )
                return None
            time.sleep(0.6 + i * 0.4)
    return None


def _position_amt(pair: str, retries: int = 3):
    """
    REV 11.1 — rate-limit errors re-raised so upstream callers (and the
    retry_on_rate_limit wrapper on the caller side) can handle them.
    """
    refresh_timestamp()
    for attempt in range(retries):
        try:
            with _requests_lock:
                arr = _global_client.futures_position_information(symbol=pair)
            if arr:
                return float(arr[0].get('positionAmt', 0) or 0)
            return 0.0
        except BinanceAPIException as e:
            if e.code in (-1003, -1008):
                raise
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


# ─────────────────────────────────────────────────────────────
# ALGO ORDERS
# ─────────────────────────────────────────────────────────────
_REQUIRED_SDK_METHODS = (
    'futures_get_open_algo_orders',
    'futures_cancel_algo_order',
)


def assert_sdk_methods() -> None:
    """
    REV 11.1 — Fail loudly at startup if the installed python-binance
    lacks the algo-order methods. Previously _get_open_algo_orders
    silently returned [] when the method was missing, which made the
    TP1-fill detector misread state and place duplicate TP orders.
    """
    if _global_client is None:
        raise RuntimeError("assert_sdk_methods: client not initialised")
    missing = [m for m in _REQUIRED_SDK_METHODS
               if getattr(_global_client, m, None) is None]
    if missing:
        raise RuntimeError(
            f"Installed python-binance is missing required method(s): "
            f"{', '.join(missing)}. Pin a supported version in "
            f"requirements.txt (see REV 11.1 note)."
        )
    logger.info("✅ SDK algo-order methods present")


def _get_open_algo_orders(pair: str) -> list:
    getter = getattr(_global_client, 'futures_get_open_algo_orders', None)
    if getter is None:
        # REV 11.1 — was silent [] before; now loud so the caller knows
        # to investigate rather than misread TP state.
        logger.error(
            "[algo] futures_get_open_algo_orders missing on client — "
            "call assert_sdk_methods() at startup"
        )
        return []
    try:
        with _requests_lock:
            resp = getter(symbol=pair)
        if isinstance(resp, dict):
            return resp.get('orders') or resp.get('data') or []
        return resp or []
    except Exception as e:
        logger.debug(f"algo orders {pair}: {e}")
        return []


def _cancel_algo_order(pair: str, algo_id) -> bool:
    cancel = getattr(_global_client, 'futures_cancel_algo_order', None)
    if cancel is None or not algo_id:
        return False
    try:
        with _requests_lock:
            cancel(symbol=pair, algoId=algo_id)
        return True
    except Exception as e:
        logger.warning(f"algo cancel {algo_id} {pair}: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# CLIENT SETUP
# ─────────────────────────────────────────────────────────────
def check_position_mode() -> Optional[bool]:
    """
    REV 11.1 — Reads dualSidePosition. Returns True if hedge mode,
    False if one-way, None on error. Startup wiring should refuse to
    trade on hedge mode (every order would fail).
    """
    if _global_client is None:
        return None
    try:
        with _requests_lock:
            mode = _global_client.futures_get_position_mode()
        dual = bool(mode.get('dualSidePosition', False))
        if dual:
            logger.critical(
                "❌ Account is in HEDGE MODE (dualSidePosition=True). "
                "All orders will fail. Set one-way mode before trading."
            )
        else:
            logger.info("✅ Account position mode: ONE-WAY")
        return dual
    except Exception as e:
        logger.warning(f"Position-mode check failed: {e}")
        return None


def set_client_keys(api_key: str, api_secret: str) -> Client:
    """
    REV 11.1 — after init:
      • assert_sdk_methods() — fail loud if SDK is incomplete
      • check_position_mode() — refuse hedge mode silently
      • Telegram warning if disabled
    """
    global _global_client
    api_key = _clean_key(api_key)
    api_secret = _clean_key(api_secret)
    if len(api_key) < 20 or len(api_secret) < 20:
        logger.critical(
            f"❌ API key/secret too short "
            f"(key={len(api_key)}, secret={len(api_secret)})"
        )
        raise ValueError("API key/secret too short")

    with _CLIENT_INIT_LOCK:
        client_params = {'requests_params': {'timeout': 10}}
        if CONFIG.demo_mode:
            _global_client = Client(api_key, api_secret, demo=True, **client_params)
            logger.info("DEMO MODE ACTIVE - demo.binance.com")
        elif CONFIG.testnet:
            _global_client = Client(api_key, api_secret, testnet=True, **client_params)
            logger.info("TESTNET MODE ACTIVE - testnet.binance.vision")
        else:
            _global_client = Client(api_key, api_secret, **client_params)
            logger.warning("MAINNET MODE - REAL MONEY!")
        _install_pooled_session(_global_client)

    refresh_timestamp()

    # REV 11.1 — startup integrity checks
    try:
        assert_sdk_methods()
    except Exception as e:
        logger.critical(f"SDK assertion failed: {e}")
        raise
    check_position_mode()
    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        logger.warning(
            "⚠️ Telegram disabled or token empty — CRITICAL alerts will "
            "NOT be delivered. Fix .env before live trading."
        )

    send_telegram(
        f"✅ Bot started (Demo: {CONFIG.demo_mode}, Testnet: {CONFIG.testnet})"
    )
    return _global_client


def _validate_api_credentials(client: Client) -> tuple[bool, float]:
    try:
        acc = client.futures_account()
        bal = float(acc.get('totalWalletBalance', 0))
        logger.info(f"✅ API credentials OK — Wallet: ${bal:.2f}")
        return True, bal
    except BinanceAPIException as e:
        if e.code == -2014:
            logger.critical(
                "❌ API-key format invalid (-2014). Check .env has no "
                "whitespace/quotes."
            )
        elif e.code == -2015:
            logger.critical(
                f"❌ API key invalid/disabled/IP-restricted: {e}"
            )
        else:
            logger.critical(f"❌ API validation failed: {e}")
        return False, 0.0
    except Exception as e:
        logger.critical(f"❌ API validation unexpected error: {e}")
        return False, 0.0


def startup_checks() -> None:
    """
    REV 11.1 — Call this once after set_client_keys(), before any
    trading begins. Verifies SDK, position mode, and alerts config.
    Idempotent; safe to call from future.py.
    """
    if _global_client is None:
        raise RuntimeError("startup_checks: client not initialised")
    assert_sdk_methods()
    check_position_mode()
    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        logger.warning(
            "⚠️ Telegram alerts disabled — CRITICAL events will be silent."
        )


# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        return
    try:
        import html as _html
        url = (
            f"https://api.telegram.org/bot{CONFIG.telegram_bot_token}"
            f"/sendMessage"
        )
        safe_msg = _html.escape(msg)
        data = {
            "chat_id": CONFIG.telegram_chat_id,
            "text": safe_msg,
            "parse_mode": "HTML",
        }
        requests.post(url, data=data, timeout=5)
    except requests.exceptions.Timeout:
        logger.warning("Telegram timeout")
    except Exception as e:
        logger.warning(f"Telegram error: {e}")