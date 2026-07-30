"""Курсоры: три известные грабли, каждая воспроизведена."""

from __future__ import annotations

from datetime import date

import pytest

from wbsync import cursors


def test_курсор_двигается_по_максимуму_а_не_по_последней_строке():
    """Ответ WB не обязан быть отсортированным."""
    rows = [
        {"lastChangeDate": "2026-01-05T10:00:00"},
        {"lastChangeDate": "2026-01-07T10:00:00"},
        {"lastChangeDate": "2026-01-06T10:00:00"},
    ]
    step = cursors.advance_change_date(rows, "2026-01-01T00:00:00")
    assert step.next_cursor == "2026-01-07T10:00:00"


def test_одинаковая_метка_на_всей_странице_останавливает_выгрузку():
    """Иначе выгрузка бесконечно читает одну и ту же страницу."""
    rows = [{"lastChangeDate": "2026-01-05T10:00:00"} for _ in range(3)]
    with pytest.raises(cursors.CursorStuck) as exc:
        cursors.advance_change_date(rows, "2026-01-05T10:00:00")
    assert "застрял" in str(exc.value)


def test_ручной_сдвиг_разрешён_но_честно_предупреждает():
    rows = [{"lastChangeDate": "2026-01-05T10:00:00"} for _ in range(3)]
    step = cursors.advance_change_date(rows, "2026-01-05T10:00:00", nudge=True)
    assert step.next_cursor == "2026-01-05T10:00:00.001"
    assert "не войдут" in step.warning


def test_сдвиг_сохраняет_миллисекунды():
    rows = [{"lastChangeDate": "2026-01-05T10:00:00.500"}]
    step = cursors.advance_change_date(rows, "2026-01-05T10:00:00.500", nudge=True)
    assert step.next_cursor == "2026-01-05T10:00:00.501"


def test_пустая_страница_завершает_обход():
    step = cursors.advance_change_date([], "2026-01-01T00:00:00")
    assert step.done and step.rows == 0


def test_rrdid_берётся_максимумом_иначе_будут_дубли():
    """Классика: взяли rrd_id последней строки, получили задвоенные суммы."""
    rows = [{"rrd_id": 500}, {"rrd_id": 900}, {"rrd_id": 700}]
    step = cursors.advance_rrdid(rows, current=0)
    assert step.next_cursor == 900


def test_не_выросший_rrdid_останавливает_выгрузку():
    rows = [{"rrd_id": 100}, {"rrd_id": 90}]
    with pytest.raises(cursors.CursorStuck):
        cursors.advance_rrdid(rows, current=100)


def test_окно_реализации_шесть_недель():
    since, until = cursors.realization_window(date(2026, 7, 30))
    assert (until - since).days == 42
    assert until == date(2026, 7, 30)


def test_отсутствие_ключевого_поля_не_проходит_молча():
    with pytest.raises(cursors.CursorStuck):
        cursors.advance_change_date([{"иное": 1}], "2026-01-01T00:00:00")
