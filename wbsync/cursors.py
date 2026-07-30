"""Курсоры постраничной выгрузки — и три способа на них погореть.

Все три собраны не из головы: это известные грабли боевых выгрузок WB,
из-за которых данные либо теряются, либо задваиваются, причём молча.
Здесь они вынесены в отдельный модуль без сети, чтобы каждую можно было
показать тестом, а не абзацем в README.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

#: Отчёт о реализации появляется через 2–4 дня после конца недели и потом
#: ещё 7–14 дней перегенерируется: строки добавляются, суммы уточняются,
#: возвраты переезжают. Значит «загрузили один раз» не работает в принципе —
#: хвост нужно перечитывать. Шесть недель закрывают и появление, и правки.
REALIZATION_WINDOW_DAYS = 42


class CursorStuck(RuntimeError):
    """Курсор не сдвинулся: следующий запрос вернёт ту же страницу."""


@dataclass(frozen=True)
class Step:
    """Результат обработки одной страницы."""

    next_cursor: str | int | None
    rows: int
    done: bool
    warning: str = ""


def advance_change_date(
    rows: list[dict],
    current: str,
    *,
    field: str = "lastChangeDate",
    nudge: bool = False,
) -> Step:
    """Двигает курсор статистики (``stocks``/``orders``/``sales``).

    Документация предлагает брать ``lastChangeDate`` последней строки ответа.
    На практике этого мало: сортировка ответа не гарантирована, поэтому здесь
    берётся максимум по всей странице — он не может оказаться меньше нужного.

    Главная ловушка в другом. Если у всей страницы одинаковый
    ``lastChangeDate`` — а это ровно то, что происходит при массовой
    перезаливке остатков, — новый курсор равен старому, и выгрузка начинает
    бесконечно читать одну и ту же страницу. Внешне это выглядит как «долго
    синхронизируется», и заметить это можно только по счётчику.

    Выхода два, и оба с потерей: остановиться (и не увидеть данные за этот
    момент времени) или сдвинуть курсор на миллисекунду вперёд (и потерять
    строки с той же меткой, не попавшие на страницу). Молча выбирать за
    вызывающего нельзя, поэтому по умолчанию — честная остановка,
    а ``nudge=True`` включает сдвиг с явным предупреждением.
    """
    if not rows:
        return Step(next_cursor=None, rows=0, done=True)

    values = [row[field] for row in rows if row.get(field)]
    if not values:
        raise CursorStuck(
            f"в ответе нет поля {field}: продолжать выгрузку нечем "
            f"(строк на странице: {len(rows)})"
        )

    highest = max(values)
    if highest > current:
        return Step(next_cursor=highest, rows=len(rows), done=False)

    if not nudge:
        raise CursorStuck(
            f"курсор застрял на {current}: у всех {len(rows)} строк страницы "
            f"одинаковый {field}. Следующий запрос вернул бы то же самое."
        )

    return Step(
        next_cursor=_nudge(highest),
        rows=len(rows),
        done=False,
        warning=(
            f"курсор сдвинут вручную с {highest}: строки с этой же меткой "
            f"времени, не попавшие на страницу, в выгрузку не войдут"
        ),
    )


def _nudge(value: str) -> str:
    """Прибавляет миллисекунду к метке RFC3339, сохраняя её формат."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            moment = datetime.strptime(value[:26], fmt)
        except ValueError:
            continue
        return (moment + timedelta(milliseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    raise CursorStuck(f"метка времени {value!r} не разбирается, сдвинуть её нечем")


def advance_rrdid(rows: list[dict], current: int, *, field: str = "rrd_id") -> Step:
    """Двигает курсор отчёта о реализации.

    Курсор здесь — ``rrd_id`` последней **прочитанной** строки, и именно на
    слове «последней» ломаются выгрузки: берут ``rows[-1]``, а строки в ответе
    идут не по возрастанию. Курсор откатывается назад, следующая страница
    приезжает повторно — и в отчёте появляются задвоенные суммы, которые потом
    ищут в сверке с кабинетом.

    Правильно — максимум по всей странице.
    """
    if not rows:
        return Step(next_cursor=None, rows=0, done=True)

    values = [row[field] for row in rows if isinstance(row.get(field), int)]
    if not values:
        raise CursorStuck(f"в ответе нет числового поля {field}")

    highest = max(values)
    if highest <= current:
        raise CursorStuck(
            f"курсор {field} не вырос: было {current}, максимум на странице "
            f"{highest}. Продолжение вернуло бы те же строки."
        )
    return Step(next_cursor=highest, rows=len(rows), done=False)


def realization_window(
    today: date, *, days: int = REALIZATION_WINDOW_DAYS
) -> tuple[date, date]:
    """Период, который отчёт о реализации нужно перечитывать каждый раз.

    Возвращает полуинтервал, пригодный для ``dateFrom``/``dateTo``.
    Перечитывается он целиком: попытка догружать только новое даёт витрину,
    которая расходится с кабинетом на суммы уже уточнённых возвратов.
    """
    if days < 1:
        raise ValueError("окно перечитывания не может быть короче суток")
    return today - timedelta(days=days), today
