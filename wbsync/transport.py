"""HTTP-слой: единственное место, где коннектор разговаривает с Wildberries.

Свойство, ради которого слой устроен именно так: **вызвать можно только то,
что объявлено в реестре.** ``call`` принимает имя метода, а не URL, поэтому
запрос на запись отклоняется до отправки — не потому, что кто-то не забыл
проверку, а потому, что произнести такой запрос нечем.

Для интеграции с кабинетом продавца это не паранойя. Ошибка в читающем методе
стоит одного неверного отчёта; ошибка в пишущем — реальных цен на витрине.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from . import limits, registry, token as token_module

#: WB ожидает токен в Authorization как есть, без схемы Bearer.
AUTH_HEADER = "Authorization"

DEFAULT_TIMEOUT = 30.0
DEFAULT_ATTEMPTS = 4


class WbError(RuntimeError):
    """Любая неудача при обращении к WB."""


class MethodNotAllowed(WbError):
    """Запрошено то, чего нет в реестре. До сети дело не дошло."""


class TokenRejected(WbError):
    """401: токен просрочен, отозван или не той категории."""


class AccessDenied(WbError):
    """403: категория недоступна этому токену либо нужна платная подписка."""


class RateLimited(WbError):
    """429, переживший все попытки."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceUnavailable(WbError):
    """5xx или сетевой сбой, не прошедший за отведённые попытки."""


@dataclass
class Event:
    """Строка для журнала. Токена здесь нет и быть не может."""

    method: str
    status: int | None
    attempt: int
    waited: float
    note: str = ""


def _retry_after(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # По RFC там может стоять дата. Разбирать её ради оценки паузы
        # смысла нет: наверх уйдёт None, а пауза будет считаться по backoff.
        return None


def _mentions_jam(text: str) -> bool:
    """403 из-за отсутствия подписки «Джем» приходит текстом, без кода ошибки."""
    lowered = text.lower()
    return "джем" in lowered or "jam" in lowered


class Transport:
    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        limiter: limits.RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
        max_attempts: int = DEFAULT_ATTEMPTS,
        on_event: Callable[[Event], None] | None = None,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not token or not token.strip():
            raise ValueError("пустой токен")
        self._token = token.strip()
        self._session = session or requests.Session()
        self._limiter = limiter or limits.RateLimiter(sleep=sleep)
        self._sleep = sleep
        self._timeout = timeout
        self._max_attempts = max(1, max_attempts)
        self._on_event = on_event or (lambda event: None)
        self._jitter = jitter
        self.token_label = token_module.fingerprint(token)

    # -- публичный интерфейс ------------------------------------------------

    def call(
        self,
        method_name: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        method = registry.get(method_name)
        if method.http not in registry.ALLOWED_HTTP:
            raise MethodNotAllowed(
                f"метод {method.name} объявлен с глаголом {method.http}, "
                f"а транспорт умеет только {sorted(registry.ALLOWED_HTTP)}"
            )
        if body is not None and method.http != "POST":
            raise MethodNotAllowed(
                f"методу {method.name} нельзя передать тело: он объявлен как GET"
            )
        return self._request(method, params=params, body=body)

    def ping(self, category: str) -> bool:
        """Отвечает ли домен этому токену.

        Именно так определяется, какие категории токену реально разрешены:
        ``/ping`` по документации проверяет в том числе совпадение категории
        токена и сервиса. Это надёжнее, чем гадать по битам внутри JWT.
        """
        try:
            self.call(f"ping.{category}")
        except (AccessDenied, TokenRejected):
            return False
        return True

    # -- внутренности -------------------------------------------------------

    def _request(
        self,
        method: registry.Method,
        *,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
    ) -> Any:
        last_error: WbError | None = None
        for attempt in range(1, self._max_attempts + 1):
            waited = self._limiter.acquire(method.limit_key)
            try:
                response = self._session.request(
                    method.http,
                    method.url,
                    params=params,
                    json=body,
                    headers={AUTH_HEADER: self._token},
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                last_error = ServiceUnavailable(f"{method.name}: сеть недоступна ({exc})")
                self._emit(method, None, attempt, waited, "сетевой сбой")
                self._backoff(attempt)
                continue

            status = response.status_code
            self._emit(method, status, attempt, waited)

            if status == 200:
                return self._decode(method, response)
            if status == 401:
                raise TokenRejected(
                    f"{method.name}: WB отверг токен {self.token_label}. "
                    f"Обычно это просроченный токен (срок до 180 дней, "
                    f"обновляется только вручную) или токен без категории "
                    f"«{registry.CATEGORY_TITLES.get(method.category, method.category)}»."
                )
            if status == 403:
                text = response.text[:500]
                if _mentions_jam(text):
                    raise AccessDenied(
                        f"{method.name}: нужна платная подписка «Джем». "
                        f"Ответ WB: {text.strip()}"
                    )
                raise AccessDenied(
                    f"{method.name}: доступ запрещён. Ответ WB: {text.strip()}"
                )
            if status == 429:
                retry_after = _retry_after(response)
                hint = token_module.diagnose_rate_limit(retry_after, method.category)
                self._limiter.penalize(
                    method.limit_key, retry_after if retry_after else 0.0
                )
                last_error = RateLimited(
                    f"{method.name}: лимит запросов исчерпан"
                    + (f". {hint}" if hint else ""),
                    retry_after,
                )
                if hint:
                    # Базовый токен ретраями не лечится: пауза измеряется
                    # часами, и «подождём и повторим» превращается в зависший
                    # процесс без объяснения причины.
                    raise last_error
                self._sleep(retry_after if retry_after else self._pause(attempt))
                continue
            if 500 <= status < 600:
                last_error = ServiceUnavailable(
                    f"{method.name}: WB ответил {status}"
                )
                self._backoff(attempt)
                continue

            raise WbError(
                f"{method.name}: неожиданный ответ {status}: {response.text[:300]}"
            )

        assert last_error is not None
        raise last_error

    def _decode(self, method: registry.Method, response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise WbError(
                f"{method.name}: ответ не разбирается как JSON ({exc})"
            ) from exc

    def _pause(self, attempt: int) -> float:
        # Экспонента с джиттером: без джиттера параллельные магазины после
        # общего 429 синхронно проснутся и получат его снова.
        return min(60.0, 2.0**attempt) * (0.5 + self._jitter())

    def _backoff(self, attempt: int) -> None:
        self._sleep(self._pause(attempt))

    def _emit(
        self,
        method: registry.Method,
        status: int | None,
        attempt: int,
        waited: float,
        note: str = "",
    ) -> None:
        self._on_event(
            Event(
                method=method.name,
                status=status,
                attempt=attempt,
                waited=round(waited, 2),
                note=note,
            )
        )
