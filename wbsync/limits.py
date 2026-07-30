"""Лимиты запросов WB.

Считаются по аккаунту продавца и раздельно по доменам, поэтому бакет здесь
свой на каждый ``limit_key``: упереться в минутный потолок статистики и из-за
этого притормозить выгрузку карточек было бы обидно и бессмысленно.

Цифры ниже — из официальной документации. Там, где документация лимит не
публикует, стоит осознанно заниженное значение и пометка: лучше выгружать
медленнее, чем поймать блокировку токена. Превышение WB встречает кодом 429.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Rate:
    """Не более ``requests`` запросов за ``seconds`` секунд."""

    requests: int
    seconds: float
    source: str


#: Ключ — ``Method.limit_key``. ``ping.*`` вынесены отдельно: у проверки связи
#: собственный лимит, независимый от лимита самих методов домена.
RATES: dict[str, Rate] = {
    "statistics": Rate(1, 60, "документация: 1 запрос в минуту, жёсткий потолок"),
    "content": Rate(100, 60, "документация: 100 запросов в минуту"),
    "promotion": Rate(5, 1, "документация: 5 запросов в секунду"),
    "feedbacks": Rate(3, 1, "документация: 3 запроса в секунду"),
    "general": Rate(1, 1, "документация: 1 запрос в секунду"),
    "marketplace": Rate(300, 60, "документация: 300 запросов в минуту"),
}

#: Для доменов, чьи таблицы лимитов WB публично не отдаёт.
DEFAULT_RATE = Rate(1, 2, "лимит не опубликован — намеренно заниженная оценка")

PING_RATE = Rate(3, 30, "документация: 3 запроса за 30 секунд на каждый домен")


def rate_for(limit_key: str) -> Rate:
    if limit_key.startswith("ping."):
        return PING_RATE
    return RATES.get(limit_key, DEFAULT_RATE)


class RateLimiter:
    """Скользящее окно на каждый ключ.

    ``sleep`` и ``clock`` вынесены в параметры не ради красоты: тесты на
    минутном лимите статистики иначе шли бы минутами.
    """

    def __init__(self, sleep=time.sleep, clock=time.monotonic) -> None:
        self._sleep = sleep
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, limit_key: str) -> float:
        """Пропускает вызов, при необходимости выждав. Возвращает время ожидания."""
        rate = rate_for(limit_key)
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                hits = self._hits.setdefault(limit_key, deque())
                while hits and now - hits[0] >= rate.seconds:
                    hits.popleft()
                if len(hits) < rate.requests:
                    hits.append(now)
                    return waited
                pause = rate.seconds - (now - hits[0])
            # Спим вне блокировки: иначе поток, ждущий статистику, запер бы
            # собой и все остальные домены.
            self._sleep(pause)
            waited += pause

    def penalize(self, limit_key: str, seconds: float) -> None:
        """Отодвигает окно после 429.

        WB уже сказал, сколько ждать; повторять его ошибку и ломиться раньше
        смысла нет — счётчик заполняется искусственно, чтобы следующий вызов
        честно выждал.
        """
        rate = rate_for(limit_key)
        with self._lock:
            now = self._clock()
            hits = self._hits.setdefault(limit_key, deque())
            hits.clear()
            future = now + max(0.0, seconds) - rate.seconds
            for _ in range(rate.requests):
                hits.append(future)
