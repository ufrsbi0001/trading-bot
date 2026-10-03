"""
client.py — Binance Futures client, exchange metadata, filters, klines,
positions, account cache, Telegram. Lowest layer of the trading engine.

Does NOT know about strategies or trade lifecycle.

REV 11.0 (2026-10-03) — KLINES CACHE + LOCK HYGIENE:
  ✅ ADDED: _KLINES_CACHE with per-interval TTL. Previously every
     get_binance_klines() call hit the API unconditionally. With 52
     coins × up to 3 TFs × a 15 s scan cycle, that was ~156 API calls
     per cycle just for klines (a real rate-limit risk). Adaptive TTLs
     (1m=20s … 1d=900s) keep data fresh while absorbing scan-loop
     re-reads. Cache returns the SAME DataFrame object — callers must
     not mutate it in place (all current callers are read-only).
  ✅ ADDED: _invalidate_klines_cache(symbol=None) helper for manual
     busting after major news / on demand.
  ✅ FIXED: fetch_position_raw() now calls refresh_timestamp() ONCE
     before the retry loop — matching _position_amt(). The REV 10.9
     comment claimed these matched but they didn't.
  ✅ FIXED: set_client_keys() now guarded by _CLIENT_INIT_LOCK. Two
     concurrent calls could otherwise race on the _global_client
     assignment and leave partially-initialised state visible to
     readers.
  ✅ FIXED: _get_exchange_info() retries 3× with backoff on timeout
     or transient error before giving up. Previously a single timeout
     immediately degraded get_filters() to the fallback path.
  ✅ FIXED: validate_symbols() now writes VALID_SYMBOLS under a lock,
     so concurrent callers can't interleave and see a partial set.

REV 10.9 (2026-10-02) — COSMETIC CLEANUP.
REV 10.8 (2026-09-30) — MAX_QTY FILTER SUPPORT (fix -4005 loop).
REV 10.7 (2026-09-29) — BOOK TICKER / SPREAD HELPERS.
REV 10.6 (2026-09-24) — TAKER COLUMNS FLOAT CONVERSION.
REV 10.5 (2026-09-20) — HARDCODED_FILTERS extended from 17 → 38 coins.
"""


from __future__ import annotations

import concurrent.futures
import logging
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
        str(_log_path), maxBytes=5*1024*1024, backupCount=3, encoding='utf-8'
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
_CLIENT_INIT_LOCK = threading.RLock()   # REV 11.0 — guards _global_client setup
_requests_lock = threading.RLock()
_TS_LOCK = threading.Lock()
_TS_OFFSET = {'value': 0, 'time': 0.0}
_TS_TTL = 60.0

_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="apicl"
)


def get_client() -> Optional[Client]:
    return _global_client


def _run_with_timeout(fn, timeout, desc="api_call"):
    fut = _TIMEOUT_EXECUTOR.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        try:
            fut.cancel()
        except Exception:
            pass
        raise


def _clean_key(s: str) -> str:
    if not s:
        return ''
    return s.strip().strip('"').strip("'")


# ─────────────────────────────────────────────────────────────
# SESSION + TIMESTAMP
# ─────────────────────────────────────────────────────────────
def _install_pooled_session(client: Client) -> None:
    try:
        old = client.session
        sess = requests.Session()
        sess.headers.update(old.headers)
        retry = Retry(
            total=3, connect=3, read=3, backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE'])
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16,
                              pool_maxsize=16, pool_block=False)
        sess.mount('https://', adapter)
        sess.mount('http://', adapter)
        client.session = sess
        logger.info("🔌 Pooled HTTP session installed")
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
    from functools import wraps
    @wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 5
        base_delay = 1
        for attempt in range(max_retries):
            try:
                with _requests_lock:
                    result = func(*args, **kwargs)
                return result
            except BinanceAPIException as e:
                if e.code in (-1003, -1008):
                    sleep_time = base_delay * (2 ** attempt)
                    logger.warning(f"Rate limit hit, retrying in {sleep_time:.1f}s ({attempt+1}/{max_retries})")
                    time.sleep(sleep_time)
                    refresh_timestamp()
                    continue
                raise
        raise Exception(f"Rate limit retries exhausted for {func.__name__}")
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
    with _ACCOUNT_CACHE_LOCK:
        if _ACCOUNT_CACHE['data'] is not None and time.time() - _ACCOUNT_CACHE['time'] < 20:
            return _ACCOUNT_CACHE['data']
    acc = _global_client.futures_account()
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
        bal = float(acc['totalWalletBalance']) + float(acc.get('totalUnrealizedProfit', 0))
        with _LAST_GOOD_BAL_LOCK:
            _LAST_GOOD_BAL['value'] = bal
            _LAST_GOOD_BAL['time'] = time.time()
        return bal, False
    except Exception as e:
        with _LAST_GOOD_BAL_LOCK:
            v, t = _LAST_GOOD_BAL['value'], _LAST_GOOD_BAL['time']
        if v is not None and (time.time() - t) < _LAST_GOOD_BAL_MAX_AGE:
            logger.warning(f"Balance fetch failed ({type(e).__name__}); last-good ${v:.2f}")
            return v, True
        raise


# ─────────────────────────────────────────────────────────────
# EXCHANGE INFO + SYMBOLS
# ─────────────────────────────────────────────────────────────
_EXCHANGE_INFO_CACHE = {'data': None, 'time': 0.0}
_EXCHANGE_INFO_LOCK = threading.Lock()
_EXCHANGE_INFO_TTL = 1800
VALID_SYMBOLS: set[str] = set()
_VALID_SYMBOLS_LOCK = threading.Lock()   # REV 11.0


def _get_exchange_info() -> dict:
    """
    Fetch + cache exchangeInfo.

    REV 11.0 — retries up to 3× with backoff on timeout / transient
    error. Previously a single timeout degraded get_filters() to the
    hardcoded fallback path, blocking live entries for a full TTL.
    """
    now = time.time()
    with _EXCHANGE_INFO_LOCK:
        cached = _EXCHANGE_INFO_CACHE['data']
        if cached is not None and now - _EXCHANGE_INFO_CACHE['time'] < _EXCHANGE_INFO_TTL:
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
            logger.warning(f"exchangeInfo timeout (attempt {attempt+1}/3)")
        except Exception as e:
            last_err = e
            logger.warning(f"exchangeInfo error (attempt {attempt+1}/3): {type(e).__name__}: {e}")
        if attempt < 2:
            time.sleep(1.0 + attempt * 0.5)

    raise last_err or RuntimeError("exchangeInfo fetch failed after 3 attempts")


def validate_symbols(coins: list[str]) -> list[str]:
    """
    Validate + memoise tradable symbols.

    REV 11.0 — VALID_SYMBOLS written under lock so concurrent callers
    can't observe a partially-populated set.
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
        fresh = {s['symbol'] for s in info['symbols'] if s['status'] == 'TRADING'}
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
# FILTERS — REV 10.8: maxQty support
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

# REV 10.8 — Safe default maxQty for fallback path.
# 1 billion units is far above any realistic per-order maxQty on
# Binance Futures, so no false caps will be applied if the fallback
# fires. The primary API path reads the real maxQty from exchangeInfo.
_DEFAULT_MAX_QTY = Decimal('1000000000')

FILTERS_CACHE: dict[str, dict] = {}
FILTERS_CACHE_LOCK = threading.Lock()
FILTERS_UNVERIFIED: dict[str, int] = {}
FILTERS_UNVERIFIED_LOCK = threading.Lock()


def _mark_unverified(pair: str) -> None:
    with FILTERS_UNVERIFIED_LOCK:
        FILTERS_UNVERIFIED[pair] = FILTERS_UNVERIFIED.get(pair, 0) + 1


def _clear_unverified(pair: str) -> None:
    with FILTERS_UNVERIFIED_LOCK:
        FILTERS_UNVERIFIED.pop(pair, None)


def _filters_ok_to_trade(pair: str) -> bool:
    with FILTERS_UNVERIFIED_LOCK:
        return FILTERS_UNVERIFIED.get(pair, 0) < 2


def _fallback_filters(pair: str, now: float, why: str = "") -> dict:
    """REV 10.8 — includes maxQty (default 1e9) so entry.py cap is
    a no-op on the fallback path (never falsely caps)."""
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
        logger.warning(f"⚠️ Filters for {pair} HARDCODED fallback ({why}) — entries blocked")
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
        logger.warning(f"⚠️ Unknown {pair} - default filters ({why}) — entries blocked")
    with FILTERS_CACHE_LOCK:
        FILTERS_CACHE[pair] = result
    return result


def get_filters(pair: str) -> dict:
    """REV 10.8 — now returns maxQty from LOT_SIZE filter.

    Returns dict with keys: stepSize, minQty, maxQty, tickSize,
    minNotional, timestamp, verified.
    """
    now = time.time()
    with FILTERS_CACHE_LOCK:
        cached = FILTERS_CACHE.get(pair)
        if cached and now - cached['timestamp'] < 3600 and cached.get('verified', False):
            return cached
    try:
        info = _get_exchange_info()
        symbols = info.get('symbols', []) if isinstance(info, dict) else []
        sym = next((s for s in symbols if s.get('symbol') == pair), None)
        if sym is None:
            raise ValueError(f"symbol {pair} not in exchangeInfo")

        lot_f = [f for f in sym['filters'] if f['filterType'] == 'LOT_SIZE']
        m_lot = [f for f in sym['filters'] if f['filterType'] == 'MARKET_LOT_SIZE']
        lot = m_lot[0] if m_lot else (lot_f[0] if lot_f else None)
        price_f = next((f for f in sym['filters'] if f['filterType'] == 'PRICE_FILTER'), None)
        notional = next((f for f in sym['filters'] if f['filterType'] == 'MIN_NOTIONAL'), None)

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
# KLINES — REV 10.6: taker columns converted to float
#           REV 11.0: added _KLINES_CACHE (rate-limit safety)
# ─────────────────────────────────────────────────────────────
#
# Cache TTL per interval. Chosen so a 15 s scan cycle mostly reuses
# cached data without staleness exceeding half the interval.
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

# Key: (symbol, interval, limit). Value: (DataFrame, ts).
# NOTE: the cached DataFrame is returned AS-IS. Callers must not
# mutate it in place — all current callers are read-only views
# (iloc slicing, column extraction).
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
            # Evict entries older than 1 h to keep the cache bounded.
            now = time.time()
            for k in list(_KLINES_CACHE.keys()):
                _, ts = _KLINES_CACHE[k]
                if now - ts > 3600:
                    del _KLINES_CACHE[k]


def invalidate_klines_cache(symbol: Optional[str] = None) -> None:
    """
    Bust the klines cache.

    REV 11.0 — call with no args to clear all entries, or with a
    symbol (e.g. 'BTCUSDT') to clear only that symbol's entries.
    """
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
    Fetch klines, with per-interval TTL cache.

    REV 11.0 — cache-first. Without it, the scan loop (52 coins ×
    3 TFs × every 15 s) issued ~156 API calls per cycle for klines
    alone. Cache absorbs scan-loop re-reads; TTL adapts to interval
    so data stays fresh at short TFs and never goes stale by more
    than half the interval.
    """
    # 1) Cache hit fast-path
    cached = _klines_cache_get(symbol, interval, limit)
    if cached is not None:
        return cached

    # 2) API fetch
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

        # 3) Populate cache and return
        _klines_cache_set(symbol, interval, limit, df)
        return df
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
# BOOK TICKER / SPREAD — REV 10.7
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
            _BOOK_TICKER_CACHE[symbol] = {'bid': bid, 'ask': ask, 'time': now}
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
    REV 11.0 — refresh_timestamp() hoisted OUT of the retry loop
    (was inside, causing a redundant server-time fetch on every retry
    when already rate-limited). Now consistent with _position_amt().
    """
    pair = symbol_short + 'USDT'
    refresh_timestamp()
    for i in range(3):
        try:
            arr = _global_client.futures_position_information(symbol=pair)
            if arr and len(arr) > 0:
                return arr[0]
            return {'symbol': pair, 'positionAmt': '0', 'entryPrice': '0',
                    'markPrice': '0', 'unRealizedProfit': '0'}
        except Exception as e:
            if i == 2:
                logger.debug(f"fetch_position_raw {symbol_short} failed: {type(e).__name__}")
                return None
            time.sleep(0.6 + i * 0.4)
    return None


def _position_amt(pair: str, retries: int = 3):
    refresh_timestamp()
    for attempt in range(retries):
        try:
            with _requests_lock:
                arr = _global_client.futures_position_information(symbol=pair)
            if arr:
                return float(arr[0].get('positionAmt', 0) or 0)
            return 0.0
        except Exception:
            time.sleep(1)
    return None


# ─────────────────────────────────────────────────────────────
# ALGO ORDERS
# ─────────────────────────────────────────────────────────────
def _get_open_algo_orders(pair: str) -> list:
    getter = getattr(_global_client, 'futures_get_open_algo_orders', None)
    if getter is None:
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
def set_client_keys(api_key: str, api_secret: str) -> Client:
    """
    REV 11.0 — guarded by _CLIENT_INIT_LOCK so concurrent calls can't
    race on the global assignment / see a partially-initialised client.
    """
    global _global_client
    api_key = _clean_key(api_key)
    api_secret = _clean_key(api_secret)
    if len(api_key) < 20 or len(api_secret) < 20:
        logger.critical(f"❌ API key/secret too short (key={len(api_key)}, secret={len(api_secret)})")
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
    # refresh_timestamp outside the lock (it acquires _requests_lock,
    # which is reentrant-safe but we prefer a short critical section)
    refresh_timestamp()
    send_telegram(f"✅ Bot started (Demo: {CONFIG.demo_mode}, Testnet: {CONFIG.testnet})")
    return _global_client


def _validate_api_credentials(client: Client) -> tuple[bool, float]:
    try:
        acc = client.futures_account()
        bal = float(acc.get('totalWalletBalance', 0))
        logger.info(f"✅ API credentials OK — Wallet: ${bal:.2f}")
        return True, bal
    except BinanceAPIException as e:
        if e.code == -2014:
            logger.critical("❌ API-key format invalid (-2014). Check .env has no whitespace/quotes.")
        elif e.code == -2015:
            logger.critical(f"❌ API key invalid/disabled/IP-restricted: {e}")
        else:
            logger.critical(f"❌ API validation failed: {e}")
        return False, 0.0
    except Exception as e:
        logger.critical(f"❌ API validation unexpected error: {e}")
        return False, 0.0


# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        return
    try:
        import html as _html
        url = f"https://api.telegram.org/bot{CONFIG.telegram_bot_token}/sendMessage"
        safe_msg = _html.escape(msg)
        data = {"chat_id": CONFIG.telegram_chat_id, "text": safe_msg, "parse_mode": "HTML"}
        requests.post(url, data=data, timeout=5)
    except requests.exceptions.Timeout:
        logger.warning("Telegram timeout")
    except Exception as e:
        logger.warning(f"Telegram error: {e}")