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
from afterhours_bot.filters import Flag
from afterhours_bot.rules import RuleSet

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


@dataclass(frozen=True)
class ActiveRules:
    text: str
    sha256: str
    revision: int
    source: str
    file_sha256: str


@dataclass(frozen=True)
class ReviewDecision:
    flags: int
    decision: str
    decider_id: int
    already_decided: bool
    message_id: int = 0
    channel_id: int = 0
    author_id: int = 0
    rules: tuple[int, ...] = ()
    created_us: int = 0
    outcome: str | None = None


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
            if version not in (0, 1, 2, 3):
                raise ActivityStateError("Unsupported activity database schema version.")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=ON")
            if version == 2:
                with self._transaction(connection):
                    connection.execute("ALTER TABLE review_flags ADD COLUMN outcome TEXT")
                    connection.execute("PRAGMA user_version=3")
            if version < 2:
                with self._transaction(connection):
                    connection.execute(
                        "CREATE TABLE review_flags (guild_id TEXT NOT NULL, "
                        "message_id TEXT NOT NULL, source TEXT NOT NULL, "
                        "channel_id TEXT NOT NULL, author_id TEXT NOT NULL, "
                        "rule INTEGER NOT NULL, score REAL NOT NULL, reason TEXT NOT NULL, "
                        "rules_revision INTEGER, post_message_id TEXT, "
                        "created_us INTEGER NOT NULL, expires_us INTEGER NOT NULL, "
                        "decision TEXT, decider_id TEXT, decided_us INTEGER, outcome TEXT, "
                        "PRIMARY KEY (guild_id, message_id, source))"
                    )
                    connection.execute(
                        "CREATE INDEX review_flags_post ON review_flags (guild_id, post_message_id)"
                    )
                    connection.execute(
                        "CREATE INDEX review_flags_expiry ON review_flags (expires_us)"
                    )
                    connection.execute(
                        "CREATE INDEX review_flags_age ON review_flags (guild_id, created_us)"
                    )
                    connection.execute("PRAGMA user_version=3")
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
                    connection.execute("PRAGMA user_version=3")
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
                latest = connection.execute(
                    "SELECT MAX(scanned_at_us) FROM scan_positions WHERE guild_id=?", (guild,)
                ).fetchone()[0]
                if settings is not None and latest is not None and latest > now:
                    raise ActivityStateError("Activity clock is older than saved scan state.")
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

    async def configure_rules(
        self,
        guild_id: int,
        rules: RuleSet,
        *,
        as_of: datetime,
        source: str = "file",
        file_sha256: str | None = None,
    ) -> int:
        """Save the community rule text; the revision changes only with the content."""
        guild, now = _identifier(guild_id), microseconds(as_of)
        if source not in ("file", "upload"):
            raise ActivityStateError("Unknown rules source.")
        encoded = json.dumps(
            {
                "sha256": rules.sha256,
                "text": rules.text,
                "source": source,
                "file_sha256": file_sha256 or rules.sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        def operation() -> int:
            connection = self._database()
            with self._transaction(connection):
                row = connection.execute(
                    "SELECT value_json, revision FROM configurations "
                    "WHERE guild_id=? AND key='moderation_rules'",
                    (guild,),
                ).fetchone()
                previous = self._saved_rules(row[0]) if row is not None else None
                changed = previous is None or previous["sha256"] != rules.sha256
                revision = (int(row[1]) if row is not None else 0) + int(changed)
                if changed or (previous is not None and previous.get("source") != source):
                    connection.execute(
                        "INSERT INTO configurations VALUES (?, 'moderation_rules', ?, ?, ?) "
                        "ON CONFLICT(guild_id, key) DO UPDATE SET value_json=excluded.value_json, "
                        "revision=excluded.revision, updated_us=excluded.updated_us",
                        (guild, encoded, revision, now),
                    )
                return revision

        return await self._call(operation)

    @staticmethod
    def _saved_rules(raw: str) -> dict[str, str]:
        try:
            saved = json.loads(raw)
            if (
                not isinstance(saved, dict)
                or type(saved.get("sha256")) is not str
                or type(saved.get("text")) is not str
            ):
                raise ValueError
        except ValueError, TypeError:
            raise ActivityStateError("Saved moderation rules are invalid.") from None
        return {key: value for key, value in saved.items() if isinstance(value, str)}

    async def active_rules(self, guild_id: int) -> ActiveRules | None:
        """The saved rule text with its revision and where it came from."""
        guild = _identifier(guild_id)

        def operation() -> ActiveRules | None:
            connection = self._database()
            row = connection.execute(
                "SELECT value_json, revision FROM configurations "
                "WHERE guild_id=? AND key='moderation_rules'",
                (guild,),
            ).fetchone()
            if row is None:
                return None
            saved = self._saved_rules(row[0])
            return ActiveRules(
                text=saved["text"],
                sha256=saved["sha256"],
                revision=int(row[1]),
                source=saved.get("source", "file"),
                file_sha256=saved.get("file_sha256", saved["sha256"]),
            )

        return await self._call(operation)

    async def rules_revision(self, guild_id: int) -> tuple[int, str] | None:
        """The saved rule revision and digest, or None before the first configuration."""
        guild = _identifier(guild_id)

        def operation() -> tuple[int, str] | None:
            connection = self._database()
            row = connection.execute(
                "SELECT value_json, revision FROM configurations "
                "WHERE guild_id=? AND key='moderation_rules'",
                (guild,),
            ).fetchone()
            if row is None:
                return None
            try:
                saved = json.loads(row[0])
                digest = saved["sha256"]
                if type(digest) is not str:
                    raise ValueError
            except ValueError, TypeError, KeyError:
                raise ActivityStateError("Saved moderation rules are invalid.") from None
            return int(row[1]), digest

        return await self._call(operation)

    async def record_flags(
        self,
        guild_id: int,
        *,
        message_id: int,
        channel_id: int,
        author_id: int,
        flags: Sequence[Flag],
        rules_revision: int | None,
        post_message_id: int | None,
        as_of: datetime,
        retention_days: int,
        max_rows: int,
    ) -> None:
        """Audit the flags for one message. Only identifiers and configured reasons are kept."""
        guild, now = _identifier(guild_id), microseconds(as_of)
        message, channel, author = (
            _identifier(message_id),
            _identifier(channel_id),
            _identifier(author_id),
        )
        post = _identifier(post_message_id) if post_message_id is not None else None
        rows = [
            (
                guild,
                message,
                flag.source,
                channel,
                author,
                flag.rule,
                flag.score,
                flag.reason,
                rules_revision,
                post,
                now,
                now + retention_days * DAY_MICROSECONDS,
            )
            for flag in flags
        ]

        def operation() -> None:
            connection = self._database()
            with self._transaction(connection):
                connection.executemany(
                    "INSERT INTO review_flags (guild_id, message_id, source, channel_id, "
                    "author_id, rule, score, reason, rules_revision, post_message_id, "
                    "created_us, expires_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(guild_id, message_id, source) DO UPDATE SET "
                    "post_message_id=COALESCE(excluded.post_message_id, post_message_id)",
                    rows,
                )
                self._prune_flags(connection, guild, now, max_rows)

        await self._call(operation)

    @staticmethod
    def _prune_flags(connection: sqlite3.Connection, guild: str, now: int, max_rows: int) -> None:
        connection.execute("DELETE FROM review_flags WHERE expires_us<=?", (now,))
        count = connection.execute(
            "SELECT COUNT(*) FROM review_flags WHERE guild_id=?", (guild,)
        ).fetchone()[0]
        if count > max_rows:
            connection.execute(
                "DELETE FROM review_flags WHERE rowid IN (SELECT rowid FROM review_flags "
                "WHERE guild_id=? ORDER BY created_us ASC, rowid ASC LIMIT ?)",
                (guild, count - max_rows),
            )

    async def record_decision(
        self,
        guild_id: int,
        post_message_id: int,
        *,
        decision: str,
        decider_id: int,
        as_of: datetime,
    ) -> ReviewDecision | None:
        """Record one staff decision for a review post; an earlier decision is kept."""
        guild, post, decider, now = (
            _identifier(guild_id),
            _identifier(post_message_id),
            _identifier(decider_id),
            microseconds(as_of),
        )
        if not decision or len(decision) > 32 or not decision.isascii():
            raise ActivityStateError("Invalid review decision.")

        def operation() -> ReviewDecision | None:
            connection = self._database()
            with self._transaction(connection):
                rows = connection.execute(
                    "SELECT decision, decider_id, message_id, channel_id, author_id, rule, "
                    "created_us, outcome FROM review_flags "
                    "WHERE guild_id=? AND post_message_id=? ORDER BY rule",
                    (guild, post),
                ).fetchall()
                if not rows:
                    return None
                first = rows[0]
                message_id = int(first["message_id"])
                channel_id = int(first["channel_id"])
                author_id = int(first["author_id"])
                rules = tuple(sorted({int(row["rule"]) for row in rows}))
                created_us = min(int(row["created_us"]) for row in rows)
                settled = [row for row in rows if row["decision"] is not None]
                if settled:
                    outcome = settled[0]["outcome"]
                    return ReviewDecision(
                        flags=len(rows),
                        decision=str(settled[0]["decision"]),
                        decider_id=int(settled[0]["decider_id"]),
                        already_decided=True,
                        message_id=message_id,
                        channel_id=channel_id,
                        author_id=author_id,
                        rules=rules,
                        created_us=created_us,
                        outcome=str(outcome) if outcome is not None else None,
                    )
                connection.execute(
                    "UPDATE review_flags SET decision=?, decider_id=?, decided_us=? "
                    "WHERE guild_id=? AND post_message_id=?",
                    (decision, decider, now, guild, post),
                )
                return ReviewDecision(
                    flags=len(rows),
                    decision=decision,
                    decider_id=decider_id,
                    already_decided=False,
                    message_id=message_id,
                    channel_id=channel_id,
                    author_id=author_id,
                    rules=rules,
                    created_us=created_us,
                )

        return await self._call(operation)

    async def message_flags(self, guild_id: int, message_id: int) -> tuple[int, int | None]:
        """How many flags a message already has and the review post they share, if any."""
        guild, message = _identifier(guild_id), _identifier(message_id)

        def operation() -> tuple[int, int | None]:
            connection = self._database()
            row = connection.execute(
                "SELECT COUNT(*), MAX(post_message_id) FROM review_flags "
                "WHERE guild_id=? AND message_id=?",
                (guild, message),
            ).fetchone()
            post = row[1]
            return int(row[0]), int(post) if post is not None else None

        return await self._call(operation)

    async def record_auto_decision(
        self,
        guild_id: int,
        message_id: int,
        *,
        decision: str,
        decider_id: int,
        outcome: str,
        as_of: datetime,
    ) -> int:
        """Mark every flag of a message as decided by the bot itself; returns rows updated."""
        guild, message, decider, now = (
            _identifier(guild_id),
            _identifier(message_id),
            _identifier(decider_id),
            microseconds(as_of),
        )
        if not decision or len(decision) > 32 or not outcome or len(outcome) > 200:
            raise ActivityStateError("Invalid automatic decision.")

        def operation() -> int:
            connection = self._database()
            with self._transaction(connection):
                cursor = connection.execute(
                    "UPDATE review_flags SET decision=?, decider_id=?, decided_us=?, outcome=? "
                    "WHERE guild_id=? AND message_id=? AND decision IS NULL",
                    (decision, decider, now, outcome, guild, message),
                )
                return int(cursor.rowcount)

        return await self._call(operation)

    async def record_outcome(self, guild_id: int, post_message_id: int, outcome: str) -> int:
        """Store what happened after a decision; returns the number of rows updated."""
        guild, post = _identifier(guild_id), _identifier(post_message_id)
        if not outcome or len(outcome) > 200:
            raise ActivityStateError("Invalid review outcome.")

        def operation() -> int:
            connection = self._database()
            with self._transaction(connection):
                cursor = connection.execute(
                    "UPDATE review_flags SET outcome=? WHERE guild_id=? AND post_message_id=? "
                    "AND decision IS NOT NULL",
                    (outcome, guild, post),
                )
                return int(cursor.rowcount)

        return await self._call(operation)

    async def flag_summary(self, guild_id: int) -> tuple[int, int]:
        """Stored flags and how many of them are still undecided."""
        guild = _identifier(guild_id)

        def operation() -> tuple[int, int]:
            connection = self._database()
            row = connection.execute(
                "SELECT COUNT(*), SUM(decision IS NULL) FROM review_flags WHERE guild_id=?",
                (guild,),
            ).fetchone()
            return int(row[0]), int(row[1] or 0)

        return await self._call(operation)

    async def close(self) -> None:
        def operation() -> None:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._closed = True

        await self._call(operation)
