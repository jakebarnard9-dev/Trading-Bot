"""IBKR adapter parsing against fake ib_async objects (no gateway needed)."""
from datetime import date
from types import SimpleNamespace

import pytest

from earnings_bot.broker.ibkr import IBKRBroker, increment_from_rules
from earnings_bot.config import IBKRCfg


class FakeIB:
    def __init__(self, rows, positions=()):
        self._rows = rows
        self._positions = positions

    def isConnected(self):
        return True

    async def accountSummaryAsync(self, account=""):
        return [SimpleNamespace(tag=t, value=v, currency="USD", account="U1") for t, v in self._rows]

    def positions(self):
        return list(self._positions)


async def test_cash_account_summary_uses_available_funds():
    b = IBKRBroker(IBKRCfg())
    b.ib = FakeIB([("NetLiquidation", "1006.5"), ("TotalCashValue", "1006.5"), ("AvailableFunds", "1006.5"), ("BuyingPower", "1006.5"),
                   ("EquityWithLoanValue", "10"), ("InitMarginReq", "0"), ("MaintMarginReq", "0"), ("DayTradesRemaining", "-1")])
    a = await b.account()
    assert a.equity == 1006.5 and a.cash == 1006.5 and a.buying_power == 1006.5
    assert a.day_trades_remaining == -1 and a.is_margin is False
    b.ib = FakeIB([("NetLiquidation", "1006.5"), ("TotalCashValue", "1006.5"), ("AvailableFunds", "10")])
    a = await b.account()
    assert a.cash == 10.0            # unsettled money is never sized against


async def test_positions_parse_options_only():
    b = IBKRBroker(IBKRCfg())
    opt = SimpleNamespace(secType="OPT", symbol="PLUG", lastTradeDateOrContractMonth="20261113", strike=2.5, right="C", multiplier="100")
    stk = SimpleNamespace(secType="STK", symbol="PLUG", lastTradeDateOrContractMonth="", strike=0.0, right="", multiplier="")
    b.ib = FakeIB([], positions=[SimpleNamespace(contract=opt, position=4, avgCost=41.3), SimpleNamespace(contract=stk, position=100, avgCost=2.0)])
    ps = await b.positions()
    assert len(ps) == 1 and ps[0].key == __import__("earnings_bot.models", fromlist=["OptionKey"]).OptionKey("PLUG", date(2026, 11, 13), 2.5, "C")
    assert ps[0].qty == 4 and abs(ps[0].avg_cost - 0.413) < 1e-9


def test_increment_rules_grid():
    assert increment_from_rules([(0, 0.01), (3, 0.05)], 0.42) == 0.01
    assert increment_from_rules([(0, 0.05), (3, 0.10)], 2.95) == 0.05
