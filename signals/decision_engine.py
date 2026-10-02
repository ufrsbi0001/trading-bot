"""
decision_engine.py — Multi-filter trade approval layer.

Sits between family_router signal generation and orders.place_order_fixed.
Evaluates each candidate trade across 6 independent dimensions:
  1. Trend strength (ADX + counter-trend awareness)
  2. Multi-timeframe consensus
  3. Volume / order-flow confirmation
  4. Recent performance memory (loss streak guard)
  5. Position correlation (max concurrent same-side)
  6. Volatility regime filter

Approval requires MIN_APPROVALS filter passes.

REV 4.1 (2026-10-02) — PHASE 1 CLEANUP:
  ✅ COUNTER_TREND_STRATEGIES ab core/config_center.py se import hoti
     hai (pehle is file mein hardcoded duplicate thi). Naya strategy
     add karne ke liye sirf config_center edit karein — yahan kuch
     nahi chhedna padega.

REV 4.0 (2026-10-02) — CENTRALIZED CONFIG (config_center.DECISION):
  ✅ Saari hardcoded values ab core/config_center.py ke DECISION dict
     se aati hain. Yeh file sirf LOGIC rakhti hai, values nahi.
  ✅ Tuning ke liye:
       • core/config_center.py → DECISION dict edit karein, YA
       • .env mein CC_DEC_<key>=value set karein
     e.g., CC_DEC_MIN_APPROVALS=4, CC_DEC_BTC_BIAS_MODE=high_risk_only
  ✅ _btc_bias_blocks() ab 3 modes support karta hai:
       • "disabled"       → gate band
       • "high_risk_only" → sirf high-risk meme coins block
       • "full"           → har TREND_UP mein SHORT block (purana behavior)
  ✅ _btc_bias_blocks() ab `symbol` parameter leta hai (high-risk filter ke liye).
  ✅ Callers (is file + base.py) `symbol` pass karte hain.

REV 1.3.0 (2026-09-30) — DOCSTRING / COMMENT TRUTH-UP.
REV 1.2.1 (2026-09-29) — SAME-SIDE CAP FROM CONFIG.
REV 1.2.0 (2026-09-29) — SIMPLIFICATION PASS.
REV 1.1.0 (2026-09-29) — FUNDING FILTER + WR CAP FIX. [superseded]
REV 1.0.6 (2026-09-29) — SOLE MTF FILTER + DOC CLARITY.
REV 1.0.5 (2026-09-29) — GLOBAL BTC BIAS HARD GATE.
REV 1.0.4 (2026-09-29) — CORRELATION HARD GATE.
REV 1.0.3 (2026-09-29) — MTF FLAG HONORED.
REV 1.0.2 (2026-09-28) — WIN-RATE MULTIPLIER + CVD STRICT MODE.
REV 1.0.1 (2026-09-28) — BE STREAK FIX + RING SIZE.
REV 1.0.0 (2026-09-28) — initial release.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from core.client import logger
from core.config import CONFIG       # needed for MTF flag + same-side cap

# ── REV 4.0 — Centralized decision config ──
# ── REV 4.1 — COUNTER_TREND_STRATEGIES bhi yahan se import ──
try:
    from core.config_center import get_decision_cfg as _get_decision_cfg
    from core.config_center import COUNTER_TREND_STRATEGIES
    _DEC = _get_decision_cfg()
except Exception as _e:
    # Graceful fallback — agar config_center load na ho to defaults
    print(f"[decision_engine] config_center unavailable ({_e}) — using defaults")
    _DEC = {
        "min_approvals": 5, "total_filters": 6,
        "btc_bias_enabled": True, "btc_bias_mode": "full",
        "btc_bias_high_risk_coins": frozenset(),
        "btc_regime_ttl_sec": 600,
        "adx_counter_trend_hard_max": 45.0,
        "adx_counter_trend_soft_max": 35.0,
        "atr_ratio_extreme": 2.5,
        "loss_streak_trigger": 4, "loss_streak_cooldown_min": 60,
        "wr_mult_min": 0.85, "wr_mult_max": 1.10, "wr_mult_min_trades": 15,
    }
    COUNTER_TREND_STRATEGIES = frozenset({
        "TREND_DOWN_FADE",
        "MEAN_REVERSION",
        "RANGE_SCALPER",
        "CHOP_FADE",
    })


# ═════════════════════════════════════════════════════════════
#  TUNABLES — REV 4.0: ab config_center se aate hain
#  Tuning ke liye: core/config_center.py ka DECISION dict
#  ya .env mein CC_DEC_* env vars set karein.
# ═════════════════════════════════════════════════════════════
MIN_APPROVALS              = _DEC["min_approvals"]
TOTAL_FILTERS              = _DEC["total_filters"]

ADX_COUNTER_TREND_HARD_MAX = _DEC["adx_counter_trend_hard_max"]
ADX_COUNTER_TREND_SOFT_MAX = _DEC["adx_counter_trend_soft_max"]
ATR_RATIO_EXTREME          = _DEC["atr_ratio_extreme"]

# Same-side cap — still from CONFIG (.env MAX_SAME_SIDE_POSITIONS)
MAX_SAME_SIDE_POSITIONS = CONFIG.max_same_side_positions

LOSS_STREAK_TRIGGER        = _DEC["loss_streak_trigger"]
LOSS_STREAK_COOLDOWN_MIN   = _DEC["loss_streak_cooldown_min"]

# ── BTC global bias gate ──
BLOCK_COUNTERTREND_TO_BTC  = _DEC["btc_bias_enabled"]
BTC_BIAS_MODE              = _DEC["btc_bias_mode"]            # "full" | "high_risk_only" | "disabled"
BTC_BIAS_HIGH_RISK_COINS   = _DEC["btc_bias_high_risk_coins"] # frozenset
BTC_REGIME_TTL_SEC         = _DEC["btc_regime_ttl_sec"]

# ── Win-rate multiplier bounds ──
WR_MULT_MIN                = _DEC["wr_mult_min"]
WR_MULT_MAX                = _DEC["wr_mult_max"]
WR_MULT_MIN_TRADES         = _DEC["wr_mult_min_trades"]

# NOTE (REV 4.1): COUNTER_TREND_STRATEGIES is now imported from
# core.config_center above. Single source of truth — edit there only.


@dataclass
class DecisionScore:
    approved: bool = False
    approvals: int = 0
    total_filters: int = TOTAL_FILTERS
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
    """Returns cached BTC regime, or UNKNOWN if stale."""
    with _btc_lock:
        if _btc_regime_ts <= 0:
            return "UNKNOWN"
        if time.time() - _btc_regime_ts > BTC_REGIME_TTL_SEC:
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
    with _recent_lock:
        recent_same = [r for r in _recent_results if r["strategy"] == strategy]
    if len(recent_same) < LOSS_STREAK_TRIGGER:
        return
    last_n = recent_same[-LOSS_STREAK_TRIGGER:]
    if all(not r["won"] for r in last_n):
        pause_until = time.time() + LOSS_STREAK_COOLDOWN_MIN * 60
        with _pauses_lock:
            _strategy_pauses[strategy] = pause_until
        logger.warning(
            f"[decision] {strategy} paused for {LOSS_STREAK_COOLDOWN_MIN}m "
            f"after {LOSS_STREAK_TRIGGER} consecutive losses"
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
    recent win rate.

    Contract:
      • Returns 1.0 (neutral) when:
          - strategy is empty/None
          - fewer than WR_MULT_MIN_TRADES recorded for that strategy
      • Otherwise: multiplier = WR_MULT_MIN + (WR_MULT_MAX - WR_MULT_MIN) * win_rate
          - WR = 0.0  → 0.85x
          - WR = 0.5  → 0.975x
          - WR = 1.0  → 1.10x

    Soft confidence nudge, not a gate.
    """
    if not strategy:
        return 1.0
    try:
        with _recent_lock:
            rows = [r for r in _recent_results if r["strategy"] == strategy]
        if len(rows) < WR_MULT_MIN_TRADES:
            return 1.0
        wins = sum(1 for r in rows if r["won"])
        wr = wins / len(rows)
        return WR_MULT_MIN + (WR_MULT_MAX - WR_MULT_MIN) * wr
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
    if adx > ADX_COUNTER_TREND_HARD_MAX:
        return False, f"counter-trend adx={adx:.0f}>{ADX_COUNTER_TREND_HARD_MAX:.0f}"
    if adx > ADX_COUNTER_TREND_SOFT_MAX:
        return True, f"counter-trend adx={adx:.0f} (soft-warn)"
    return True, f"counter-trend adx={adx:.0f} (weak, ideal)"


def _filter_mtf_consensus(side: str, ind_1h: dict, ind_4h: dict,
                          ind_1d: Optional[dict]):
    """
    SOLE MTF FILTER in the system.

    future.py REV 1.4.13 removed the duplicate binary MTF hard-gate
    that used to run before the decision engine.

    TF set: 1h, 4h, 1d — EXACTLY the TFs the scan loop caches.

    Behaviour: 2 of 3 TFs must agree with the trade direction.
      • BUY  agrees if e20 > e50 AND price > e50
      • SELL agrees if e20 < e50 AND price < e50
    When CONFIG.use_mtf_confluence is False, short-circuits to PASS
    with reason "mtf_disabled" and still counts toward the 6-filter
    total (a "free vote").
    """
    if not CONFIG.use_mtf_confluence:
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

    NOTE: this filter still runs inside the 6-filter vote. The HARD
    GATE in evaluate_trade() already short-circuits when same-side
    >= MAX_SAME_SIDE_POSITIONS, so this filter will always pass when
    reached. Kept for defence-in-depth and for the detail[] trail.
    """
    if not active_trades:
        return True, "no_positions"
    same_side = sum(
        1 for t in active_trades
        if (side == "BUY" and t.get("side") == "LONG")
        or (side == "SELL" and t.get("side") == "SHORT")
    )
    if same_side >= MAX_SAME_SIDE_POSITIONS:
        return False, f"{same_side}_same_side>={MAX_SAME_SIDE_POSITIONS}"
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
    if atr_ratio > ATR_RATIO_EXTREME:
        return False, f"atr_{atr_ratio:.2f}x>={ATR_RATIO_EXTREME}"
    return True, f"atr_{atr_ratio:.2f}x"


# ═════════════════════════════════════════════════════════════
#  BTC BIAS GATE (helper)
#  REV 4.0 — 3 modes support (full | high_risk_only | disabled)
# ═════════════════════════════════════════════════════════════
def _btc_bias_blocks(side: str, symbol: str = "") -> Optional[str]:
    """
    REV 4.0 — BTC bias gate with 3 modes.

    Modes (config_center.DECISION.btc_bias_mode):
      • "disabled"       → gate OFF, no block
      • "high_risk_only" → sirf high-risk meme coins block
      • "full"           → har TREND_UP mein SHORT block,
                            TREND_DOWN mein LONG block

    Returns rejection reason string, or None if allowed.
    """
    if not BLOCK_COUNTERTREND_TO_BTC:
        return None

    if BTC_BIAS_MODE == "disabled":
        return None

    # Normalize symbol for high-risk check
    _sym = (symbol or "").upper()
    if _sym and not _sym.endswith("USDT"):
        _sym += "USDT"

    # High-risk-only mode: allow all other coins freely
    if BTC_BIAS_MODE == "high_risk_only":
        if not _sym or _sym not in BTC_BIAS_HIGH_RISK_COINS:
            return None

    # Full mode or high-risk coin → check regime
    regime = get_btc_regime()
    if regime == "TREND_UP" and side == "SELL":
        return "BTC_TREND_UP_blocks_SHORT"
    if regime == "TREND_DOWN" and side == "BUY":
        return "BTC_TREND_DOWN_blocks_LONG"
    return None


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

      GATE 2: Same-side correlation limit (CONFIG.max_same_side_positions).

    If both gates pass, the 6-filter vote runs.
    Approve iff approvals >= MIN_APPROVALS.
    """
    result = DecisionScore()
    active = active_trades_list or []

    # ═══════════════════════════════════════════════════════════
    #  HARD GATE #1 — BTC global bias
    # ═══════════════════════════════════════════════════════════
    btc_reason = _btc_bias_blocks(side, symbol)
    if btc_reason:
        result.approved = False
        result.approvals = 0
        result.total_filters = TOTAL_FILTERS
        result.top_reason = f"HARD_REJECT: {btc_reason}"
        result.reasons = [f"btc_bias:{btc_reason}"]
        result.detail["btc_bias"] = {"pass": False, "reason": btc_reason}
        logger.warning(
            f"[decision] 🚫 HARD REJECT {symbol} {side} [{strategy}] — "
            f"BTC regime={get_btc_regime()} mode={BTC_BIAS_MODE} → {btc_reason} | "
            f"conf={conf:.0f}% rr={rr:.2f}"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    #  HARD GATE #2 — same-side correlation limit
    # ═══════════════════════════════════════════════════════════
    same_side_count = _count_same_side(side, active)
    if same_side_count >= MAX_SAME_SIDE_POSITIONS:
        result.approved = False
        result.approvals = 0
        result.total_filters = TOTAL_FILTERS
        result.top_reason = (
            f"HARD_REJECT: {same_side_count} same-side already open "
            f"(max {MAX_SAME_SIDE_POSITIONS})"
        )
        result.reasons = [
            f"correlation:{same_side_count}_same_side>={MAX_SAME_SIDE_POSITIONS}"
        ]
        result.detail["correlation"] = {
            "pass": False,
            "reason": f"{same_side_count}_same_side>={MAX_SAME_SIDE_POSITIONS}",
        }
        logger.warning(
            f"[decision] 🚫 HARD REJECT {symbol} {side} [{strategy}] — "
            f"{same_side_count} same-side already open "
            f"(max {MAX_SAME_SIDE_POSITIONS}) | conf={conf:.0f}% rr={rr:.2f}"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    #  NORMAL 6-FILTER VOTE
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

    result.approved = result.approvals >= MIN_APPROVALS
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
        "btc_bias_mode": BTC_BIAS_MODE,
        "min_approvals": MIN_APPROVALS,
        "total_filters": TOTAL_FILTERS,
    }