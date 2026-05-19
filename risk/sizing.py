"""Volatility-adjusted position sizing.

Locked by CLAUDE.md "Position sizing is volatility-adjusted":
    qty = (equity * risk_per_trade) / (atr_stop_mult * ATR_at_entry)
Capped so a single pair never claims more than `max_pct_equity` of the book.

This is the only piece of session 8's risk module that session 7 needs, so
it lives in its own module — session 8's RiskManager will compose this rather
than reimplement it.
"""
from __future__ import annotations


def compute_position_qty(
    *,
    equity: float,
    atr: float,
    price: float,
    risk_per_trade: float = 0.01,
    atr_stop_mult: float = 2.0,
    max_pct_equity: float = 0.25,
) -> float:
    """Units to buy for a long entry. Returns 0.0 if any input is non-positive
    (caller treats 0 as "skip this trade")."""
    if equity <= 0 or atr <= 0 or price <= 0:
        return 0.0
    if risk_per_trade <= 0 or atr_stop_mult <= 0:
        return 0.0

    stop_distance = atr_stop_mult * atr
    risk_qty = (equity * risk_per_trade) / stop_distance

    # Concentration cap: never let a single pair eat more than max_pct_equity.
    max_qty_by_cap = (equity * max_pct_equity) / price
    return min(risk_qty, max_qty_by_cap)
