"""Разбор токена Wildberries.

Токен WB — это JWT: три части через точку, посередине base64 с полезной
нагрузкой. Подпись проверить нельзя, ключ есть только у самого Wildberries,
поэтому здесь именно **чтение заявленных данных**, а не проверка подлинности.
Практическая ценность от этого не страдает: нам нужно узнать срок годности и
контур до того, как токен уйдёт в бой, а подлинность всё равно подтвердит
первый же ответ API.

Зачем это вообще нужно. У WB нет refresh-flow: токен живёт до 180 дней и
умирает молча. Синхронизация после этого не падает с грохотом, она просто
перестаёт приносить данные — а выглядит это как «ваш коннектор сломался».
Поэтому срок годности читается заранее и показывается рядом со статусом
магазина, а не выясняется постфактум.

Отдельно про тип токена. С 30 марта 2026 у WB четыре типа, и у базовых лимиты
обрушены вплоть до одного запроса в 3–24 часа. Тип **не** выводится из
полезной нагрузки: публично задокументированного поля с типом нет, а гадать
по битовой маске — значит однажды тихо соврать. Тип определяется по поведению
живого API, см. ``diagnose_rate_limit``.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

#: С этого момента токен пора менять: 180-дневный срок, а предупреждать
#: имеет смысл настолько заранее, чтобы человек успел дойти до кабинета.
EXPIRY_WARNING = timedelta(days=14)


class TokenError(ValueError):
    """Токен нечитаем: не JWT, битый base64 или payload не является объектом."""


@dataclass(frozen=True)
class TokenInfo:
    """Что удалось прочитать из токена.

    ``seller_id`` и ``sandbox`` помечены как сведения по практике: поля ``sid``
    и ``t`` в полезной нагрузке присутствуют у всех виденных токенов, но
    официальной таблицы полей WB не публикует. Если поля нет — здесь будет
    ``None``, а не выдуманное значение.
    """

    expires_at: datetime | None
    issued_at: datetime | None
    seller_id: str | None
    sandbox: bool | None
    raw_claims: dict = field(repr=False, default_factory=dict)

    def days_left(self, now: datetime | None = None) -> int | None:
        if self.expires_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (self.expires_at - now).days

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= (now or datetime.now(timezone.utc))

    def expires_soon(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        return not self.is_expired(now) and self.expires_at - now <= EXPIRY_WARNING


def _decode_segment(segment: str) -> dict:
    # base64url без выравнивания: JWT его срезает, а base64 требует.
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise TokenError(f"полезная нагрузка токена не декодируется: {exc}") from exc
    try:
        claims = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TokenError(f"полезная нагрузка токена не JSON: {exc}") from exc
    if not isinstance(claims, dict):
        raise TokenError("полезная нагрузка токена не является объектом")
    return claims


def _timestamp(claims: dict, key: str) -> datetime | None:
    value = claims.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse(token: str) -> TokenInfo:
    """Читает JWT WB. Подпись не проверяется — проверить её нечем."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise TokenError(
            "не похоже на JWT: ожидались три части через точку, "
            f"получено {len(parts)}"
        )
    claims = _decode_segment(parts[1])
    sandbox = claims.get("t")
    return TokenInfo(
        expires_at=_timestamp(claims, "exp"),
        issued_at=_timestamp(claims, "iat"),
        seller_id=str(claims["sid"]) if claims.get("sid") is not None else None,
        sandbox=bool(sandbox) if isinstance(sandbox, bool) else None,
        raw_claims=claims,
    )


def fingerprint(token: str) -> str:
    """Короткая метка токена для журналов.

    Токен целиком в лог попадать не должен никогда, но отличать «тот же самый
    токен» от «другого» в диагностике нужно постоянно.
    """
    signature = token.strip().rsplit(".", 1)[-1]
    return f"…{signature[-6:]}" if signature else "(пусто)"


#: Порог, выше которого пауза перестаёт быть «подожди секунду» и становится
#: приговором для синхронизации. Персональный токен на статистике живёт с
#: лимитом порядка минуты; базовый — часами.
SLOW_TOKEN_RETRY_AFTER = 600


def diagnose_rate_limit(retry_after: float | None, category: str) -> str | None:
    """Объясняет 429 человеческим языком.

    Возвращает предупреждение, если ответ WB выглядит как признак базового
    токена: на статистике такой токен получает паузы в часы, и никакой
    ретрай эту ситуацию не спасёт — нужен другой токен из кабинета.
    """
    if retry_after is None or retry_after < SLOW_TOKEN_RETRY_AFTER:
        return None
    hours = retry_after / 3600
    return (
        f"WB просит подождать {hours:.1f} ч перед следующим запросом к категории "
        f"«{category}». Так ведут себя базовые токены: их лимиты снижены до "
        f"одного запроса в 3–24 часа. Регулярная синхронизация на таком токене "
        f"невозможна — нужен персональный или сервисный токен из личного "
        f"кабинета продавца."
    )
