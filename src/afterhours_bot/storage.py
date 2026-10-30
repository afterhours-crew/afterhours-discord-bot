# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Versioned SQLite configuration and bounded incremental activity state."""

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from afterhours_bot.activity import ActivityMessage
from afterhours_bot.config import ActiveRoleSettings

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DAY_MICROSECONDS = 86_400_000_000


class ActivityStateError(RuntimeError):
    """State is incompatible, incomplete or concurrently changed."""


def microseconds(value: datetime) -> int:
    if value.utcoffset() is None:
        raise ActivityStateError("Activity time must include a timezone.")
    return (value - EPOCH) // timedelta(microseconds=1)


def _identifier(value: int) -> str:
    if type(value) is not int or not 0 < value < 2**64:
        raise ActivityStateError("Invalid Discord identity in activity state.")
    return str(value)


@dataclass(frozen=True)
class SourcePosition:
    cursor_id: int
    scanned_at_us: int


class ActivityStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    async def _call[T](self, operation: Callable[[], T]) -> T:
        async with self._lock:
            task = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A cancelled await must not release the lock while its disk transaction is running.
                with suppress(Exception):
                    await task
                raise

    def _database(self) -> sqlite3.Connection:
        if self._closed:
            raise ActivityStateError("Activity store is closed.")
        if self._connection is not None:
            return self._connection
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            raise ActivityStateError("The activity database directory is not writable.") from None
        connection = sqlite3.connect(
            self.path, timeout=5, isolation_level=None, check_same_thread=False
        )
        try:
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ActivityStateError("Unsupported activity database schema version.")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=ON")
            if version == 0:
                with self._transaction(connection):
                    connection.execute(
                        "CREATE TABLE configurations (guild_id TEXT NOT NULL, key TEXT NOT NULL, "
                        "value_json TEXT NOT NULL, revision INTEGER NOT NULL, "
                        "updated_us INTEGER NOT NULL, "
                        "PRIMARY KEY (guild_id, key))"
                    )
                    connection.execute(
                        "CREATE TABLE scan_positions (guild_id TEXT NOT NULL, "
                        "source_id TEXT NOT NULL, "
                        "cursor_id TEXT NOT NULL, scanned_at_us INTEGER NOT NULL, "
                        "PRIMARY KEY (guild_id, source_id))"
                    )
                    connection.execute(
                        "CREATE TABLE activity (guild_id TEXT NOT NULL, source_id TEXT NOT NULL, "
                        "message_id TEXT NOT NULL, author_id TEXT NOT NULL, "
                        "created_us INTEGER NOT NULL, "
                        "expires_us INTEGER NOT NULL, PRIMARY KEY (guild_id, message_id))"
                    )
                    connection.execute(
                        "CREATE INDEX activity_recent ON activity "
                        "(guild_id, source_id, author_id, created_us DESC, message_id DESC)"
                    )
                    connection.execute("CREATE INDEX activity_expiry ON activity (expires_us)")
                    connection.execute("PRAGMA user_version=1")
            try:
                self.path.chmod(0o600)
            except OSError:
                raise ActivityStateError(
                    "The activity database permissions could not be set."
                ) from None
        except BaseException:
            connection.close()
            raise
        self._connection = connection
        return connection

    @staticmethod
    @contextmanager
    def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _trim_source(connection: sqlite3.Connection, guild: str, source: str, minimum: int) -> None:
        connection.execute(
            "DELETE FROM activity WHERE rowid IN ("
            "SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER ("
            "PARTITION BY author_id ORDER BY created_us DESC, message_id DESC) AS rank "
            "FROM activity WHERE guild_id=? AND source_id=?) WHERE rank>?)",
            (guild, source, minimum),
        )

    @staticmethod
    def _require_revision(connection: sqlite3.Connection, guild: str, revision: int) -> None:
        row = connection.execute(
            "SELECT revision FROM configurations WHERE guild_id=? AND key='active_role'", (guild,)
        ).fetchone()
        if row is None or int(row[0]) != revision:
            raise ActivityStateError("Activity configuration changed during the scan.")

    async def configure(
        self, guild_id: int, settings: ActiveRoleSettings | None, *, as_of: datetime
    ) -> int:
        guild, now = _identifier(guild_id), microseconds(as_of)
        value = (
            {
                "role_id": str(settings.role_id),
                "window_days": settings.window_days,
                "min_messages": settings.min_messages,
            }
            if settings is not None
            else None
        )
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))

        def operation() -> int:
            connection = self._database()
            with self._transaction(connection):
                row = connection.execute(
                    "SELECT value_json, revision FROM configurations "
                    "WHERE guild_id=? AND key='active_role'",
                    (guild,),
                ).fetchone()
                try:
                    previous = json.loads(row[0]) if row is not None else None
                    if previous is not None and (
                        not isinstance(previous, dict)
                        or type(previous.get("window_days")) is not int
                        or type(previous.get("min_messages")) is not int
                    ):
                        raise ValueError
                except ValueError, TypeError:
                    raise ActivityStateError("Saved activity configuration is invalid.") from None
                changed = row is None or row[0] != encoded
                revision = (int(row[1]) if row is not None else 0) + int(changed)
                if changed:
                    connection.execute(
                        "INSERT INTO configurations VALUES (?, 'active_role', ?, ?, ?) "
                        "ON CONFLICT(guild_id, key) DO UPDATE SET value_json=excluded.value_json, "
                        "revision=excluded.revision, updated_us=excluded.updated_us",
                        (guild, encoded, revision, now),
                    )
                if settings is None:
                    connection.execute("DELETE FROM activity WHERE guild_id=?", (guild,))
                    connection.execute("DELETE FROM scan_positions WHERE guild_id=?", (guild,))
                else:
                    # A larger window/threshold needs history that the smaller cache did not retain.
                    if previous is None or (
                        settings.window_days > previous["window_days"]
                        or settings.min_messages > previous["min_messages"]
                    ):
                        connection.execute("DELETE FROM scan_positions WHERE guild_id=?", (guild,))
                    if changed:
                        connection.execute(
                            "UPDATE activity SET expires_us=created_us+? WHERE guild_id=?",
                            (settings.window_days * DAY_MICROSECONDS, guild),
                        )
                        sources = connection.execute(
                            "SELECT DISTINCT source_id FROM activity WHERE guild_id=?", (guild,)
                        ).fetchall()
                        for source in sources:
                            self._trim_source(connection, guild, source[0], settings.min_messages)
                    cutoff = now - settings.window_days * DAY_MICROSECONDS
                    connection.execute(
                        "DELETE FROM scan_positions WHERE guild_id=? AND scanned_at_us<?",
                        (guild, cutoff),
                    )
                connection.execute("DELETE FROM activity WHERE expires_us<=?", (now,))
                return revision

        return await self._call(operation)

    async def position(
        self, guild_id: int, source_id: int, revision: int, *, as_of: datetime
    ) -> SourcePosition | None:
        guild, source, now = _identifier(guild_id), _identifier(source_id), microseconds(as_of)

        def operation() -> SourcePosition | None:
            connection = self._database()
            self._require_revision(connection, guild, revision)
            row = connection.execute(
                "SELECT cursor_id, scanned_at_us FROM scan_positions "
                "WHERE guild_id=? AND source_id=?",
                (guild, source),
            ).fetchone()
            if row is None:
                return None
            if row[1] > now:
                raise ActivityStateError("Activity clock is older than the saved scan position.")
            return SourcePosition(int(row[0]), int(row[1]))

        return await self._call(operation)

    async def commit_source(
        self,
        guild_id: int,
        source_id: int,
        settings: ActiveRoleSettings,
        revision: int,
        *,
        expected: SourcePosition | None,
        cursor_id: int,
        as_of: datetime,
        messages: Sequence[ActivityMessage],
        max_rows: int,
    ) -> None:
        guild, source, now = _identifier(guild_id), _identifier(source_id), microseconds(as_of)
        cursor = _identifier(cursor_id)
        cutoff = now - settings.window_days * DAY_MICROSECONDS
        batch = tuple(messages)

        def operation() -> None:
            connection = self._database()
            rows = [
                (
                    guild,
                    source,
                    _identifier(message.message_id).zfill(20),
                    _identifier(message.author_id),
                    microseconds(message.created_at),
                    microseconds(message.created_at) + settings.window_days * DAY_MICROSECONDS,
                )
                for message in batch
                if not message.is_bot
                and not message.is_webhook
                and message.is_user_message
                and cutoff < microseconds(message.created_at) <= now
            ]
            with self._transaction(connection):
                self._require_revision(connection, guild, revision)
                row = connection.execute(
                    "SELECT cursor_id, scanned_at_us FROM scan_positions "
                    "WHERE guild_id=? AND source_id=?",
                    (guild, source),
                ).fetchone()
                current = None if row is None else SourcePosition(int(row[0]), int(row[1]))
                if current != expected or (current is not None and cursor_id < current.cursor_id):
                    raise ActivityStateError("Activity scan position changed during the scan.")
                connection.executemany(
                    "INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, message_id) DO NOTHING",
                    rows,
                )
                self._trim_source(connection, guild, source, settings.min_messages)
                connection.execute("DELETE FROM activity WHERE expires_us<=?", (now,))
                count = connection.execute(
                    "SELECT COUNT(*) FROM activity WHERE guild_id=?", (guild,)
                ).fetchone()[0]
                if count > max_rows:
                    raise ActivityStateError("Stored activity limit reached.")
                connection.execute(
                    "INSERT INTO scan_positions VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, source_id) DO UPDATE SET "
                    "cursor_id=excluded.cursor_id, scanned_at_us=excluded.scanned_at_us",
                    (guild, source, cursor, now),
                )

        await self._call(operation)

    async def qualified(
        self,
        guild_id: int,
        settings: ActiveRoleSettings,
        revision: int,
        *,
        source_ids: Sequence[int],
        member_ids: Sequence[int],
        as_of: datetime,
    ) -> frozenset[int]:
        guild, now = _identifier(guild_id), microseconds(as_of)
        sources = [(_identifier(value),) for value in source_ids]
        members = [(_identifier(value),) for value in member_ids]

        def operation() -> frozenset[int]:
            connection = self._database()
            with self._transaction(connection):
                self._require_revision(connection, guild, revision)
                connection.execute("DELETE FROM activity WHERE expires_us<=?", (now,))
                connection.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS selected_sources (id TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS selected_members (id TEXT PRIMARY KEY)"
                )
                connection.execute("DELETE FROM selected_sources")
                connection.execute("DELETE FROM selected_members")
                connection.executemany("INSERT INTO selected_sources VALUES (?)", sources)
                connection.executemany("INSERT INTO selected_members VALUES (?)", members)
                rows = connection.execute(
                    "SELECT a.author_id FROM activity a "
                    "JOIN selected_sources s ON s.id=a.source_id "
                    "JOIN selected_members m ON m.id=a.author_id "
                    "WHERE a.guild_id=? AND a.created_us>? AND a.created_us<=? "
                    "GROUP BY a.author_id HAVING COUNT(*)>=?",
                    (
                        guild,
                        now - settings.window_days * DAY_MICROSECONDS,
                        now,
                        settings.min_messages,
                    ),
                ).fetchall()
                connection.execute("DELETE FROM selected_sources")
                connection.execute("DELETE FROM selected_members")
                return frozenset(int(row[0]) for row in rows)

        return await self._call(operation)

    async def close(self) -> None:
        def operation() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._closed = True

        await self._call(operation)
