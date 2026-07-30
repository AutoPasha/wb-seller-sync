"""Разбор токена и предупреждения о его сроке."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from wbsync import token as token_module

from .conftest import make_token


def _ts(delta: timedelta) -> int:
    return int((datetime.now(timezone.utc) + delta).timestamp())


def test_читает_срок_продавца_и_контур():
    raw = make_token(exp=_ts(timedelta(days=100)), sid="seller-42", t=False)
    info = token_module.parse(raw)
    assert info.seller_id == "seller-42"
    assert info.sandbox is False
    assert 99 <= info.days_left() <= 100


def test_просроченный_токен_виден_до_первого_запроса():
    info = token_module.parse(make_token(exp=_ts(timedelta(days=-1))))
    assert info.is_expired()


def test_предупреждение_за_две_недели():
    """У WB нет refresh — узнать о смерти токена постфактум значит простой."""
    info = token_module.parse(make_token(exp=_ts(timedelta(days=10))))
    assert info.expires_soon() and not info.is_expired()


def test_тестовый_контур_распознаётся():
    assert token_module.parse(make_token(t=True)).sandbox is True


def test_нежвт_отвергается_понятно():
    with pytest.raises(token_module.TokenError) as exc:
        token_module.parse("просто-строка")
    assert "три части" in str(exc.value)


def test_битая_нагрузка_не_роняет_разбор_молча():
    with pytest.raises(token_module.TokenError):
        token_module.parse("header.!!!нея-base64!!!.signature")


def test_отсутствующие_поля_дают_none_а_не_выдумку():
    info = token_module.parse(make_token(sid=None, t=None, exp=None))
    assert info.seller_id is None and info.sandbox is None and info.expires_at is None
    assert info.days_left() is None


def test_отпечаток_не_раскрывает_токен():
    raw = make_token()
    label = token_module.fingerprint(raw)
    assert label not in raw[:-6] and len(label) <= 8


def test_многочасовая_пауза_опознаётся_как_базовый_токен():
    hint = token_module.diagnose_rate_limit(10800, "statistics")
    assert hint and "3–24 часа" in hint


def test_обычная_пауза_не_повод_для_тревоги():
    assert token_module.diagnose_rate_limit(60, "statistics") is None
