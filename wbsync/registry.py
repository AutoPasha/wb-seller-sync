"""Реестр методов Wildberries Seller API.

Единственный источник правды о том, куда коннектору вообще разрешено ходить.
Транспорт не принимает произвольный URL — он принимает имя метода из этого
реестра. Отсюда следует свойство, которое иначе пришлось бы доказывать
построчным чтением кода: **записать что-либо в кабинет продавца невозможно**,
потому что ни одного пишущего пути в реестре нет.

Поле ``source`` у каждого метода — не украшение. Часть сигнатур выверена по
официальной документации дословно, часть взята из практики интеграторов и на
живом токене нами не проверялась. Смешивать эти две категории молча — самый
дешёвый способ отдать заказчику код, который развалится на первом же боевом
запросе.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Source = Literal["doc", "practice"]

#: Домены WB. У каждого свои лимиты и свои независимые падения: лежащий
#: ``statistics-api`` ничего не говорит о состоянии ``content-api``.
HOSTS: dict[str, str] = {
    "general": "https://common-api.wildberries.ru",
    "content": "https://content-api.wildberries.ru",
    "prices": "https://discounts-prices-api.wildberries.ru",
    "marketplace": "https://marketplace-api.wildberries.ru",
    "statistics": "https://statistics-api.wildberries.ru",
    "analytics": "https://seller-analytics-api.wildberries.ru",
    "promotion": "https://advert-api.wildberries.ru",
    "feedbacks": "https://feedbacks-api.wildberries.ru",
    "chat": "https://buyer-chat-api.wildberries.ru",
    "supplies": "https://supplies-api.wildberries.ru",
    "returns": "https://returns-api.wildberries.ru",
    "documents": "https://documents-api.wildberries.ru",
    "finance": "https://finance-api.wildberries.ru",
}

#: Человекочитаемые названия категорий — ровно те, что продавец видит в
#: личном кабинете при выдаче токена. Нужны, чтобы отчёт о токене можно было
#: сверить с экраном ЛК не переводя в уме.
CATEGORY_TITLES: dict[str, str] = {
    "general": "Общее",
    "content": "Контент",
    "prices": "Цены и скидки",
    "marketplace": "Маркетплейс",
    "statistics": "Статистика",
    "analytics": "Аналитика",
    "promotion": "Продвижение",
    "feedbacks": "Вопросы и отзывы",
    "chat": "Чат с покупателями",
    "supplies": "Поставки",
    "returns": "Возвраты покупателями",
    "documents": "Документы",
    "finance": "Финансы",
}


@dataclass(frozen=True)
class Method:
    """Один разрешённый вызов.

    ``limit_key`` отделён от ``category`` намеренно: лимит WB считает по
    домену, но у ``/ping`` внутри того же домена лимит свой собственный
    (3 запроса за 30 секунд), и складывать их в один бакет нельзя.
    """

    name: str
    category: str
    path: str
    http: Literal["GET", "POST"]
    limit_key: str
    source: Source
    doc: str

    @property
    def url(self) -> str:
        return HOSTS[self.category] + self.path


def _ping(category: str) -> Method:
    return Method(
        name=f"ping.{category}",
        category=category,
        path="/ping",
        http="GET",
        limit_key=f"ping.{category}",
        source="doc",
        doc=(
            "Проверяет, что запрос доходит до WB, токен валиден и категория "
            "токена совпадает с сервисом. Не проверяет доступность самого "
            "сервиса."
        ),
    )


METHODS: tuple[Method, ...] = (
    # --- проверка связности и самого токена -----------------------------
    *(_ping(category) for category in HOSTS),
    Method(
        name="seller_info",
        category="general",
        path="/api/v1/seller-info",
        http="GET",
        limit_key="general",
        source="doc",
        doc="Название продавца и его идентификатор. Дешёвая проверка живости токена.",
    ),
    # --- каталог --------------------------------------------------------
    Method(
        name="cards_list",
        category="content",
        path="/content/v2/get/cards/list",
        http="POST",
        limit_key="content",
        source="practice",
        doc=(
            "Карточки товаров постранично. Курсор возвращается в ответе "
            "(updatedAt + nmID) и передаётся в следующий запрос. "
            "POST здесь — способ передать фильтр, а не запись: метод читающий."
        ),
    ),
    # --- статистика -----------------------------------------------------
    Method(
        name="stocks",
        category="statistics",
        path="/api/v1/supplier/stocks",
        http="GET",
        limit_key="statistics",
        source="doc",
        doc="Остатки на складах на момент запроса. Курсор — dateFrom по lastChangeDate.",
    ),
    Method(
        name="orders",
        category="statistics",
        path="/api/v1/supplier/orders",
        http="GET",
        limit_key="statistics",
        source="doc",
        doc=(
            "Заказы. Ответ ограничен примерно 80 000 строк, поэтому в следующий "
            "запрос кладётся lastChangeDate последней строки."
        ),
    ),
    Method(
        name="sales",
        category="statistics",
        path="/api/v1/supplier/sales",
        http="GET",
        limit_key="statistics",
        source="doc",
        doc="Продажи и возвраты. Курсор устроен так же, как у заказов.",
    ),
    Method(
        name="report_detail",
        category="statistics",
        path="/api/v5/supplier/reportDetailByPeriod",
        http="GET",
        limit_key="statistics",
        source="practice",
        doc=(
            "Отчёт о реализации — единственный источник комиссий, логистики и "
            "удержаний. Постранично по курсору rrdid."
        ),
    ),
)

BY_NAME: dict[str, Method] = {m.name: m for m in METHODS}

#: HTTP-глаголы, которые вообще может произнести транспорт. PUT/PATCH/DELETE
#: отсутствуют не «пока», а по устройству: коннектор читающий.
ALLOWED_HTTP = frozenset({"GET", "POST"})


def get(name: str) -> Method:
    try:
        return BY_NAME[name]
    except KeyError:
        raise LookupError(
            f"метод {name!r} не объявлен в реестре; "
            f"известны: {', '.join(sorted(BY_NAME))}"
        ) from None


def methods_of(category: str) -> tuple[Method, ...]:
    return tuple(m for m in METHODS if m.category == category and m.path != "/ping")
