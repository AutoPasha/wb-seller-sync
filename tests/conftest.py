"""Общая оснастка тестов.

Сети здесь нет вообще: WB подменяется очередью заранее заготовленных ответов,
а часы и сон — счётчиками. Иначе тест на минутный лимит статистики шёл бы
минуту, и его бы просто выключили.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import pytest

from wbsync import limits
from wbsync.transport import Transport


@dataclass
class FakeResponse:
    status_code: int = 200
    payload: object = None
    headers: dict = field(default_factory=dict)
    text_body: str = ""

    def json(self):
        if self.payload is None:
            raise ValueError("нет тела")
        return self.payload

    @property
    def text(self) -> str:
        return self.text_body or json.dumps(self.payload, ensure_ascii=False)


class FakeSession:
    """Отдаёт ответы по очереди и запоминает, о чём её спросили."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def request(self, http, url, *, params=None, json=None, headers=None, timeout=None):
        self.calls.append(
            {
                "http": http,
                "url": url,
                "params": params,
                "body": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError(f"лишний запрос к {url}: ответы кончились")
        return self.responses.pop(0)


class FakeClock:
    """Часы, которые двигает только сон."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_transport(clock):
    def factory(responses: list[FakeResponse], **kwargs) -> tuple[Transport, FakeSession]:
        session = FakeSession(responses)
        transport = Transport(
            kwargs.pop("token", make_token()),
            session=session,
            limiter=limits.RateLimiter(sleep=clock.sleep, clock=clock.monotonic),
            sleep=clock.sleep,
            jitter=lambda: 0.5,
            **kwargs,
        )
        return transport, session

    return factory


def make_token(**claims) -> str:
    """Собирает правдоподобный JWT WB. Подпись фиктивная — её никто не проверяет."""
    payload = {"sid": "seller-1", "t": False, "exp": 4102444800}
    payload.update(claims)
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    return f"header.{encoded}.signature"
