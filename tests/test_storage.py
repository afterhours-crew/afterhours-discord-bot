# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from afterhours_bot.activity import ActivityMessage
from afterhours_bot.config import ActiveRoleSettings
from afterhours_bot.storage import ActivityStateError, ActivityStore

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
CONFIG = ActiveRoleSettings(99)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ActivityStore]:
    instance = ActivityStore(tmp_path / "nightshift.sqlite3")
    yield instance
    asyncio.run(instance.close())


async def commit(
    store: ActivityStore,
    messages: list[ActivityMessage],
    *,
    config: ActiveRoleSettings = CONFIG,
    guild: int = 123,
    source: int = 10,
    cursor: int = 1000,
    as_of: datetime = NOW,
    max_rows: int = 100,
) -> int:
    revision = await store.configure(guild, config, as_of=as_of)
    position = await store.position(guild, source, revision, as_of=as_of)
    await store.commit_source(
        guild,
        source,
        config,
        revision,
        expected=position,
        cursor_id=cursor,
        as_of=as_of,
        messages=messages,
        max_rows=max_rows,
    )
    return revision


async def qualified(
    store: ActivityStore,
    revision: int,
    *,
    config: ActiveRoleSettings = CONFIG,
    guild: int = 123,
    sources: tuple[int, ...] = (10,),
    as_of: datetime = NOW,
) -> frozenset[int]:
    return await store.qualified(
        guild, config, revision, source_ids=sources, member_ids=(1, 2), as_of=as_of
    )


def row_count(store: ActivityStore) -> int:
    with sqlite3.connect(store.path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM activity").fetchone()[0])


def test_duplicate_messages_count_once_and_three_are_required(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(store, [ActivityMessage(i, 1, NOW) for i in (1, 1, 2)])
        assert not await qualified(store, revision)
        await commit(store, [ActivityMessage(3, 1, NOW)], cursor=1001)
        assert await qualified(store, revision) == frozenset({1})
        assert row_count(store) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "offset,qualifies",
    [
        (timedelta(days=-14, microseconds=-1), False),
        (timedelta(days=-14), False),
        (timedelta(days=-14, microseconds=1), True),
        (timedelta(), True),
        (timedelta(microseconds=1), False),
    ],
)
def test_persistent_window_boundaries(
    store: ActivityStore, offset: timedelta, qualifies: bool
) -> None:
    async def scenario() -> None:
        config = ActiveRoleSettings(99, min_messages=1)
        revision = await commit(store, [ActivityMessage(1, 1, NOW + offset)], config=config)
        assert (1 in await qualified(store, revision, config=config)) is qualifies

    asyncio.run(scenario())


@pytest.mark.parametrize("flag", ["bot", "webhook", "system"])
def test_storage_excludes_nonhuman_activity(store: ActivityStore, flag: str) -> None:
    async def scenario() -> None:
        config = ActiveRoleSettings(99, min_messages=1)
        revision = await commit(
            store,
            [
                ActivityMessage(
                    1,
                    1,
                    NOW,
                    is_bot=flag == "bot",
                    is_webhook=flag == "webhook",
                    is_user_message=flag != "system",
                )
            ],
            config=config,
        )
        assert not await qualified(store, revision, config=config)
        assert row_count(store) == 0

    asyncio.run(scenario())


def test_keeps_only_latest_minimum_per_member_and_source(store: ActivityStore) -> None:
    async def scenario() -> None:
        messages = [ActivityMessage(i, 1, NOW - timedelta(minutes=i)) for i in range(1, 11)]
        revision = await commit(store, messages)
        await commit(store, [ActivityMessage(i, 1, NOW) for i in (20, 21, 22)], source=11)
        assert row_count(store) == 6
        assert await qualified(store, revision, sources=(10,)) == frozenset({1})
        assert await qualified(store, revision, sources=(11,)) == frozenset({1})
        with sqlite3.connect(store.path) as connection:
            ids = connection.execute(
                "SELECT message_id FROM activity WHERE source_id='10' ORDER BY message_id"
            ).fetchall()
        assert [int(row[0]) for row in ids] == [1, 2, 3]

    asyncio.run(scenario())


def test_restart_preserves_cursor_counts_and_nonsecret_configuration(store: ActivityStore) -> None:
    async def scenario() -> None:
        config = ActiveRoleSettings(2**64 - 1)
        revision = await commit(
            store, [ActivityMessage(i, 1, NOW) for i in (1, 2, 3)], config=config
        )
        await store.close()
        reopened = ActivityStore(store.path)
        try:
            assert await qualified(reopened, revision, config=config) == frozenset({1})
            position = await reopened.position(123, 10, revision, as_of=NOW)
            assert position is not None and position.cursor_id == 1000
            with sqlite3.connect(store.path) as connection:
                raw = connection.execute("SELECT value_json FROM configurations").fetchone()[0]
                assert json.loads(raw) == {
                    "role_id": str(2**64 - 1),
                    "window_days": 14,
                    "min_messages": 3,
                }
                assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
                columns = {row[1] for row in connection.execute("PRAGMA table_info(activity)")}
                assert columns == {
                    "guild_id",
                    "source_id",
                    "message_id",
                    "author_id",
                    "created_us",
                    "expires_us",
                }
        finally:
            await reopened.close()

    asyncio.run(scenario())


def test_expiry_does_not_require_history_or_new_messages(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(
            store, [ActivityMessage(i, 1, NOW - timedelta(days=13, hours=23)) for i in (1, 2, 3)]
        )
        assert await qualified(store, revision) == frozenset({1})
        assert not await qualified(store, revision, as_of=NOW + timedelta(hours=2))
        assert row_count(store) == 0

    asyncio.run(scenario())


def test_invisible_sources_and_other_guilds_never_contribute(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(store, [ActivityMessage(i, 1, NOW) for i in (1, 2, 3)])
        assert not await qualified(store, revision, sources=(20,))
        other_revision = await store.configure(456, CONFIG, as_of=NOW)
        assert not await qualified(store, other_revision, guild=456)
        assert await qualified(store, revision) == frozenset({1})

    asyncio.run(scenario())


def test_limit_failure_rolls_back_both_metadata_and_cursor(store: ActivityStore) -> None:
    async def scenario() -> None:
        with pytest.raises(ActivityStateError, match="limit"):
            await commit(store, [ActivityMessage(i, 1, NOW) for i in (1, 2, 3)], max_rows=2)
        revision = await store.configure(123, CONFIG, as_of=NOW)
        assert await store.position(123, 10, revision, as_of=NOW) is None
        assert row_count(store) == 0
        await commit(store, [ActivityMessage(i, 1, NOW) for i in (1, 2, 3)])
        assert await qualified(store, revision) == frozenset({1})

    asyncio.run(scenario())


def test_stale_scan_cannot_overwrite_another_cursor(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(store, [])
        with pytest.raises(ActivityStateError, match="position changed"):
            await store.commit_source(
                123,
                10,
                CONFIG,
                revision,
                expected=None,
                cursor_id=1001,
                as_of=NOW,
                messages=[ActivityMessage(1, 1, NOW)],
                max_rows=100,
            )
        assert row_count(store) == 0
        position = await store.position(123, 10, revision, as_of=NOW)
        assert position is not None and position.cursor_id == 1000

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["days", "minimum"])
def test_increasing_requirements_invalidates_cursors(store: ActivityStore, change: str) -> None:
    async def scenario() -> None:
        await commit(store, [])
        config = ActiveRoleSettings(
            99,
            window_days=21 if change == "days" else 14,
            min_messages=5 if change == "minimum" else 3,
        )
        revision = await store.configure(123, config, as_of=NOW)
        assert await store.position(123, 10, revision, as_of=NOW) is None

    asyncio.run(scenario())


def test_decreasing_requirements_keeps_cursor_and_prunes_cache(store: ActivityStore) -> None:
    async def scenario() -> None:
        await commit(store, [ActivityMessage(i, 1, NOW) for i in (1, 2, 3)])
        config = ActiveRoleSettings(99, window_days=7, min_messages=2)
        revision = await store.configure(123, config, as_of=NOW)
        position = await store.position(123, 10, revision, as_of=NOW)
        assert position is not None and position.cursor_id == 1000
        assert row_count(store) == 2
        assert await qualified(store, revision, config=config) == frozenset({1})

    asyncio.run(scenario())


def test_disable_clears_activity_but_preserves_other_configurations(store: ActivityStore) -> None:
    async def scenario() -> None:
        await commit(store, [ActivityMessage(1, 1, NOW)])
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "INSERT INTO configurations VALUES ('123', 'future_feature', '{}', 1, 0)"
            )
        revision = await store.configure(123, None, as_of=NOW)
        assert row_count(store) == 0
        assert await store.position(123, 10, revision, as_of=NOW) is None
        with sqlite3.connect(store.path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM configurations WHERE key='future_feature'"
                ).fetchone()[0]
                == 1
            )

    asyncio.run(scenario())


def test_configuration_change_rejects_inflight_commit(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await store.configure(123, CONFIG, as_of=NOW)
        await store.configure(123, ActiveRoleSettings(100), as_of=NOW)
        with pytest.raises(ActivityStateError, match="configuration changed"):
            await store.commit_source(
                123,
                10,
                CONFIG,
                revision,
                expected=None,
                cursor_id=1000,
                as_of=NOW,
                messages=[],
                max_rows=100,
            )

    asyncio.run(scenario())


def test_clock_regression_does_not_reset_saved_cursor(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(store, [])
        with pytest.raises(ActivityStateError, match="clock"):
            await store.position(123, 10, revision, as_of=NOW - timedelta(seconds=1))

    asyncio.run(scenario())


def test_configuration_backfill_cannot_hide_clock_regression(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(store, [ActivityMessage(1, 1, NOW)])
        with pytest.raises(ActivityStateError, match="clock"):
            await store.configure(
                123,
                ActiveRoleSettings(99, window_days=21),
                as_of=NOW - timedelta(seconds=1),
            )
        position = await store.position(123, 10, revision, as_of=NOW)
        assert position is not None and position.cursor_id == 1000
        assert row_count(store) == 1

    asyncio.run(scenario())


def test_unknown_schema_is_rejected_without_rewriting_it(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    instance = ActivityStore(path)

    async def scenario() -> None:
        try:
            with pytest.raises(ActivityStateError, match="schema"):
                await instance.configure(123, CONFIG, as_of=NOW)
        finally:
            await instance.close()

    asyncio.run(scenario())
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_cancelled_disk_work_finishes_before_another_operation(store: ActivityStore) -> None:
    async def scenario() -> None:
        await store.configure(123, CONFIG, as_of=NOW)
        started, finish = threading.Event(), threading.Event()

        def write() -> None:
            connection = store._database()
            with store._transaction(connection):
                connection.execute(
                    "INSERT INTO configurations VALUES ('123', 'marker', '{}', 1, 0)"
                )
                started.set()
                assert finish.wait(timeout=2)

        task = asyncio.create_task(store._call(write))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        next_operation = asyncio.create_task(store.configure(123, CONFIG, as_of=NOW))
        try:
            await asyncio.sleep(0.01)
            assert not task.done() and not next_operation.done()
        finally:
            finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await next_operation
        with sqlite3.connect(store.path) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM configurations WHERE key='marker'"
                ).fetchone()[0]
                == 1
            )

    asyncio.run(scenario())


def test_qualification_does_not_retain_the_full_member_list(store: ActivityStore) -> None:
    async def scenario() -> None:
        revision = await commit(store, [])
        await qualified(store, revision)

        def counts() -> tuple[int, int]:
            connection = store._database()
            return (
                connection.execute("SELECT COUNT(*) FROM selected_sources").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM selected_members").fetchone()[0],
            )

        assert await store._call(counts) == (0, 0)

    asyncio.run(scenario())


def test_invalid_database_directory_has_sanitized_error(tmp_path: Path) -> None:
    parent = tmp_path / "existing-file"
    parent.write_text("fixture", encoding="utf-8")
    store = ActivityStore(parent / "state.sqlite3")

    async def scenario() -> None:
        try:
            with pytest.raises(ActivityStateError, match="directory is not writable"):
                await store.configure(123, CONFIG, as_of=NOW)
        finally:
            await store.close()

    asyncio.run(scenario())
