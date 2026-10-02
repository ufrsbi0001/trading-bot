"""
decision_engine.py — Multi-filter trade approval layer.

REV 4.2 (2026-10-02) — LIVE CONFIG READS:
  ✅ Removed module-level caching of config values. All decision-engine
     config now read LIVE from config_center on each call, so
     `update_runtime()` takes effect immediately (no restart).
  ✅ Removed dead `from core.config import CONFIG` import — the comment
     claimed it was "needed for MTF flag + same-side cap", but both were
     already migrated to `CC.get(...)`.
  ✅ Removed dead `_DEC` module-level dict — replaced with `_dec_get()`
     helper that reads fresh on every call.
  ✅ Fallback path now uses `logger.warning` instead of `print()`.
  ✅ `total_filters` moved to instance-level (was class default).

REV 4.1 (2026-10-02) — PHASE 1 CLEANUP:
  ✅ COUNTER_TREND_STRATEGIES now imported from core/config_center.py.

REV 4.0 (2026-10-02) — CENTRALIZED CONFIG.
REV 1.3.0 — 1.0.0 — various.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from core.client import logger
from core import config_center as CC

# ── REV 4.1 — COUNTER_TREND_STRATEGIES from config_center ──
try:
    from core.config_center import COUNTER_TREND_STRATEGIES
except Exception as _ct_err:
    logger.warning(
        f"[decision_engine] COUNTER_TREND_STRATEGIES import failed "
        f"({_ct_err}); using built-in fallback"
    )
    COUNTER_TREND_STRATEGIES = frozenset({
        "TREND_DOWN_FADE",
        "MEAN_REVERSION",
        "RANGE_SCALPER",
        "CHOP_FADE",
    })


# ═════════════════════════════════════════════════════════════
#  REV 4.2 — LIVE CONFIG HELPERS
#  Read fresh on every call so update_runtime() takes effect
#  immediately. Falls back to safe defaults only in the
#  pathological case where config_center is unavailable.
# ═════════════════════════════════════════════════════════════
_DEFAULT_DECISION = {
    "min_approvals": 5,
    "total_filters": 6,
    "btc_bias_enabled": True,
    "btc_bias_mode": "full",
    "btc_bias_high_risk_coins": frozenset(),
    "btc_regime_ttl_sec": 600,
    "adx_counter_trend_hard_max": 45.0,
    "adx_counter_trend_soft_max": 35.0,
    "atr_ratio_extreme": 2.5,
    "loss_streak_trigger": 4,
    "loss_streak_cooldown_min": 60,
    "wr_mult_min": 0.85,
    "wr_mult_max": 1.10,
    "wr_mult_min_trades": 15,
}


def _dec_get(key: str, default=None):
    """Live decision config read (fresh each call)."""
    try:
        val = CC.get_decision_cfg().get(key)
        if val is not None:
            return val
    except Exception:
        pass
    if default is not None:
        return default
    return _DEFAULT_DECISION.get(key)


def _global_get(key: str, default=None):
    """Live GLOBAL config read."""
    try:
        return CC.get(key, default)
    except Exception:
        return default


# ═════════════════════════════════════════════════════════════
#  GLOBAL BTC BIAS (module-level shared state)
# ═════════════════════════════════════════════════════════════
_btc_regime: str = "UNKNOWN"
_btc_regime_ts: float = 0.0
_btc_lock = threading.Lock()


def set_btc_regime(regime: str) -> None:
    """
    Called by the scanner once per scan cycle AFTER computing the
    BTC 1h indicators dict.

    Expected values (case-insensitive):
      "TREND_UP", "TREND_DOWN", "CHOP", "VOLATILE", "QUIET", "UNKNOWN"

    Until this is called at least once, the gate is a no-op.
    """
    global _btc_regime, _btc_regime_ts
    new = (regime or "UNKNOWN").upper().strip()
    with _btc_lock:
        if new != _btc_regime:
            logger.info(f"[decision] BTC regime → {new}")
        _btc_regime = new
        _btc_regime_ts = time.time()


def get_btc_regime() -> str:
    """Returns cached BTC regime, or UNKNOWN if stale.

    REV 4.2 — TTL read LIVE from config_center each call.
    """
    _ttl = float(_dec_get("btc_regime_ttl_sec", 600) or 600)
    with _btc_lock:
        if _btc_regime_ts <= 0:
            return "UNKNOWN"
        if time.time() - _btc_regime_ts > _ttl:
            return "UNKNOWN"
        return _btc_regime


# ═════════════════════════════════════════════════════════════
#  RECENT PERFORMANCE MEMORY
# ═════════════════════════════════════════════════════════════
_recent_results: list = []
_recent_lock = threading.Lock()
_MAX_RECENT = 200
_strategy_pauses: dict = {}
_pauses_lock = threading.Lock()


def record_trade_result(strategy: str, pnl: float) -> None:
    """
    Record a closed-trade outcome for the loss-streak guard.

    BE (pnl == 0) is classified as `won=True`:
      • A breakeven trade is NOT a loss.
      • It breaks any in-progress loss streak.
      • Only actual pnl < 0 trades extend the streak.
    """
    with _recent_lock:
        _recent_results.append({
            "ts": time.time(),
            "strategy": strategy or "UNKNOWN",
            "won": pnl >= 0,
        })
        if len(_recent_results) > _MAX_RECENT:
            del _recent_results[: len(_recent_results) - _MAX_RECENT]
    if pnl < 0:
        _check_and_pause_strategy(strategy)


def _check_and_pause_strategy(strategy: str) -> None:
    if not strategy:
        return
    _trigger = int(_dec_get("loss_streak_trigger", 4) or 4)
    _cooldown = int(_dec_get("loss_streak_cooldown_min", 60) or 60)
    with _recent_lock:
        recent_same = [r for r in _recent_results if r["strategy"] == strategy]
    if len(recent_same) < _trigger:
        return
    last_n = recent_same[-_trigger:]
    if all(not r["won"] for r in last_n):
        pause_until = time.time() + _cooldown * 60
        with _pauses_lock:
            _strategy_pauses[strategy] = pause_until
        logger.warning(
            f"[decision] {strategy} paused for {_cooldown}m "
            f"after {_trigger} consecutive losses"
        )


def _is_strategy_paused(strategy: str):
    with _pauses_lock:
        until = _strategy_pauses.get(strategy, 0)
    if until > time.time():
        return True, int((until - time.time()) / 60)
    return False, 0


# ═════════════════════════════════════════════════════════════
#  WIN-RATE MULTIPLIER (public)
# ═════════════════════════════════════════════════════════════
def get_strategy_multiplier(strategy: str) -> float:
    """
    Return a 0.85–1.10 confidence multiplier based on the strategy's
    recent win rate. Live-reads bounds from config_center each call.

    Contract:
      • Returns 1.0 (neutral) when:
          - strategy is empty/None
          - fewer than `wr_mult_min_trades` recorded for that strategy
      • Otherwise: multiplier = min + (max - min) * win_rate

    Soft confidence nudge, not a gate.
    """
    if not strategy:
        return 1.0
    _min = float(_dec_get("wr_mult_min", 0.85) or 0.85)
    _max = float(_dec_get("wr_mult_max", 1.10) or 1.10)
    _min_trades = int(_dec_get("wr_mult_min_trades", 15) or 15)
    try:
        with _recent_lock:
            rows = [r for r in _recent_results if r["strategy"] == strategy]
        if len(rows) < _min_trades:
            return 1.0
        wins = sum(1 for r in rows if r["won"])
        wr = wins / len(rows)
        return _min + (_max - _min) * wr
    except Exception:
        return 1.0


# ═════════════════════════════════════════════════════════════
#  INDIVIDUAL FILTERS
# ═════════════════════════════════════════════════════════════
def _filter_trend_strength(side: str, strategy: str, ind_1h: dict):
    is_counter = strategy in COUNTER_TREND_STRATEGIES
    adx = float(ind_1h.get("adx", 0) or 0)
    if not is_counter:
        return True, f"trend-following adx={adx:.0f}"
    _hard = float(_dec_get("adx_counter_trend_hard_max", 45.0) or 45.0)
    _soft = float(_dec_get("adx_counter_trend_soft_max", 35.0) or 35.0)
    if adx > _hard:
        return False, f"counter-trend adx={adx:.0f}>{_hard:.0f}"
    if adx > _soft:
        return True, f"counter-trend adx={adx:.0f} (soft-warn)"
    return True, f"counter-trend adx={adx:.0f} (weak, ideal)"


def _filter_mtf_consensus(side: str, ind_1h: dict, ind_4h: dict,
                          ind_1d: Optional[dict]):
    """
    SOLE MTF FILTER in the system.

    TF set: 1h, 4h, 1d — EXACTLY the TFs the scan loop caches.
    Behaviour: 2 of 3 TFs must agree with the trade direction.
      • BUY  agrees if e20 > e50 AND price > e50
      • SELL agrees if e20 < e50 AND price < e50
    When `use_mtf_confluence` is False, short-circuits to PASS with
    reason "mtf_disabled" and still counts toward the total (free vote).

    REV 4.2 — reads flag LIVE from config_center each call.
    """
    if not _global_get("use_mtf_confluence", False):
        return True, "mtf_disabled"

    def agrees(ind, s):
        if not ind:
            return False
        e20 = float(ind.get("ema20", 0) or 0)
        e50 = float(ind.get("ema50", 0) or 0)
        price = float(ind.get("price", 0) or 0)
        if e20 <= 0 or e50 <= 0 or price <= 0:
            return False
        if s == "BUY":
            return e20 > e50 and price > e50
        return e20 < e50 and price < e50

    agree_count = 0
    agree_list = []
    for tf, ind in (("1h", ind_1h), ("4h", ind_4h), ("1d", ind_1d)):
        if agrees(ind, side):
            agree_count += 1
            agree_list.append(tf)
    if agree_count >= 2:
        return True, f"mtf {agree_count}/3 ({','.join(agree_list)})"
    return False, f"mtf only {agree_count}/3"


def _filter_volume_confirmation(side: str, ind_1h: dict,
                                strict: bool = False):
    """CVD slope is a HARD FAIL when strict=True."""
    try:
        cvd_div = str(ind_1h.get("cvd_div", "NONE"))
        cvd_slope = float(ind_1h.get("cvd_slope", 0) or 0)

        if side == "BUY" and cvd_div == "BEARISH_CVD":
            return False, "cvd_bearish_div"
        if side == "SELL" and cvd_div == "BULLISH_CVD":
            return False, "cvd_bullish_div"

        if side == "BUY" and cvd_slope < 0:
            if strict:
                return False, "cvd_slope_neg (strict)"
            return True, "cvd_slope_neg (weak)"
        if side == "SELL" and cvd_slope > 0:
            if strict:
                return False, "cvd_slope_pos (strict)"
            return True, "cvd_slope_pos (weak)"
        return True, "cvd_ok"
    except Exception as e:
        return True, f"cvd_skip:{type(e).__name__}"


def _filter_recent_performance(strategy: str):
    if not strategy:
        return True, "no_strategy_key"
    paused, mins_left = _is_strategy_paused(strategy)
    if paused:
        return False, f"{strategy}_paused_{mins_left}m"
    return True, f"{strategy}_not_paused"


def _filter_correlation(side: str, active_trades: list):
    """
    Correlation counter for same-side positions.

    NOTE: the HARD GATE in evaluate_trade() already short-circuits when
    same-side >= max_same_side_positions, so this filter will always
    pass when reached. Kept for defence-in-depth and detail[] trail.

    REV 4.2 — cap read LIVE from config_center each call.
    """
    if not active_trades:
        return True, "no_positions"
    _max_ss = int(_global_get("max_same_side_positions", 2) or 2)
    same_side = sum(
        1 for t in active_trades
        if (side == "BUY" and t.get("side") == "LONG")
        or (side == "SELL" and t.get("side") == "SHORT")
    )
    if same_side >= _max_ss:
        return False, f"{same_side}_same_side>={_max_ss}"
    return True, f"{same_side}_same_side"


def _count_same_side(side: str, active_trades: list) -> int:
    """Count of currently-open trades on the same side."""
    if not active_trades:
        return 0
    return sum(
        1 for t in active_trades
        if (side == "BUY" and t.get("side") == "LONG")
        or (side == "SELL" and t.get("side") == "SHORT")
    )


def _filter_volatility(ind_1h: dict):
    try:
        atr_ratio = float(ind_1h.get("atr_ratio", 1.0) or 1.0)
    except (TypeError, ValueError):
        return True, "atr_parse_fail"
    _extreme = float(_dec_get("atr_ratio_extreme", 2.5) or 2.5)
    if atr_ratio > _extreme:
        return False, f"atr_{atr_ratio:.2f}x>={_extreme}"
    return True, f"atr_{atr_ratio:.2f}x"


# ═════════════════════════════════════════════════════════════
#  BTC BIAS GATE (helper)
#  REV 4.0 — 3 modes support (full | high_risk_only | disabled)
#  REV 4.2 — all reads LIVE from config_center each call
# ═════════════════════════════════════════════════════════════
def _btc_bias_blocks(side: str, symbol: str = "") -> Optional[str]:
    """
    BTC bias gate with 3 modes.

    Modes (config_center.DECISION.btc_bias_mode):
      • "disabled"       → gate OFF, no block
      • "high_risk_only" → sirf high-risk meme coins block
      • "full"           → har TREND_UP mein SHORT block,
                            TREND_DOWN mein LONG block

    Returns rejection reason string, or None if allowed.
    """
    if not _dec_get("btc_bias_enabled", True):
        return None

    _mode = _dec_get("btc_bias_mode", "full")
    if _mode == "disabled":
        return None

    _high_risk = _dec_get("btc_bias_high_risk_coins", frozenset())
    if not isinstance(_high_risk, (set, frozenset, list, tuple)):
        _high_risk = frozenset()

    # Normalize symbol for high-risk check
    _sym = (symbol or "").upper()
    if _sym and not _sym.endswith("USDT"):
        _sym += "USDT"

    # High-risk-only mode: allow all other coins freely
    if _mode == "high_risk_only":
        if not _sym or _sym not in _high_risk:
            return None

    # Full mode or high-risk coin → check regime
    regime = get_btc_regime()
    if regime == "TREND_UP" and side == "SELL":
        return "BTC_TREND_UP_blocks_SHORT"
    if regime == "TREND_DOWN" and side == "BUY":
        return "BTC_TREND_DOWN_blocks_LONG"
    return None


# ═════════════════════════════════════════════════════════════
#  DECISION SCORE
# ═════════════════════════════════════════════════════════════
@dataclass
class DecisionScore:
    approved: bool = False
    approvals: int = 0
    total_filters: int = 6
    top_reason: str = ""
    reasons: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def add(self, name: str, passed: bool, reason: str):
        self.detail[name] = {"pass": passed, "reason": reason}
        if passed:
            self.approvals += 1
        else:
            self.reasons.append(f"{name}:{reason}")


# ═════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════
def evaluate_trade(symbol: str, side: str, ind_1h: dict, ind_4h: dict,
                   ind_1d: Optional[dict], strategy: str = "",
                   conf: float = 0.0, rr: float = 0.0,
                   active_trades_list: Optional[list] = None) -> DecisionScore:
    """
    Two HARD GATES before the vote:

      GATE 1: BTC global bias. Depends on btc_bias_mode:
              • "full"           → TREND_UP blocks all SELLs
              • "high_risk_only" → sirf high-risk meme coins block
              • "disabled"       → gate OFF

      GATE 2: Same-side correlation limit.

    If both gates pass, the N-filter vote runs.
    Approve iff approvals >= min_approvals.

    REV 4.2 — All config values read LIVE from config_center, so
    update_runtime() takes effect on the very next call.
    """
    # Live reads (fresh per call)
    _min_approvals = int(_dec_get("min_approvals", 5) or 5)
    _total_filters = int(_dec_get("total_filters", 6) or 6)
    _max_ss = int(_global_get("max_same_side_positions", 2) or 2)
    _btc_mode = _dec_get("btc_bias_mode", "full")

    result = DecisionScore()
    result.total_filters = _total_filters
    active = active_trades_list or []

    # ═══════════════════════════════════════════════════════════
    #  HARD GATE #1 — BTC global bias
    # ═══════════════════════════════════════════════════════════
    btc_reason = _btc_bias_blocks(side, symbol)
    if btc_reason:
        result.approved = False
        result.approvals = 0
        result.top_reason = f"HARD_REJECT: {btc_reason}"
        result.reasons = [f"btc_bias:{btc_reason}"]
        result.detail["btc_bias"] = {"pass": False, "reason": btc_reason}
        logger.warning(
            f"[decision] 🚫 HARD REJECT {symbol} {side} [{strategy}] — "
            f"BTC regime={get_btc_regime()} mode={_btc_mode} → {btc_reason} | "
            f"conf={conf:.0f}% rr={rr:.2f}"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    #  HARD GATE #2 — same-side correlation limit
    # ═══════════════════════════════════════════════════════════
    same_side_count = _count_same_side(side, active)
    if same_side_count >= _max_ss:
        result.approved = False
        result.approvals = 0
        result.top_reason = (
            f"HARD_REJECT: {same_side_count} same-side already open "
            f"(max {_max_ss})"
        )
        result.reasons = [
            f"correlation:{same_side_count}_same_side>={_max_ss}"
        ]
        result.detail["correlation"] = {
            "pass": False,
            "reason": f"{same_side_count}_same_side>={_max_ss}",
        }
        logger.warning(
            f"[decision] 🚫 HARD REJECT {symbol} {side} [{strategy}] — "
            f"{same_side_count} same-side already open "
            f"(max {_max_ss}) | conf={conf:.0f}% rr={rr:.2f}"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    #  NORMAL N-FILTER VOTE
    # ═══════════════════════════════════════════════════════════
    _is_trend_following = strategy not in COUNTER_TREND_STRATEGIES

    result.add("trend_strength",
               *_filter_trend_strength(side, strategy, ind_1h))
    result.add("mtf_consensus",
               *_filter_mtf_consensus(side, ind_1h, ind_4h, ind_1d))
    result.add("volume_confirm",
               *_filter_volume_confirmation(side, ind_1h,
                                            strict=_is_trend_following))
    result.add("recent_perf",
               *_filter_recent_performance(strategy))
    result.add("correlation",
               *_filter_correlation(side, active))
    result.add("volatility",
               *_filter_volatility(ind_1h))

    result.approved = result.approvals >= _min_approvals
    result.top_reason = (
        "APPROVED" if result.approved
        else (result.reasons[0] if result.reasons else "unknown_reject")
    )

    tag = "✅" if result.approved else "🚫"
    logger.info(
        f"[decision] {tag} {symbol} {side} [{strategy}] "
        f"{result.approvals}/{result.total_filters} "
        f"conf={conf:.0f}% rr={rr:.2f} → {result.top_reason}"
    )
    if not result.approved:
        logger.debug(f"[decision] {symbol} reject reasons: {result.reasons}")
    return result


def get_stats() -> dict:
    with _recent_lock:
        recent = list(_recent_results)
    with _pauses_lock:
        pauses = {
            k: int((v - time.time()) / 60)
            for k, v in _strategy_pauses.items()
            if v > time.time()
        }
    by_strat: dict = {}
    for r in recent:
        s = r["strategy"]
        if s not in by_strat:
            by_strat[s] = {"wins": 0, "losses": 0}
        if r["won"]:
            by_strat[s]["wins"] += 1
        else:
            by_strat[s]["losses"] += 1
    return {
        "recent_count": len(recent),
        "by_strategy": by_strat,
        "paused_strategies": pauses,
        "btc_regime": get_btc_regime(),
        "btc_bias_mode": _dec_get("btc_bias_mode", "full"),
        "min_approvals": int(_dec_get("min_approvals", 5) or 5),
        "total_filters": int(_dec_get("total_filters", 6) or 6),
    }