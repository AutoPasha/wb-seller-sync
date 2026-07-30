"""Снимок выгрузки в SQLite.

Два решения, которые здесь стоит объяснить, потому что они не очевидны.

**Строка хранится целиком.** WB добавляет поля в ответы без анонса, и выгрузка,
которая раскладывает ответ строго по колонкам, однажды тихо перестаёт видеть
новое поле. Поэтому в таблице лежат и разобранные ключевые поля (по ним ищут и
джойнят), и исходный JSON. Появилось поле — оно уже в базе, достать его можно
задним числом, повторная выгрузка за прошлый период не нужна.

**Публикация атомарна.** Прогон либо виден целиком, либо не виден вовсе:
всё пишется в одной транзакции. Иначе отчёт, построенный в момент падения
выгрузки, показал бы половину остатков — и выглядело бы это не как сбой,
а как правдоподобные цифры.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS rows (
    store_id   INTEGER NOT NULL,
    dataset    TEXT    NOT NULL,
    row_key    TEXT    NOT NULL,
    period     TEXT,
    payload    TEXT    NOT NULL,
    loaded_at  TEXT    NOT NULL,
    PRIMARY KEY (store_id, dataset, row_key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS rows_period
    ON rows (store_id, dataset, period);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id    INTEGER NOT NULL,
    dataset     TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    rows        INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS sync_runs_recent
    ON sync_runs (store_id, dataset, started_at DESC);

CREATE TABLE IF NOT EXISTS cursors (
    store_id  INTEGER NOT NULL,
    dataset   TEXT    NOT NULL,
    value     TEXT    NOT NULL,
    saved_at  TEXT    NOT NULL,
    PRIMARY KEY (store_id, dataset)
) WITHOUT ROWID;
"""

#: Как из строки ответа получается её идентичность. Ключ обязан быть
#: устойчивым между выгрузками, иначе перезагрузка окна раздвоит данные.
#: Поля выбраны по документации и практике; если поля в ответе нет — выгрузка
#: останавливается с внятной ошибкой, а не пишет мусор под ключом ``None``.
ROW_KEYS: dict[str, tuple[str, ...]] = {
    "cards": ("nmID",),
    "stocks": ("nmId", "barcode", "warehouseName"),
    "orders": ("srid",),
    "sales": ("srid",),
    "realization": ("rrd_id",),
}

#: Поле, по которому строка относится к периоду. Нужно скользящему окну:
#: перечитывая шесть недель отчёта реализации, старые строки этого периода
#: надо сначала убрать, иначе исправленная строка ляжет рядом с ошибочной.
PERIOD_FIELDS: dict[str, str] = {
    "realization": "rr_dt",
    "orders": "date",
    "sales": "date",
}


class StorageError(RuntimeError):
    pass


@dataclass
class RunStats:
    rows: int = 0
    requests: int = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_key(dataset: str, row: dict) -> str:
    fields = ROW_KEYS.get(dataset)
    if not fields:
        raise StorageError(f"для набора {dataset!r} не задан ключ строки")
    parts: list[str] = []
    for name in fields:
        value = row.get(name)
        if value is None or value == "":
            raise StorageError(
                f"в строке набора {dataset} нет ключевого поля {name!r}. "
                f"Скорее всего WB изменил формат ответа — загрузка остановлена, "
                f"чтобы не записать дубли под пустым ключом."
            )
        parts.append(str(value))
    return "|".join(parts)


def _period_of(dataset: str, row: dict) -> str | None:
    field = PERIOD_FIELDS.get(dataset)
    if not field:
        return None
    value = row.get(field)
    return str(value)[:10] if value else None


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- запись -------------------------------------------------------------

    @contextmanager
    def run(self, store_id: int, dataset: str) -> Iterator["RunWriter"]:
        """Один прогон выгрузки: либо публикуется целиком, либо не публикуется."""
        started = _utc_now()
        cursor = self._conn.execute(
            "INSERT INTO sync_runs (store_id, dataset, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (store_id, dataset, started),
        )
        run_id = cursor.lastrowid
        self._conn.commit()

        writer = RunWriter(self._conn, store_id, dataset)
        try:
            self._conn.execute("BEGIN")
            yield writer
        except BaseException as exc:
            self._conn.rollback()
            self._conn.execute(
                "UPDATE sync_runs SET finished_at=?, status='failed', note=?, "
                "rows=?, requests=? WHERE id=?",
                (_utc_now(), str(exc)[:500], writer.stats.rows,
                 writer.stats.requests, run_id),
            )
            self._conn.commit()
            raise
        self._conn.commit()
        self._conn.execute(
            "UPDATE sync_runs SET finished_at=?, status='ok', rows=?, requests=? "
            "WHERE id=?",
            (_utc_now(), writer.stats.rows, writer.stats.requests, run_id),
        )
        self._conn.commit()

    # -- чтение -------------------------------------------------------------

    def count(self, store_id: int, dataset: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM rows WHERE store_id=? AND dataset=?",
            (store_id, dataset),
        ).fetchone()
        return int(row["n"])

    def last_run(self, store_id: int, dataset: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sync_runs WHERE store_id=? AND dataset=? "
            "ORDER BY started_at DESC LIMIT 1",
            (store_id, dataset),
        ).fetchone()

    def cursor_value(self, store_id: int, dataset: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM cursors WHERE store_id=? AND dataset=?",
            (store_id, dataset),
        ).fetchone()
        return row["value"] if row else None

    def rows(self, store_id: int, dataset: str, limit: int = 20) -> list[dict]:
        found = self._conn.execute(
            "SELECT payload FROM rows WHERE store_id=? AND dataset=? LIMIT ?",
            (store_id, dataset, limit),
        ).fetchall()
        return [json.loads(row["payload"]) for row in found]


class RunWriter:
    """Пишет строки внутри открытой транзакции прогона."""

    def __init__(self, conn: sqlite3.Connection, store_id: int, dataset: str) -> None:
        self._conn = conn
        self._store_id = store_id
        self._dataset = dataset
        self.stats = RunStats()

    def drop_period(self, since: date, until: date) -> int:
        """Убирает строки периода перед его перечитыванием.

        Без этого исправленная задним числом строка отчёта о реализации легла
        бы рядом со старой версией, а не вместо неё.
        """
        cursor = self._conn.execute(
            "DELETE FROM rows WHERE store_id=? AND dataset=? "
            "AND period IS NOT NULL AND period >= ? AND period <= ?",
            (self._store_id, self._dataset, since.isoformat(), until.isoformat()),
        )
        return cursor.rowcount

    def replace_all(self) -> int:
        """Полная замена набора — для снимков вроде остатков и карточек."""
        cursor = self._conn.execute(
            "DELETE FROM rows WHERE store_id=? AND dataset=?",
            (self._store_id, self._dataset),
        )
        return cursor.rowcount

    def write(self, rows: Sequence[dict]) -> int:
        loaded_at = _utc_now()
        payload: Iterable[tuple] = (
            (
                self._store_id,
                self._dataset,
                row_key(self._dataset, row),
                _period_of(self._dataset, row),
                json.dumps(row, ensure_ascii=False),
                loaded_at,
            )
            for row in rows
        )
        self._conn.executemany(
            "INSERT INTO rows (store_id, dataset, row_key, period, payload, loaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (store_id, dataset, row_key) DO UPDATE SET"
            "   payload=excluded.payload, period=excluded.period,"
            "   loaded_at=excluded.loaded_at",
            payload,
        )
        self.stats.rows += len(rows)
        return len(rows)

    def count_request(self) -> None:
        self.stats.requests += 1

    def save_cursor(self, value: str | int) -> None:
        self._conn.execute(
            "INSERT INTO cursors (store_id, dataset, value, saved_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT (store_id, dataset) DO UPDATE SET"
            "   value=excluded.value, saved_at=excluded.saved_at",
            (self._store_id, self._dataset, str(value), _utc_now()),
        )
