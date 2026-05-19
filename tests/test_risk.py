"""RiskManager unit tests (mocked) + optional DB-state smoke print.

Pytest mode is fully offline: repo, executor, and data_client are MagicMocks,
so no SQL Server and no Alpaca call happens. Script mode prints the current
drawdown / daily-loss anchor / open positions from SQL Server if reachable;
gracefully skips if the DB is unavailable.

Run modes (per CLAUDE.md dual-mode pattern):
    python -m pytest tests/test_risk.py            # offline assertions
    python -m tests.test_risk                      # prints live risk state
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from risk.manager import RiskAction, RiskManager


def _make(**overrides):
    """Build a RiskManager with all dependencies mocked. Returns
    (manager, repo, executor, data_client) so tests can poke each in turn."""
    repo = MagicMock()
    executor = MagicMock()
    data_client = MagicMock()
    defaults = dict(
        max_positions=2,
        max_daily_loss_pct=0.03,
        max_drawdown_pct=0.15,
        risk_per_trade=0.01,
        atr_stop_mult=2.0,
        max_pct_equity=0.25,
    )
    defaults.update(overrides)
    rm = RiskManager(
        repo=repo, executor=executor, data_client=data_client, **defaults,
    )
    return rm, repo, executor, data_client


# ---------- sizing ----------

def test_position_size_concentration_cap_binds():
    rm, *_ = _make()
    # risk_qty = (10_000 * 0.01) / (2 * 100) = 0.5
    # cap_qty  = (10_000 * 0.25) / 50_000 = 0.05  <- this binds
    qty = rm.position_size(equity=10_000, atr=100.0, price=50_000.0)
    assert qty == pytest.approx(0.05, rel=1e-9)


def test_position_size_risk_qty_binds():
    rm, *_ = _make()
    # risk_qty = (10_000 * 0.01) / (2 * 1000) = 0.05
    # cap_qty  = (10_000 * 0.25) / 100 = 25 — doesn't bind
    qty = rm.position_size(equity=10_000, atr=1000.0, price=100.0)
    assert qty == pytest.approx(0.05, rel=1e-9)


def test_position_size_returns_zero_on_bad_inputs():
    rm, *_ = _make()
    assert rm.position_size(equity=0, atr=100, price=50_000) == 0.0
    assert rm.position_size(equity=10_000, atr=0, price=50_000) == 0.0
    assert rm.position_size(equity=10_000, atr=100, price=0) == 0.0


# ---------- drawdown ----------

def test_drawdown_triggers_kill_switch():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    d = rm.check_drawdown(8_400.0)  # -16% from peak
    assert d.action == RiskAction.KILL_SWITCH
    assert "drawdown" in d.reason


def test_drawdown_at_threshold_triggers():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    # Exactly -15% should trip (<= -max_drawdown_pct).
    d = rm.check_drawdown(8_500.0)
    assert d.action == RiskAction.KILL_SWITCH


def test_drawdown_within_limit_allows():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    d = rm.check_drawdown(9_000.0)  # -10%
    assert d.action == RiskAction.ALLOW


def test_drawdown_no_history_allows():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = None
    d = rm.check_drawdown(10_000.0)
    assert d.action == RiskAction.ALLOW


# ---------- daily loss ----------

def test_daily_loss_halt():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    repo.get_daily_anchor_equity.return_value = 10_000.0
    d = rm.gate_new_entry(current_equity=9_600.0, open_positions_count=0)  # -4%
    assert d.action == RiskAction.HALT_DAILY_LOSS


def test_daily_loss_within_limit_allows():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    repo.get_daily_anchor_equity.return_value = 10_000.0
    d = rm.gate_new_entry(current_equity=9_800.0, open_positions_count=0)  # -2%
    assert d.action == RiskAction.ALLOW


# ---------- max positions ----------

def test_max_positions_blocks_new_entry():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    repo.get_daily_anchor_equity.return_value = 10_000.0
    d = rm.gate_new_entry(current_equity=10_000.0, open_positions_count=2)
    assert d.action == RiskAction.HALT_MAX_POSITIONS


def test_below_max_positions_allows():
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    repo.get_daily_anchor_equity.return_value = 10_000.0
    d = rm.gate_new_entry(current_equity=10_100.0, open_positions_count=1)
    assert d.action == RiskAction.ALLOW


def test_drawdown_takes_precedence_over_daily_loss():
    """Both gates would fire — kill switch must win (more severe action)."""
    rm, repo, *_ = _make()
    repo.get_peak_equity.return_value = 10_000.0
    repo.get_daily_anchor_equity.return_value = 10_000.0
    d = rm.gate_new_entry(current_equity=8_000.0, open_positions_count=0)  # -20%
    assert d.action == RiskAction.KILL_SWITCH


# ---------- stop monitor ----------

def _pos(symbol="BTC/USD", qty=0.1, entry=50_000.0, stop=48_000.0):
    return {
        "symbol": symbol,
        "qty": qty,
        "avg_entry_price": entry,
        "current_stop": stop,
        "opened_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    }


def _quote(bid=49_000.0, ask=49_050.0):
    q = MagicMock()
    q.bid_price = bid
    q.ask_price = ask
    return q


def test_stop_monitor_triggers_sell_when_bid_below_stop():
    rm, repo, executor, data_client = _make()
    repo.get_open_positions_with_stops.return_value = [_pos(stop=48_000.0)]
    data_client.get_latest_quote.return_value = _quote(bid=47_500.0)

    order = MagicMock()
    order.raw = {"filled_avg_price": "47480"}
    executor.submit_market_order.return_value = order

    triggered = rm.monitor_stops()

    assert len(triggered) == 1
    executor.submit_market_order.assert_called_once_with(
        symbol="BTC/USD", qty=0.1, side="SELL",
    )
    repo.record_closed_trade.assert_called_once()
    repo.delete_position.assert_called_once_with("BTC/USD")
    # Realised exit price should reflect Alpaca's fill, not the pre-trade bid.
    assert triggered[0]["exit_price"] == pytest.approx(47_480.0)


def test_stop_monitor_does_nothing_when_bid_above_stop():
    rm, repo, executor, data_client = _make()
    repo.get_open_positions_with_stops.return_value = [_pos(stop=48_000.0)]
    data_client.get_latest_quote.return_value = _quote(bid=49_000.0)

    triggered = rm.monitor_stops()

    assert triggered == []
    executor.submit_market_order.assert_not_called()
    repo.delete_position.assert_not_called()


def test_stop_monitor_skips_position_with_no_stop():
    rm, repo, executor, data_client = _make()
    repo.get_open_positions_with_stops.return_value = [_pos(stop=None)]

    triggered = rm.monitor_stops()

    assert triggered == []
    data_client.get_latest_quote.assert_not_called()
    executor.submit_market_order.assert_not_called()


def test_stop_monitor_handles_quote_failure():
    rm, repo, executor, data_client = _make()
    repo.get_open_positions_with_stops.return_value = [_pos()]
    data_client.get_latest_quote.side_effect = RuntimeError("alpaca down")

    triggered = rm.monitor_stops()

    assert triggered == []
    executor.submit_market_order.assert_not_called()
    repo.log.assert_called()  # error was logged


# ---------- kill switch ----------

def test_kill_switch_closes_all_and_invokes_callback():
    rm, repo, executor, data_client = _make()
    repo.get_open_positions_with_stops.return_value = [
        _pos(symbol="BTC/USD", qty=0.1),
        _pos(symbol="ETH/USD", qty=1.0, entry=3_000.0, stop=None),
    ]
    called: list[bool] = []
    rm.on_kill_switch = lambda: called.append(True)

    n = rm.trigger_kill_switch("test drawdown")

    assert n == 2
    assert executor.submit_market_order.call_count == 2
    # Both pairs should have a SELL submitted (callable with side='SELL').
    sides = [c.kwargs["side"] for c in executor.submit_market_order.call_args_list]
    assert sides == ["SELL", "SELL"]
    assert called == [True]


def test_kill_switch_is_idempotent():
    rm, repo, executor, _ = _make()
    repo.get_open_positions_with_stops.return_value = [_pos()]
    rm.trigger_kill_switch("first")
    n2 = rm.trigger_kill_switch("second")
    assert n2 == 0
    # SELL only fired during the first call.
    assert executor.submit_market_order.call_count == 1


if __name__ == "__main__":
    # Script mode: print live risk state from SQL Server, if reachable.
    # ASCII only — Windows cp1252 console can't encode arrows/non-Latin chars.
    from dotenv import load_dotenv
    load_dotenv()

    try:
        from db.repository import Repository
        repo = Repository()
        peak = repo.get_peak_equity()
        anchor = repo.get_daily_anchor_equity()
        positions = repo.get_open_positions_with_stops()
        open_count = repo.count_open_positions()

        print("Live risk state (from SQL):")
        print(f"  Peak equity (all time): {peak}")
        print(f"  Daily anchor equity:    {anchor}")
        print(f"  Open positions:         {open_count}")
        for p in positions:
            stop = p["current_stop"]
            stop_s = f"{stop:.2f}" if stop is not None else "None"
            print(
                f"    {p['symbol']}: qty={p['qty']} "
                f"entry={p['avg_entry_price']:.2f} stop={stop_s}"
            )
    except Exception as e:
        print(f"DB unavailable (skipping live state): {type(e).__name__}: {e}")

    print("\nMocked RiskManager smoke (no DB, no Alpaca):")
    rm, repo, executor, data_client = _make()
    repo.get_peak_equity.return_value = 10_000.0
    repo.get_daily_anchor_equity.return_value = 10_000.0
    for equity in (10_100, 9_800, 9_600, 8_400):
        d = rm.gate_new_entry(current_equity=equity, open_positions_count=0)
        print(f"  equity={equity}: {d.action.value} | {d.reason}")
