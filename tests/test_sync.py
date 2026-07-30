"""Сценарии выгрузки целиком: транспорт + курсор + снимок."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from wbsync import sync
from wbsync.storage import Storage

from .conftest import FakeResponse


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "snapshot.db") as store:
        yield store


def _card(nm_id: int) -> dict:
    return {"nmID": nm_id, "vendorCode": f"art-{nm_id}", "title": f"Ручка {nm_id}"}


def test_карточки_читаются_постранично_до_неполной_страницы(make_transport, storage):
    transport, session = make_transport(
        [
            FakeResponse(
                payload={
                    "cards": [_card(1), _card(2)],
                    "cursor": {"updatedAt": "2026-07-01T10:00:00Z", "nmID": 2},
                }
            ),
            FakeResponse(payload={"cards": [_card(3)], "cursor": {}}),
        ]
    )
    result = sync.sync_cards(transport, storage, store_id=1, page_size=2)

    assert result.ok and result.rows == 3 and result.pages == 2
    assert storage.count(1, "cards") == 3
    # Курсор второй страницы должен уехать в тело запроса, иначе каталог
    # будет читаться с начала бесконечно.
    assert session.calls[1]["body"]["settings"]["cursor"]["nmID"] == 2


def test_остатки_заменяются_целиком_а_не_накапливаются(make_transport, storage):
    transport, _ = make_transport(
        [FakeResponse(payload=[{"nmId": 1, "barcode": "a", "warehouseName": "Коледино",
                                "quantity": 5, "lastChangeDate": "2026-07-02T10:00:00"}]),
         FakeResponse(payload=[])]
    )
    sync.sync_stocks(transport, storage, 1, date_from=datetime(2026, 7, 1, tzinfo=timezone.utc))

    transport2, _ = make_transport(
        [FakeResponse(payload=[{"nmId": 9, "barcode": "z", "warehouseName": "Казань",
                                "quantity": 1, "lastChangeDate": "2026-07-03T10:00:00"}]),
         FakeResponse(payload=[])]
    )
    sync.sync_stocks(transport2, storage, 1, date_from=datetime(2026, 7, 2, tzinfo=timezone.utc))

    rows = storage.rows(1, "stocks")
    assert len(rows) == 1 and rows[0]["nmId"] == 9


def test_застрявший_курсор_останавливает_выгрузку_но_данные_сохраняются(
    make_transport, storage
):
    """Половина данных лучше, чем зависший навсегда процесс — но об этом надо сказать."""
    same_moment = "2026-07-02T10:00:00"
    transport, _ = make_transport(
        [
            FakeResponse(
                payload=[
                    {"srid": f"s{i}", "date": "2026-07-02", "lastChangeDate": same_moment}
                    for i in range(3)
                ]
            )
        ]
    )
    result = sync.sync_orders(
        transport, storage, 1, date_from=datetime(2026, 7, 2, 10, 0, 0)
    )

    assert not result.ok
    assert "застрял" in result.stopped_early
    assert storage.count(1, "orders") == 3


def test_ручной_сдвиг_курсора_доводит_выгрузку_и_предупреждает(make_transport, storage):
    same_moment = "2026-07-02T10:00:00"
    transport, _ = make_transport(
        [
            FakeResponse(
                payload=[{"srid": "s1", "date": "2026-07-02", "lastChangeDate": same_moment}]
            ),
            FakeResponse(payload=[]),
        ]
    )
    result = sync.sync_orders(
        transport,
        storage,
        1,
        date_from=datetime(2026, 7, 2, 10, 0, 0),
        nudge=True,
    )

    assert result.ok
    assert any("не войдут" in w for w in result.warnings)


def test_отчёт_реализации_идёт_по_rrdid_и_не_двоит(make_transport, storage):
    transport, session = make_transport(
        [
            FakeResponse(
                payload=[
                    {"rrd_id": 10, "rr_dt": "2026-07-01", "ppvz_for_pay": 100},
                    {"rrd_id": 30, "rr_dt": "2026-07-01", "ppvz_for_pay": 200},
                    {"rrd_id": 20, "rr_dt": "2026-07-01", "ppvz_for_pay": 150},
                ]
            ),
            FakeResponse(payload=[]),
        ]
    )
    result = sync.sync_realization(transport, storage, 1, today=date(2026, 7, 30))

    assert result.ok and result.rows == 3
    assert storage.count(1, "realization") == 3
    # Курсор второй страницы — максимум, а не rrd_id последней строки (20).
    assert session.calls[1]["params"]["rrdid"] == 30


def test_окно_реализации_перечитывается_целиком(make_transport, storage):
    transport, session = make_transport([FakeResponse(payload=[])])
    sync.sync_realization(transport, storage, 1, today=date(2026, 7, 30))
    params = session.calls[0]["params"]
    assert params["dateFrom"] == "2026-06-18" and params["dateTo"] == "2026-07-30"


def test_отчёт_о_токене_собирает_доступные_категории(make_transport):
    from .conftest import make_token

    raw = make_token()
    # 13 доменов: контент и статистика доступны, остальное — нет.
    responses = []
    for name in ["general", "content", "prices", "marketplace", "statistics",
                 "analytics", "promotion", "feedbacks", "chat", "supplies",
                 "returns", "documents", "finance"]:
        ok = name in {"content", "statistics"}
        responses.append(
            FakeResponse(payload={"Status": "OK"}) if ok
            else FakeResponse(status_code=403, text_body="no access")
        )
    transport, _ = make_transport(responses, token=raw)

    report = sync.check_token(transport, raw)
    assert report.categories["statistics"] is True
    assert report.categories["finance"] is False
    assert report.usable_for_sync
