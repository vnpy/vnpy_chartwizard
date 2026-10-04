from collections.abc import Callable
from datetime import datetime

import pytest

from vnpy.event import Event
from vnpy.trader.constant import Exchange, Interval, Product
from vnpy.trader.object import BarData, ContractData, HistoryRequest

from vnpy_chartwizard import ChartWizardApp
from vnpy_chartwizard.engine import (
    APP_NAME,
    EVENT_CHART_HISTORY,
    ChartWizardEngine,
)


START: datetime = datetime(2024, 1, 2, 9, 0)
END: datetime = datetime(2024, 1, 5, 15, 0)


class InlineThread:
    def __init__(
        self,
        target: Callable[..., None],
        args: tuple[object, ...] | list[object] = (),
    ) -> None:
        self._target: Callable[..., None] = target
        self._args: tuple[object, ...] | list[object] = args

    def start(self) -> None:
        self._target(*self._args)


class RecordingEventEngine:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def put(self, event: Event) -> None:
        self.events.append(event)


class FakeMainEngine:
    def __init__(self, contract: ContractData | None = None) -> None:
        self.contract: ContractData | None = contract
        self.history_calls: list[tuple[HistoryRequest, str]] = []
        self.history_bars: list[BarData] = []

    def get_contract(self, vt_symbol: str) -> ContractData | None:
        if self.contract and self.contract.vt_symbol == vt_symbol:
            return self.contract
        return None

    def query_history(
        self,
        req: HistoryRequest,
        gateway_name: str,
    ) -> list[BarData]:
        self.history_calls.append((req, gateway_name))
        return self.history_bars


class RecordingDatabase:
    def __init__(self, bars: list[BarData]) -> None:
        self.bars: list[BarData] = bars
        self.calls: list[tuple[str, Exchange, Interval, datetime, datetime]] = []

    def load_bar_data(
        self,
        symbol: str,
        exchange: Exchange,
        interval: Interval,
        start: datetime,
        end: datetime,
    ) -> list[BarData]:
        self.calls.append((symbol, exchange, interval, start, end))
        return self.bars


class RecordingDatafeed:
    def __init__(self, bars: list[BarData]) -> None:
        self.bars: list[BarData] = bars
        self.requests: list[HistoryRequest] = []

    def query_bar_history(
        self,
        req: HistoryRequest,
        output: object = None,
    ) -> list[BarData]:
        self.requests.append(req)
        return self.bars


def make_bar(symbol: str, close_price: float) -> BarData:
    return BarData(
        symbol=symbol,
        exchange=Exchange.SHFE,
        datetime=START,
        interval=Interval.MINUTE,
        volume=1,
        open_price=close_price,
        high_price=close_price,
        low_price=close_price,
        close_price=close_price,
        gateway_name="DB",
    )


def make_contract(history_data: bool) -> ContractData:
    return ContractData(
        symbol="rb2501",
        exchange=Exchange.SHFE,
        name="rb",
        product=Product.FUTURES,
        size=10,
        pricetick=1,
        history_data=history_data,
        gateway_name="FAKE",
    )


def make_engine(
    monkeypatch: pytest.MonkeyPatch,
    main_engine: FakeMainEngine,
    database: RecordingDatabase,
    datafeed: RecordingDatafeed,
) -> tuple[ChartWizardEngine, RecordingEventEngine]:
    def get_database() -> RecordingDatabase:
        return database

    def get_datafeed() -> RecordingDatafeed:
        return datafeed

    monkeypatch.setattr("vnpy_chartwizard.engine.get_database", get_database)
    monkeypatch.setattr("vnpy_chartwizard.engine.get_datafeed", get_datafeed)
    monkeypatch.setattr("vnpy_chartwizard.engine.Thread", InlineThread)
    event_engine: RecordingEventEngine = RecordingEventEngine()
    engine: ChartWizardEngine = ChartWizardEngine(main_engine, event_engine)  # type: ignore[arg-type]
    return engine, event_engine


class TestChartWizardEngine:
    def test_app_points_at_engine_without_opening_widget(self) -> None:
        app: ChartWizardApp = ChartWizardApp()
        assert app.app_name == APP_NAME == "ChartWizard"
        assert app.engine_class is ChartWizardEngine
        assert app.widget_name == "ChartWizardWidget"
        assert app.display_name == "K线图表"

    def test_query_history_without_contract_loads_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bars: list[BarData] = [make_bar("rb2501", 100)]
        database: RecordingDatabase = RecordingDatabase(bars)
        datafeed: RecordingDatafeed = RecordingDatafeed([])
        main_engine: FakeMainEngine = FakeMainEngine()
        engine, event_engine = make_engine(monkeypatch, main_engine, database, datafeed)

        engine.query_history("rb2501.SHFE", Interval.MINUTE, START, END)

        assert database.calls == [("rb2501", Exchange.SHFE, Interval.MINUTE, START, END)]
        assert datafeed.requests == []
        assert main_engine.history_calls == []
        assert len(event_engine.events) == 1
        event: Event = event_engine.events[0]
        assert event.type == EVENT_CHART_HISTORY == "eChartHistory"
        assert event.data is bars

    def test_query_history_splits_symbol_on_last_dot(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bars: list[BarData] = [make_bar("rb.main", 101)]
        database: RecordingDatabase = RecordingDatabase(bars)
        engine, event_engine = make_engine(
            monkeypatch,
            FakeMainEngine(),
            database,
            RecordingDatafeed([]),
        )

        engine.query_history("rb.main.SHFE", Interval.HOUR, START, END)

        assert database.calls == [("rb.main", Exchange.SHFE, Interval.HOUR, START, END)]
        assert event_engine.events[0].data is bars
        assert event_engine.events[0].data[0].vt_symbol == "rb.main.SHFE"

    def test_query_history_uses_gateway_when_contract_has_history(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bars: list[BarData] = [make_bar("rb2501", 102)]
        contract: ContractData = make_contract(history_data=True)
        main_engine: FakeMainEngine = FakeMainEngine(contract)
        main_engine.history_bars = bars
        database: RecordingDatabase = RecordingDatabase([])
        datafeed: RecordingDatafeed = RecordingDatafeed([])
        engine, event_engine = make_engine(monkeypatch, main_engine, database, datafeed)

        engine.query_history(contract.vt_symbol, Interval.MINUTE, START, END)

        assert len(main_engine.history_calls) == 1
        req: HistoryRequest
        gateway_name: str
        req, gateway_name = main_engine.history_calls[0]
        assert gateway_name == "FAKE"
        assert req.symbol == "rb2501"
        assert req.exchange == Exchange.SHFE
        assert req.interval == Interval.MINUTE
        assert req.start == START
        assert req.end == END
        assert database.calls == []
        assert datafeed.requests == []
        assert event_engine.events[0].data is bars

    def test_query_history_uses_datafeed_when_contract_has_no_history(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bars: list[BarData] = [make_bar("rb2501", 103)]
        contract: ContractData = make_contract(history_data=False)
        main_engine: FakeMainEngine = FakeMainEngine(contract)
        database: RecordingDatabase = RecordingDatabase([])
        datafeed: RecordingDatafeed = RecordingDatafeed(bars)
        engine, event_engine = make_engine(monkeypatch, main_engine, database, datafeed)

        engine.query_history(contract.vt_symbol, Interval.DAILY, START, END)

        assert len(datafeed.requests) == 1
        req: HistoryRequest = datafeed.requests[0]
        assert req.symbol == "rb2501"
        assert req.exchange == Exchange.SHFE
        assert req.interval == Interval.DAILY
        assert req.start == START
        assert req.end == END
        assert main_engine.history_calls == []
        assert database.calls == []
        assert event_engine.events[0].data is bars

    def test_query_history_invalid_symbol_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 缺少交易所后缀时 extract_vt_symbol 抛出 ValueError，引擎本身不捕获。
        database: RecordingDatabase = RecordingDatabase([])
        engine, event_engine = make_engine(
            monkeypatch,
            FakeMainEngine(),
            database,
            RecordingDatafeed([]),
        )

        with pytest.raises(ValueError):
            engine.query_history("rb2501", Interval.MINUTE, START, END)

        assert database.calls == []
        assert event_engine.events == []
