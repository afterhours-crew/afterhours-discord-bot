# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import discord
import pytest
from test_activity_service import NOW, MetadataMessage, guild_with, member, source

from afterhours_bot.activity_service import reconcile_active_role
from afterhours_bot.config import ActiveRoleSettings
from afterhours_bot.storage import ActivityStore

CONFIG = ActiveRoleSettings(99)


def test_next_pass_and_restart_fetch_only_after_saved_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, role = guild_with(person)
        messages = [MetadataMessage(i) for i in (1, 2, 3)]
        channel = source(guild, 10, messages)
        guild.channels = [channel]
        try:
            first = await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            assert first.messages == 3 and first.granted == 1
            person.roles = [role]
            messages.append(MetadataMessage(4, created_at=NOW + timedelta(minutes=30)))
            second = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=1)
            )
            assert second.messages == 1 and second.granted == second.removed == 0
            after = channel.history.call_args.kwargs["after"]
            assert after.id == discord.utils.time_snowflake(NOW, high=False) - 1
            await store.close()
            store = ActivityStore(store.path)
            third = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=2)
            )
            assert third.messages == 0 and third.qualified == 1
            assert channel.history.call_args.kwargs["after"].id == (
                discord.utils.time_snowflake(NOW + timedelta(hours=1), high=False) - 1
            )
            person.add_roles.assert_awaited_once()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_expiry_removes_role_without_refetching_old_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, role = guild_with(person)
        channel = source(
            guild,
            10,
            [MetadataMessage(i, created_at=NOW - timedelta(days=13, hours=23)) for i in (1, 2, 3)],
        )
        guild.channels = [channel]
        try:
            await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            person.roles = [role]
            result = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=2)
            )
            assert result.messages == 0 and result.removed == 1
            assert channel.history.call_args.kwargs["after"].id == (
                discord.utils.time_snowflake(NOW, high=False) - 1
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_deleted_counted_messages_keep_counting_without_history_rechecks(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, role = guild_with(person)
        messages = [MetadataMessage(i) for i in (1, 2, 3)]
        guild.channels = [source(guild, 10, messages)]
        try:
            await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            person.roles = [role]
            messages.clear()
            result = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=1)
            )
            assert result.messages == 0 and result.qualified == 1 and result.removed == 0
            person.remove_roles.assert_not_awaited()
        finally:
            await store.close()

    asyncio.run(scenario())


def test_partial_pass_resumes_completed_sources_and_retries_failed_source(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, _ = guild_with(person)
        first = source(guild, 10, [MetadataMessage(i) for i in (1, 2, 3)])
        failure = discord.HTTPException(Mock(status=500, reason="failure"), "detail")
        broken = source(guild, 11, error=failure)
        guild.channels = [first, broken]
        try:
            with pytest.raises(discord.HTTPException):
                await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            person.add_roles.assert_not_awaited()
            revision = await store.configure(guild.id, CONFIG, as_of=NOW)
            assert await store.position(guild.id, 10, revision, as_of=NOW) is not None
            assert await store.position(guild.id, 11, revision, as_of=NOW) is None
            fixed = source(guild, 11)
            guild.channels = [first, fixed]
            result = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=1)
            )
            assert result.messages == 0 and result.granted == 1
            assert first.history.call_args.kwargs["after"].id == (
                discord.utils.time_snowflake(NOW, high=False) - 1
            )
            assert fixed.history.call_args.kwargs["after"].id == discord.utils.time_snowflake(
                NOW + timedelta(hours=1) - timedelta(days=14), high=True
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_newly_discovered_thread_gets_one_initial_backfill(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, _ = guild_with(person)
        base = source(guild, 10)
        guild.channels = [base]
        thread = source(guild, 20, [MetadataMessage(i) for i in (1, 2, 3)], kind=discord.Thread)
        try:
            await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            guild.active_threads.return_value = [thread]
            result = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=1)
            )
            assert result.granted == 1 and result.messages == 3
            assert thread.history.call_args.kwargs["after"].id == discord.utils.time_snowflake(
                NOW + timedelta(hours=1) - timedelta(days=14), high=True
            )
        finally:
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["days", "minimum"])
def test_larger_window_or_minimum_backfills_once_then_resumes(tmp_path: Path, change: str) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, role = guild_with(person)
        channel = source(guild, 10, [MetadataMessage(i) for i in range(1, 6)])
        guild.channels = [channel]
        changed = ActiveRoleSettings(
            99,
            window_days=21 if change == "days" else 14,
            min_messages=5 if change == "minimum" else 3,
        )
        try:
            await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            person.roles = [role]
            result = await reconcile_active_role(
                guild, changed, store=store, as_of=NOW + timedelta(hours=1)
            )
            assert result.messages == 5 and result.qualified == 1
            assert channel.history.call_args.kwargs["after"].id == discord.utils.time_snowflake(
                NOW + timedelta(hours=1) - timedelta(days=changed.window_days), high=True
            )
            result = await reconcile_active_role(
                guild, changed, store=store, as_of=NOW + timedelta(hours=2)
            )
            assert result.messages == 0 and result.qualified == 1
        finally:
            await store.close()

    asyncio.run(scenario())


def test_visibility_changes_filter_cached_counts_and_preserve_cursors(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, role = guild_with(person)
        visible = source(guild, 10)
        changed = source(guild, 11, [MetadataMessage(i) for i in (1, 2, 3)])
        guild.channels = [visible, changed]
        try:
            await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            person.roles = [role]
            guild.channels = [visible]
            result = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=1)
            )
            assert result.removed == 1 and result.qualified == 0
            person.roles = []
            guild.channels = [visible, changed]
            result = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(hours=2)
            )
            assert result.granted == 1 and result.messages == 0
            assert changed.history.call_args.kwargs["after"].id == (
                discord.utils.time_snowflake(NOW, high=False) - 1
            )
        finally:
            await store.close()

    asyncio.run(scenario())


def test_current_millisecond_is_not_skipped_by_cursor_advancement(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ActivityStore(tmp_path / "state.sqlite3")
        person = member(1)
        guild, _ = guild_with(person)
        messages = [MetadataMessage(i, created_at=NOW) for i in (1, 2, 3)]
        guild.channels = [source(guild, 10, messages)]
        try:
            first = await reconcile_active_role(guild, CONFIG, store=store, as_of=NOW)
            assert first.messages == 0 and first.qualified == 0
            second = await reconcile_active_role(
                guild, CONFIG, store=store, as_of=NOW + timedelta(milliseconds=1)
            )
            assert second.messages == 3 and second.granted == 1
        finally:
            await store.close()

    asyncio.run(scenario())
