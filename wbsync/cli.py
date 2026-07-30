"""Командная строка коннектора.

Токен берётся из окружения (``WB_TOKEN`` или ``WB_TOKEN_<номер магазина>``) и
никогда не принимается аргументом: аргументы попадают в историю оболочки и в
список процессов, откуда их видит любой пользователь машины.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

from . import registry, sync, token as token_module
from .storage import Storage
from .transport import Event, Transport, WbError

DEFAULT_DB = "wb_snapshot.db"


def _token_for(store_id: int) -> str:
    for name in (f"WB_TOKEN_{store_id}", "WB_TOKEN"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    raise SystemExit(
        f"не задан токен: положите его в переменную окружения WB_TOKEN_{store_id} "
        f"или WB_TOKEN. Аргументом командной строки токен не принимается — "
        f"он утёк бы в историю оболочки и в список процессов."
    )


def _verbose_logger(enabled: bool):
    def log(event: Event) -> None:
        if not enabled:
            return
        waited = f" ждали {event.waited} с" if event.waited else ""
        status = event.status if event.status is not None else "нет ответа"
        note = f" — {event.note}" if event.note else ""
        print(f"  [{event.method}] {status}{waited}{note}", file=sys.stderr)

    return log


def _connect(store_id: int, verbose: bool) -> tuple[Transport, str]:
    raw = _token_for(store_id)
    return Transport(raw, on_event=_verbose_logger(verbose)), raw


def cmd_registry(args: argparse.Namespace) -> int:
    print("Разрешённые методы (всё остальное транспорт отклоняет до отправки):\n")
    width = max(len(m.name) for m in registry.METHODS)
    for method in registry.METHODS:
        if method.path == "/ping" and not args.all:
            continue
        mark = "документация" if method.source == "doc" else "практика, требует проверки"
        print(f"  {method.name:<{width}}  {method.http:<4} {method.url}")
        print(f"  {'':<{width}}  источник сигнатуры: {mark}")
    print(
        f"\nПишущих методов в реестре: 0. "
        f"Глаголы, доступные транспорту: {', '.join(sorted(registry.ALLOWED_HTTP))}."
    )
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    transport, raw = _connect(args.store, args.verbose)
    info = token_module.parse(raw)

    print(f"Токен {token_module.fingerprint(raw)}")
    if info.seller_id:
        print(f"  продавец: {info.seller_id}")
    if info.expires_at:
        left = info.days_left()
        print(f"  действует до {info.expires_at:%d.%m.%Y} (осталось дней: {left})")
    else:
        print("  срок действия в токене не указан")
    if info.sandbox is not None:
        print(f"  контур: {'тестовый' if info.sandbox else 'боевой'}")

    if args.offline:
        print("\n(проверка категорий пропущена: запуск без обращения к WB)")
        return 0

    print("\nПроверяю категории через /ping — это единственный документированный")
    print("способ узнать, что токену действительно разрешено:\n")
    report = sync.check_token(transport, raw)
    for name, available in report.categories.items():
        title = registry.CATEGORY_TITLES.get(name, name)
        print(f"  {'✓' if available else '×'} {title}")

    if report.problems:
        print("\nЧто требует внимания:")
        for problem in report.problems:
            print(f"  ! {problem}")
    else:
        print("\nПрепятствий для регулярной синхронизации не видно.")
    return 0 if report.usable_for_sync else 1


def cmd_pull(args: argparse.Namespace) -> int:
    transport, _ = _connect(args.store, args.verbose)
    since = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        if args.since
        else datetime.now(timezone.utc) - timedelta(days=args.days)
    )

    with Storage(args.db) as storage:
        runners = {
            "cards": lambda: sync.sync_cards(transport, storage, args.store),
            "stocks": lambda: sync.sync_stocks(
                transport, storage, args.store, date_from=since, nudge=args.nudge
            ),
            "orders": lambda: sync.sync_orders(
                transport, storage, args.store, date_from=since, nudge=args.nudge
            ),
            "sales": lambda: sync.sync_sales(
                transport, storage, args.store, date_from=since, nudge=args.nudge
            ),
            "realization": lambda: sync.sync_realization(
                transport, storage, args.store, window_days=args.window
            ),
        }
        result = runners[args.dataset]()

    print(
        f"{result.dataset}: строк {result.rows}, страниц {result.pages}"
    )
    for warning in result.warnings:
        print(f"  ! {warning}")
    if result.stopped_early:
        print(f"  остановлено: {result.stopped_early}")
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with Storage(args.db) as storage:
        print(f"Снимок: {storage.path}\n")
        for dataset in ("cards", "stocks", "orders", "sales", "realization"):
            count = storage.count(args.store, dataset)
            run = storage.last_run(args.store, dataset)
            when = run["started_at"] if run else "никогда"
            status = run["status"] if run else "—"
            print(f"  {dataset:<12} строк: {count:<8} последний прогон: {when} ({status})")
            if run and run["status"] == "failed" and run["note"]:
                print(f"  {'':<12} причина: {run['note']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wb-sync",
        description="Read-only коннектор Wildberries Seller API.",
    )
    parser.add_argument("--store", type=int, default=1, help="номер магазина")
    parser.add_argument("--db", default=DEFAULT_DB, help="файл снимка SQLite")
    parser.add_argument("-v", "--verbose", action="store_true", help="журнал запросов")
    subparsers = parser.add_subparsers(dest="command", required=True)

    registry_cmd = subparsers.add_parser(
        "registry", help="показать все разрешённые методы"
    )
    registry_cmd.add_argument("--all", action="store_true", help="включая /ping")
    registry_cmd.set_defaults(func=cmd_registry)

    token_cmd = subparsers.add_parser(
        "token", help="срок жизни токена и доступные ему категории"
    )
    token_cmd.add_argument(
        "--offline", action="store_true", help="только разбор JWT, без запросов к WB"
    )
    token_cmd.set_defaults(func=cmd_token)

    pull_cmd = subparsers.add_parser("pull", help="выгрузить набор данных")
    pull_cmd.add_argument(
        "dataset", choices=["cards", "stocks", "orders", "sales", "realization"]
    )
    pull_cmd.add_argument("--since", help="начальная дата, ISO")
    pull_cmd.add_argument("--days", type=int, default=1, help="за сколько дней назад")
    pull_cmd.add_argument(
        "--window", type=int, default=42, help="окно перечитывания отчёта реализации"
    )
    pull_cmd.add_argument(
        "--nudge",
        action="store_true",
        help="сдвигать застрявший курсор вручную (с риском пропустить строки)",
    )
    pull_cmd.set_defaults(func=cmd_pull)

    status_cmd = subparsers.add_parser("status", help="что лежит в снимке")
    status_cmd.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except WbError as exc:
        print(f"Ошибка WB: {exc}", file=sys.stderr)
        return 2
    except token_module.TokenError as exc:
        print(f"Токен не читается: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
