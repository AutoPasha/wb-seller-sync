"""Сценарии выгрузки: транспорт + курсор + снимок.

Каждый сценарий отвечает за один набор данных и держится одного правила:
выгрузка либо доходит до конца и публикуется целиком, либо не публикуется.
Промежуточных состояний в снимке нет — на них потом строят отчёты.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import cursors, registry, storage as storage_module
from .transport import Transport

#: Ограничитель на число страниц: защита от бесконечного цикла, если WB
#: однажды начнёт отдавать курсор, который не двигается способом, нам пока
#: неизвестным. Лучше остановиться с внятной ошибкой, чем крутиться сутки.
MAX_PAGES = 1000

CARDS_PAGE = 100


@dataclass
class SyncResult:
    dataset: str
    rows: int
    pages: int
    warnings: list[str] = field(default_factory=list)
    stopped_early: str = ""

    @property
    def ok(self) -> bool:
        return not self.stopped_early


def _iso(moment: datetime | date) -> str:
    if isinstance(moment, datetime):
        return moment.strftime("%Y-%m-%dT%H:%M:%S")
    return moment.isoformat()


def sync_cards(
    transport: Transport,
    storage: storage_module.Storage,
    store_id: int,
    *,
    page_size: int = CARDS_PAGE,
) -> SyncResult:
    """Карточки товаров целиком.

    Курсор WB отдаёт в ответе (``updatedAt`` + ``nmID``), его же кладём в
    следующий запрос. Признак последней страницы — вернулось меньше строк,
    чем просили: отдельного флага «это конец» в ответе нет.
    """
    result = SyncResult(dataset="cards", rows=0, pages=0)
    cursor: dict = {"limit": page_size}

    with storage.run(store_id, "cards") as writer:
        writer.replace_all()
        for _ in range(MAX_PAGES):
            body = {"settings": {"cursor": dict(cursor), "filter": {"withPhoto": -1}}}
            payload = transport.call("cards_list", body=body)
            writer.count_request()
            cards = payload.get("cards") or []
            result.pages += 1
            if not cards:
                break
            writer.write(cards)
            result.rows += len(cards)

            got = payload.get("cursor") or {}
            if len(cards) < page_size:
                break
            if not got.get("updatedAt") or got.get("nmID") is None:
                result.stopped_early = (
                    "WB не вернул курсор для следующей страницы — выгрузка "
                    "остановлена, чтобы не начать читать каталог заново."
                )
                break
            cursor = {
                "limit": page_size,
                "updatedAt": got["updatedAt"],
                "nmID": got["nmID"],
            }
            writer.save_cursor(f"{got['updatedAt']}|{got['nmID']}")
        else:
            result.stopped_early = f"превышен предел в {MAX_PAGES} страниц"

    return result


def _sync_change_feed(
    transport: Transport,
    storage: storage_module.Storage,
    store_id: int,
    *,
    dataset: str,
    method: str,
    date_from: datetime,
    nudge: bool,
    extra_params: dict | None = None,
) -> SyncResult:
    """Общий обход для ``stocks``/``orders``/``sales``.

    Все три устроены одинаково: ``dateFrom`` по ``lastChangeDate``, ответ до
    ~80 000 строк, продолжение — с метки последней строки. И одинаково же
    ломаются, когда у всей страницы метка совпадает.
    """
    result = SyncResult(dataset=dataset, rows=0, pages=0)
    current = _iso(date_from)

    with storage.run(store_id, dataset) as writer:
        if dataset == "stocks":
            # Остатки — снимок на момент запроса, накапливать их бессмысленно.
            writer.replace_all()
        for _ in range(MAX_PAGES):
            params = {"dateFrom": current}
            params.update(extra_params or {})
            rows = transport.call(method, params=params)
            writer.count_request()
            result.pages += 1
            if not isinstance(rows, list):
                result.stopped_early = f"ожидался список строк, пришло {type(rows).__name__}"
                break
            if not rows:
                break
            writer.write(rows)
            result.rows += len(rows)

            try:
                step = cursors.advance_change_date(rows, current, nudge=nudge)
            except cursors.CursorStuck as exc:
                result.stopped_early = str(exc)
                break
            if step.warning:
                result.warnings.append(step.warning)
            if step.done or step.next_cursor is None:
                break
            current = str(step.next_cursor)
            writer.save_cursor(current)
        else:
            result.stopped_early = f"превышен предел в {MAX_PAGES} страниц"

    return result


def sync_stocks(
    transport: Transport,
    storage: storage_module.Storage,
    store_id: int,
    *,
    date_from: datetime | None = None,
    nudge: bool = False,
) -> SyncResult:
    return _sync_change_feed(
        transport,
        storage,
        store_id,
        dataset="stocks",
        method="stocks",
        date_from=date_from or datetime.now(timezone.utc) - timedelta(days=1),
        nudge=nudge,
    )


def sync_orders(
    transport: Transport,
    storage: storage_module.Storage,
    store_id: int,
    *,
    date_from: datetime,
    nudge: bool = False,
) -> SyncResult:
    return _sync_change_feed(
        transport,
        storage,
        store_id,
        dataset="orders",
        method="orders",
        date_from=date_from,
        nudge=nudge,
        extra_params={"flag": 0},
    )


def sync_sales(
    transport: Transport,
    storage: storage_module.Storage,
    store_id: int,
    *,
    date_from: datetime,
    nudge: bool = False,
) -> SyncResult:
    return _sync_change_feed(
        transport,
        storage,
        store_id,
        dataset="sales",
        method="sales",
        date_from=date_from,
        nudge=nudge,
        extra_params={"flag": 0},
    )


def sync_realization(
    transport: Transport,
    storage: storage_module.Storage,
    store_id: int,
    *,
    today: date | None = None,
    window_days: int = cursors.REALIZATION_WINDOW_DAYS,
    page_size: int = 100_000,
) -> SyncResult:
    """Отчёт о реализации за скользящее окно.

    Окно перечитывается целиком при каждом запуске, потому что WB уточняет
    уже отданные строки ещё 7–14 дней после конца периода. Это же причина,
    по которой сверять цифры с кабинетом имеет смысл только по закрытым
    периодам: по незакрытому расходятся не наши расчёты, а сам источник.
    """
    today = today or datetime.now(timezone.utc).date()
    since, until = cursors.realization_window(today, days=window_days)
    result = SyncResult(dataset="realization", rows=0, pages=0)
    rrdid = 0

    with storage.run(store_id, "realization") as writer:
        writer.drop_period(since, until)
        for _ in range(MAX_PAGES):
            rows = transport.call(
                "report_detail",
                params={
                    "dateFrom": since.isoformat(),
                    "dateTo": until.isoformat(),
                    "limit": page_size,
                    "rrdid": rrdid,
                },
            )
            writer.count_request()
            result.pages += 1
            if not rows:
                break
            if not isinstance(rows, list):
                result.stopped_early = f"ожидался список строк, пришло {type(rows).__name__}"
                break
            writer.write(rows)
            result.rows += len(rows)

            try:
                step = cursors.advance_rrdid(rows, rrdid)
            except cursors.CursorStuck as exc:
                result.stopped_early = str(exc)
                break
            if step.done or step.next_cursor is None:
                break
            rrdid = int(step.next_cursor)
            writer.save_cursor(rrdid)
        else:
            result.stopped_early = f"превышен предел в {MAX_PAGES} страниц"

    return result


@dataclass
class TokenReport:
    """Что токен умеет на самом деле, а не что о нём думают."""

    label: str
    days_left: int | None
    sandbox: bool | None
    seller_id: str | None
    categories: dict[str, bool]
    problems: list[str] = field(default_factory=list)

    @property
    def usable_for_sync(self) -> bool:
        return self.categories.get("statistics", False) and not self.problems


def check_token(transport: Transport, raw_token: str) -> TokenReport:
    """Отчёт о токене: срок, контур и реально доступные категории.

    Категории проверяются вызовом ``/ping`` на каждом домене — по
    документации этот метод проверяет в том числе совпадение категории токена
    и сервиса. Разбирать битовую маску внутри JWT было бы быстрее, но её
    раскладка нигде официально не описана, а ошибка здесь означает, что
    коннектор уверенно сообщит доступ, которого нет.
    """
    from . import token as token_module

    info = token_module.parse(raw_token)
    problems: list[str] = []
    if info.is_expired():
        problems.append("Токен просрочен — WB отклонит любой запрос.")
    elif info.expires_soon():
        problems.append(
            f"Токен истекает через {info.days_left()} дн. Обновить его можно "
            f"только вручную в кабинете продавца: refresh-flow у WB нет."
        )
    if info.sandbox:
        problems.append("Это токен тестового контура — боевых данных в нём нет.")

    categories = {name: transport.ping(name) for name in registry.HOSTS}
    if not categories.get("statistics"):
        problems.append(
            "Категория «Статистика» этому токену недоступна, а без неё нет ни "
            "остатков, ни продаж, ни отчёта о реализации."
        )
    if not categories.get("content"):
        problems.append("Категория «Контент» недоступна — карточки товаров не выгрузить.")

    return TokenReport(
        label=token_module.fingerprint(raw_token),
        days_left=info.days_left(),
        sandbox=info.sandbox,
        seller_id=info.seller_id,
        categories=categories,
        problems=problems,
    )
