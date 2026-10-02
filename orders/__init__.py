"""
orders — Facade package.

REV 23.0 (2026-10-02) — UNIFIED CONFIG CLEANUP:
  ✅ Removed `_PER_CLASS_CFG` and `_STRATEGY_HOLD_MIN` re-exports
     (moved to config_center.VOL_CLASS_R_THRESHOLDS).

REV 1.5.0 (2026-09-28) — SPLIT FROM orders.py.
"""
from .utils import (
    _get_risk_unit, _pick_real_sl,
    _derive_sl_level, _vol_class, _r_thresholds_per_class,
    get_trade_status,
    LEVERAGE, RISK_PERCENT, MAX_OPEN_POSITIONS, MAX_TOTAL_MARGIN_PCT,
    PARTIAL_CLOSE_USDT, DRY_RUN, MAX_HOLD_MINUTES,
)

from .exit import (
    emergency_close_retry, robust_cancel_all, handle_trade_close,
    get_total_pnl,
)

from .repair import (
    reconcile_active_trade, cancel_specific_sl, cancel_all_sl_stops,
    cancel_orphan_bot_orders, sync_existing_positions, clean_orders,
)

from .entry import place_order_fixed

from .manage import (
    manage_single_trade, trade_manager_loop,
    _detect_tp1_fill_and_move_be,
)


__all__ = [
    # Entry
    "place_order_fixed",
    # Manage
    "manage_single_trade", "trade_manager_loop",
    # Repair
    "reconcile_active_trade", "cancel_specific_sl", "cancel_all_sl_stops",
    "cancel_orphan_bot_orders", "sync_existing_positions", "clean_orders",
    # Exit
    "emergency_close_retry", "robust_cancel_all", "handle_trade_close",
    "get_total_pnl",
    # Utils
    "get_trade_status",
    "_get_risk_unit", "_pick_real_sl",
    "_derive_sl_level", "_vol_class", "_r_thresholds_per_class",
    "LEVERAGE", "RISK_PERCENT", "MAX_OPEN_POSITIONS", "MAX_TOTAL_MARGIN_PCT",
    "PARTIAL_CLOSE_USDT", "DRY_RUN", "MAX_HOLD_MINUTES",
]