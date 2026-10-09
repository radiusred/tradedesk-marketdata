from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd


@dataclass
class _UpdateCall:
    task_id: int
    advance: int


class FakeProgress:
    def __init__(self) -> None:
        self.add_task_calls: list[tuple[str, int, dict]] = []
        self.update_calls: list[_UpdateCall] = []
        self._next_id = 1

    def add_task(self, description: str, *, total: int, **fields: object) -> int:
        task_id = self._next_id
        self._next_id += 1
        self.add_task_calls.append((description, total, dict(fields)))
        return task_id

    def update(self, task_id: int, *, advance: int = 0, **_kwargs: object) -> None:
        if advance:
            self.update_calls.append(_UpdateCall(task_id=task_id, advance=int(advance)))


def test_export_range_reports_download_and_resample_progress(tmp_path, monkeypatch):
    from tradedesk_marketdata import export as ex
    from tradedesk_marketdata.sources import dukascopy as dk

    start = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    hours = [start + timedelta(hours=i) for i in range(3)]
    monkeypatch.setattr(dk, "_iter_hours", lambda *_args, **_kwargs: iter(hours))
    monkeypatch.setattr(dk, "DOWNLOAD_THREADS_PER_INSTRUMENT", 1)

    monkeypatch.setattr(dk, "_download_bi5", lambda *_args, **_kwargs: b"x")
    monkeypatch.setattr(dk, "_probe_price_format", lambda *_args, **_kwargs: "float")

    def _fake_decode_ticks(hour_start, _compressed, *, price_format, price_divisor):
        return [ex.Tick(ts=hour_start, bid=1.0, ask=1.0, bid_vol=1.0, ask_vol=1.0)]

    monkeypatch.setattr(dk, "_decode_ticks", _fake_decode_ticks)

    def _fake_ticks_to_candles(_ticks, *, resample_rule, price_side):
        idx = pd.DatetimeIndex([start], tz="UTC")
        return pd.DataFrame(
            {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
            index=idx,
        )

    monkeypatch.setattr(ex, "_ticks_to_candles", _fake_ticks_to_candles)

    prog = FakeProgress()
    out_dir = tmp_path / "out"

    out_csv = dk.export_range(
        symbol="EURUSD",
        start_utc=start,
        end_utc_inclusive=start,
        out=out_dir,
        price_divisor=1.0,
        resample_rule="5min",
        cache_dir=None,
        probe=False,
        progress=prog,
    )

    bid_csv, ask_csv = out_csv
    assert bid_csv is not None and bid_csv.exists()
    assert ask_csv is not None and ask_csv.exists()
    assert "_bid" in bid_csv.name
    assert "_ask" in ask_csv.name

    assert len(prog.add_task_calls) == 3
    phases = {fields["phase"] for _desc, _total, fields in prog.add_task_calls}
    assert phases == {"dl", "rs", "write"}

    advances_by_task: dict[int, int] = {}
    for c in prog.update_calls:
        advances_by_task[c.task_id] = advances_by_task.get(c.task_id, 0) + c.advance

    # dl and rs each advance once per hour; write advances once per file (bid + ask = 2)
    assert sorted(advances_by_task.values()) == [2, len(hours), len(hours)]
