"""Транспорт: запрет записи, лимиты, разбор ошибок WB."""

from __future__ import annotations

import pytest

from wbsync import registry
from wbsync.transport import (
    AccessDenied,
    MethodNotAllowed,
    RateLimited,
    ServiceUnavailable,
    TokenRejected,
)

from .conftest import FakeResponse


def test_реестр_не_содержит_пишущих_методов():
    """Главная гарантия коннектора, и проверяется она не глазами."""
    assert all(m.http in {"GET", "POST"} for m in registry.METHODS)
    assert registry.ALLOWED_HTTP == {"GET", "POST"}
    # POST в реестре допустим только там, где им передают фильтр чтения.
    posts = [m for m in registry.METHODS if m.http == "POST"]
    assert [m.name for m in posts] == ["cards_list"]


def test_незнакомый_метод_не_доходит_до_сети(make_transport):
    transport, session = make_transport([])
    with pytest.raises(LookupError):
        transport.call("cards_update")
    assert session.calls == [], "запрос ушёл в сеть, хотя метода нет в реестре"


def test_телу_нет_места_в_get_методе(make_transport):
    transport, session = make_transport([])
    with pytest.raises(MethodNotAllowed):
        transport.call("stocks", body={"anything": 1})
    assert session.calls == []


def test_токен_уходит_в_заголовке_без_схемы(make_transport):
    transport, session = make_transport([FakeResponse(payload=[])])
    transport.call("stocks", params={"dateFrom": "2026-01-01"})
    assert session.calls[0]["headers"]["Authorization"].startswith("header.")
    assert "Bearer" not in session.calls[0]["headers"]["Authorization"]


def test_401_объясняет_что_проверить(make_transport):
    transport, _ = make_transport([FakeResponse(status_code=401, text_body="")])
    with pytest.raises(TokenRejected) as exc:
        transport.call("stocks", params={"dateFrom": "2026-01-01"})
    assert "180 дней" in str(exc.value)


def test_403_про_джем_называет_подписку(make_transport):
    transport, _ = make_transport(
        [FakeResponse(status_code=403, text_body="no active jam subscription")]
    )
    with pytest.raises(AccessDenied) as exc:
        transport.call("stocks", params={"dateFrom": "2026-01-01"})
    assert "Джем" in str(exc.value)


def test_429_с_короткой_паузой_повторяется(make_transport, clock):
    transport, session = make_transport(
        [
            FakeResponse(status_code=429, headers={"Retry-After": "5"}),
            FakeResponse(payload=[{"lastChangeDate": "2026-01-02T00:00:00"}]),
        ]
    )
    rows = transport.call("stocks", params={"dateFrom": "2026-01-01"})
    assert rows and len(session.calls) == 2
    assert 5 in clock.slept, "пауза из Retry-After проигнорирована"


def test_429_с_многочасовой_паузой_не_ретраится_а_объясняется(make_transport):
    """Базовый токен ретраями не лечится — он лечится другим токеном."""
    transport, session = make_transport(
        [FakeResponse(status_code=429, headers={"Retry-After": "10800"})]
    )
    with pytest.raises(RateLimited) as exc:
        transport.call("stocks", params={"dateFrom": "2026-01-01"})
    assert "базовые токены" in str(exc.value)
    assert len(session.calls) == 1, "повторять запрос с паузой в часы бессмысленно"


def test_пятисотка_повторяется_и_сдаётся(make_transport):
    transport, session = make_transport(
        [FakeResponse(status_code=502) for _ in range(3)], max_attempts=3
    )
    with pytest.raises(ServiceUnavailable):
        transport.call("stocks", params={"dateFrom": "2026-01-01"})
    assert len(session.calls) == 3


def test_минутный_лимит_статистики_соблюдается(make_transport, clock):
    transport, _ = make_transport([FakeResponse(payload=[]), FakeResponse(payload=[])])
    transport.call("stocks", params={"dateFrom": "2026-01-01"})
    transport.call("stocks", params={"dateFrom": "2026-01-02"})
    assert sum(clock.slept) >= 60, "второй запрос к статистике ушёл раньше минуты"


def test_лимиты_доменов_независимы(make_transport, clock):
    """Упёрлись в минутный потолок статистики — карточки ждать не должны."""
    transport, _ = make_transport(
        [FakeResponse(payload=[]), FakeResponse(payload={"cards": []})]
    )
    transport.call("stocks", params={"dateFrom": "2026-01-01"})
    transport.call("cards_list", body={"settings": {"cursor": {"limit": 1}}})
    assert clock.slept == [], "выгрузка карточек ждала чужой лимит"


def test_ping_отвечает_доступна_ли_категория(make_transport):
    transport, _ = make_transport(
        [FakeResponse(payload={"Status": "OK"}), FakeResponse(status_code=403, text_body="")]
    )
    assert transport.ping("content") is True
    assert transport.ping("finance") is False
