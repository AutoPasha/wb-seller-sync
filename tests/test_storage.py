"""Снимок: атомарность прогона, отсутствие дублей, перечитывание периода."""

from __future__ import annotations

from datetime import date

import pytest

from wbsync.storage import Storage, StorageError


@pytest.fixture
def storage(tmp_path):
    with Storage(tmp_path / "snapshot.db") as store:
        yield store


def test_упавший_прогон_не_публикует_половину_данных(storage):
    """Отчёт, построенный по половине остатков, выглядит правдоподобно — и врёт."""
    with pytest.raises(RuntimeError):
        with storage.run(1, "stocks") as writer:
            writer.write([{"nmId": 1, "barcode": "a", "warehouseName": "Коледино"}])
            raise RuntimeError("сеть отвалилась на середине")

    assert storage.count(1, "stocks") == 0
    assert storage.last_run(1, "stocks")["status"] == "failed"


def test_успешный_прогон_виден_целиком(storage):
    with storage.run(1, "stocks") as writer:
        writer.write(
            [
                {"nmId": 1, "barcode": "a", "warehouseName": "Коледино"},
                {"nmId": 2, "barcode": "b", "warehouseName": "Электросталь"},
            ]
        )
    assert storage.count(1, "stocks") == 2
    assert storage.last_run(1, "stocks")["status"] == "ok"


def test_повторная_выгрузка_обновляет_а_не_двоит(storage):
    row = {"rrd_id": 10, "rr_dt": "2026-07-01", "ppvz_for_pay": 100}
    with storage.run(1, "realization") as writer:
        writer.write([row])
    with storage.run(1, "realization") as writer:
        writer.write([{**row, "ppvz_for_pay": 120}])

    assert storage.count(1, "realization") == 1
    assert storage.rows(1, "realization")[0]["ppvz_for_pay"] == 120


def test_перечитывание_окна_убирает_исчезнувшие_строки(storage):
    """WB может убрать строку из отчёта при пересчёте. Она должна исчезнуть и у нас."""
    with storage.run(1, "realization") as writer:
        writer.write(
            [
                {"rrd_id": 1, "rr_dt": "2026-07-01"},
                {"rrd_id": 2, "rr_dt": "2026-07-02"},
            ]
        )
    with storage.run(1, "realization") as writer:
        writer.drop_period(date(2026, 7, 1), date(2026, 7, 30))
        writer.write([{"rrd_id": 1, "rr_dt": "2026-07-01"}])

    assert storage.count(1, "realization") == 1


def test_магазины_не_смешиваются(storage):
    for store_id in (1, 2):
        with storage.run(store_id, "cards") as writer:
            writer.write([{"nmID": 100 + store_id}])
    assert storage.count(1, "cards") == 1
    assert storage.count(2, "cards") == 1


def test_пропавшее_ключевое_поле_останавливает_загрузку(storage):
    """Признак смены формата ответа. Писать под пустым ключом нельзя."""
    with pytest.raises(StorageError) as exc:
        with storage.run(1, "cards") as writer:
            writer.write([{"название": "без nmID"}])
    assert "nmID" in str(exc.value)
    assert storage.count(1, "cards") == 0


def test_исходный_json_сохраняется_целиком(storage):
    """WB добавляет поля без анонса — они должны оказаться в базе сразу."""
    with storage.run(1, "cards") as writer:
        writer.write([{"nmID": 5, "поле_которого_вчера_не_было": "значение"}])
    assert storage.rows(1, "cards")[0]["поле_которого_вчера_не_было"] == "значение"
