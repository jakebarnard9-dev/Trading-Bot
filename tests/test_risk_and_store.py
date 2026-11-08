from datetime import date, datetime

from earnings_bot.clock import ET
from earnings_bot.config import Config, load_config
from earnings_bot.models import AccountInfo, OptionKey, Position, PositionLeg
from earnings_bot.risk.manager import RiskManager
from earnings_bot.state.store import Store


def _cfg(**risk):
    cfg = load_config("tests/fixture_config.yaml")
    for k, v in risk.items():
        setattr(cfg.risk, k, v)
    return cfg


def _pos(symbol="AAA", cost=600.0, remaining=1):
    k = OptionKey(symbol, date(2026, 11, 13), 100.0, "C")
    return Position(f"{symbol}-1", symbol, "earnings", "call", [PositionLeg(k, 1, cost / 100, remaining)], datetime(2026, 11, 12, 15, 40, tzinfo=ET),
                    date(2026, 11, 13), date(2026, 11, 13), cost, meta={"sector": "Technology"})


def test_profiles_and_bounds():
    assert Config(risk={"profile": "conservative"}).risk.per_trade_pct == 0.05
    assert Config(risk={"profile": "max"}).risk.max_deployed_pct == 0.80
    assert Config(risk={"profile": "max", "per_trade_pct": 0.9}).risk.per_trade_pct == 0.9    # caps are the owner's call
    assert Config(risk={"profile": "unlimited", "per_trade_pct": 1.5}).risk.per_trade_pct == 1.0  # ...up to the physical 100%
    assert Config(risk={"profile": "unlimited"}).risk.daily_loss_limit_pct == 1.0


def test_sizing_and_gates(tmp_path):
    cfg = _cfg()
    rm = RiskManager(cfg, Store(tmp_path / "s.sqlite"))
    acct = AccountInfo(5000, 5000, 5000, -1, False)
    units, d = rm.size_units(480.0, acct, [])
    assert units == 1 and d["budget"] == 600.0            # 12% of 5000 = 600 -> one $480 unit
    units, _ = rm.size_units(150.0, acct, [])
    assert units == 4
    open_pos = [_pos("A", 1500), _pos("B", 1400)]            # 2900 deployed of 3000 cap
    units, d = rm.size_units(150.0, acct, open_pos)
    assert units == 0 and d["deployable"] == 100.0
    now = datetime(2026, 11, 12, 15, 20, tzinfo=ET)
    ok, why = rm.can_open(AccountInfo(5000, 5000, 5000), [], now)
    assert ok
    ok, why = rm.can_open(AccountInfo(3700, 3700, 3700), [], now)   # day start recorded at 5000 -> -26%
    assert not ok and "daily loss" in why
    ok, why = rm.can_open(AccountInfo(4800, 4800, 4800), [_pos(str(i)) for i in range(4)], now)
    assert not ok and "max open" in why


def test_equity_floor_halts_and_reset(tmp_path):
    rm = RiskManager(_cfg(), Store(tmp_path / "s.sqlite"))
    now = datetime(2026, 11, 12, 10, 0, tzinfo=ET)
    assert rm.check_floor(2600, now) is None
    assert rm.check_floor(2500, now) is not None
    ok, why = rm.can_open(AccountInfo(4000, 4000, 4000), [], now)
    assert not ok and "halted" in why
    rm.reset_halt()
    assert rm.halt_reason() is None


def test_day_trade_modes(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    rm = RiskManager(_cfg(pdt_mode="auto"), st)
    assert rm.day_trade_allowed(AccountInfo(5000, 5000, 5000, -1), date(2026, 11, 12))[0]
    assert not rm.day_trade_allowed(AccountInfo(5000, 5000, 5000, 0), date(2026, 11, 12))[0]
    for _ in range(3):
        st.record_day_trade(date(2026, 11, 10), "X")
    assert not rm.day_trade_allowed(AccountInfo(5000, 5000, 5000, None), date(2026, 11, 12))[0]
    assert RiskManager(_cfg(pdt_mode="off"), st).day_trade_allowed(AccountInfo(5000, 5000, 5000, 0), date(2026, 11, 12))[0]


def test_losing_streak_pause(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    rm = RiskManager(_cfg(), st)
    for i, (a, b) in enumerate([(5000, 4800), (4800, 4500), (4500, 4400)]):
        d = date(2026, 11, 9 + i)
        st.start_day(d, a)
        msg = rm.after_close(d, b, b - a, 1)
    assert msg and "paused" in msg
    ok, why = rm.can_open(AccountInfo(4400, 4400, 4400), [], datetime(2026, 11, 12, 15, 0, tzinfo=ET))
    assert not ok and "paused" in why


def test_store_position_roundtrip(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    p = _pos()
    p.notes.append("hello")
    st.save_position(p)
    got = st.open_positions()
    assert len(got) == 1 and got[0].legs[0].key == p.legs[0].key and got[0].notes == ["hello"] and got[0].meta["sector"] == "Technology"
    got[0].legs[0].remaining = 0
    got[0].status = "closed"
    got[0].closed_at = datetime(2026, 11, 13, 10, 0, tzinfo=ET)
    st.save_position(got[0])
    assert st.open_positions() == [] and st.all_positions()[0].status == "closed"
