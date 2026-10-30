# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from afterhours_bot.activity_service import (
    ActivityRoleUnavailable,
    IncompleteActivityScan,
    ScanLimits,
    reconcile_active_role,
)
from afterhours_bot.config import ActiveRoleSettings

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)
CONFIG = ActiveRoleSettings(99)


async def stream[T](items: Iterable[T], error: Exception | None = None) -> AsyncIterator[T]:
    for item in items:
        yield item
    if error is not None:
        raise error


class MetadataMessage:
    def __init__(
        self,
        message_id: int,
        author_id: int = 1,
        *,
        bot: bool = False,
        webhook: bool = False,
        message_type: discord.MessageType = discord.MessageType.default,
        created_at: datetime = NOW - timedelta(hours=1),
    ) -> None:
        self.id = message_id
        self.author = SimpleNamespace(id=author_id, bot=bot)
        self.webhook_id = 123 if webhook else None
        self.type = message_type
        self.created_at = created_at

    @property
    def content(self) -> str:
        raise AssertionError("Message content must never be inspected.")


def member(member_id: int, *, role: Mock | None = None, bot: bool = False) -> Mock:
    return Mock(
        spec=discord.Member,
        id=member_id,
        bot=bot,
        roles=[] if role is None else [role],
        add_roles=AsyncMock(),
        remove_roles=AsyncMock(),
    )


def guild_with(*members: Mock) -> tuple[Mock, Mock]:
    role = Mock(spec=discord.Role, id=99, is_assignable=Mock(return_value=True))
    guild = Mock(spec=discord.Guild, id=123, channels=[], unavailable=False)
    guild.me = Mock(guild_permissions=SimpleNamespace(manage_roles=True))
    guild.get_role.return_value = role
    guild.active_threads = AsyncMock(return_value=[])
    guild.fetch_channels = AsyncMock(side_effect=lambda: guild.channels)
    guild.fetch_members = Mock(side_effect=lambda **_: stream(members))
    return guild, role


def source(
    guild: Mock,
    source_id: int,
    messages: Iterable[MetadataMessage] = (),
    *,
    kind: type = discord.TextChannel,
    visible: bool = True,
    history: bool = True,
    manage_threads: bool = True,
    public: tuple[Mock, ...] = (),
    private: tuple[Mock, ...] = (),
    error: Exception | None = None,
) -> Mock:
    channel = Mock(spec=kind, id=source_id, guild=guild)
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=visible, read_message_history=history, manage_threads=manage_threads
    )
    channel.history = Mock(side_effect=lambda **_: stream(messages, error))
    channel.archived_threads = Mock(
        side_effect=lambda **kwargs: stream(private if kwargs.get("private") else public)
    )
    channel.is_news = Mock(return_value=False)
    return channel


def assert_no_updates(*members: Mock) -> None:
    for item in members:
        item.add_roles.assert_not_awaited()
        item.remove_roles.assert_not_awaited()


def test_complete_scan_grants_removes_and_preserves_other_roles() -> None:
    first, second, existing, absent = member(1), member(2), member(3), member(4)
    guild, role = guild_with(first, second, existing, absent)
    unrelated = Mock(spec=discord.Role, id=88)
    first.roles = [unrelated]
    second.roles = [role, unrelated]
    existing.roles = [role]
    guild.channels = [
        source(guild, 10, [MetadataMessage(1), MetadataMessage(2)]),
        source(
            guild,
            11,
            [MetadataMessage(3), *(MetadataMessage(i, 3) for i in range(4, 7))],
            kind=discord.VoiceChannel,
        ),
    ]
    result = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    first.add_roles.assert_awaited_once_with(
        role, reason="Nightshift activity qualification", atomic=True
    )
    second.remove_roles.assert_awaited_once_with(
        role, reason="Nightshift activity window expired", atomic=True
    )
    assert_no_updates(existing, absent)
    assert first.roles == [unrelated] and second.roles == [role, unrelated]
    assert (result.sources, result.granted, result.removed) == (2, 1, 1)
    for channel in guild.channels:
        channel.history.assert_called_once_with(
            limit=250_001, after=NOW - timedelta(days=14), before=NOW, oldest_first=False
        )


def test_active_archived_public_private_and_forum_threads_are_deduplicated() -> None:
    person = member(1)
    guild, _ = guild_with(person)
    active = source(guild, 20, [MetadataMessage(1)], kind=discord.Thread)
    archived = source(guild, 21, [MetadataMessage(2)], kind=discord.Thread)
    private = source(guild, 22, [MetadataMessage(3)], kind=discord.Thread)
    forum_thread = source(guild, 23, [MetadataMessage(4)], kind=discord.Thread)
    parent = source(guild, 10, public=(active, archived), private=(private,))
    forum = source(guild, 11, kind=discord.ForumChannel, public=(forum_thread,))
    guild.channels = [parent, forum]
    guild.active_threads.return_value = [active]
    result = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    person.add_roles.assert_awaited_once()
    assert result.sources == 5 and result.messages == 4
    active.history.assert_called_once()
    parent.archived_threads.assert_any_call(limit=5_001, private=True, joined=False)
    forum.archived_threads.assert_called_once_with(limit=5_001)


def test_joined_private_archives_are_used_without_manage_threads() -> None:
    person = member(1)
    guild, _ = guild_with(person)
    parent = source(guild, 10, manage_threads=False)
    guild.channels = [parent]
    asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    parent.archived_threads.assert_any_call(limit=5_001, private=True, joined=True)


def test_announcements_do_not_request_unsupported_private_archives() -> None:
    guild, _ = guild_with(member(1))
    parent = source(guild, 10)
    parent.is_news.return_value = True
    guild.channels = [parent]
    asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    parent.archived_threads.assert_called_once_with(limit=5_001)


def test_hidden_channels_are_out_of_scope() -> None:
    guild, _ = guild_with(member(1))
    hidden = source(guild, 10, visible=False, history=False)
    guild.channels = [hidden, source(guild, 11)]
    result = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert result.sources == 1
    hidden.history.assert_not_called()
    hidden.archived_threads.assert_not_called()


@pytest.mark.parametrize("kind", [discord.VoiceChannel, discord.StageChannel])
def test_voice_and_stage_channel_text_chats_count(kind: type) -> None:
    person = member(1)
    guild, _ = guild_with(person)
    guild.channels = [source(guild, 10, [MetadataMessage(i) for i in range(1, 4)], kind=kind)]
    result = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert result.granted == 1
    person.add_roles.assert_awaited_once()


def test_no_visible_history_cannot_strip_existing_roles() -> None:
    person = member(1)
    guild, role = guild_with(person)
    person.roles = [role]
    with pytest.raises(IncompleteActivityScan, match="No readable"):
        asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert_no_updates(person)


@pytest.mark.parametrize(
    "failure", ["permission", "history", "archives", "active", "members", "channels"]
)
def test_incomplete_scans_never_grant_or_remove_roles(failure: str) -> None:
    first, second = member(1), member(2)
    guild, role = guild_with(first, second)
    second.roles = [role]
    channel = source(guild, 10, [MetadataMessage(i) for i in range(1, 4)])
    guild.channels = [channel]
    error = discord.HTTPException(Mock(status=500, reason="failure"), "sensitive detail")
    if failure == "permission":
        guild.channels.append(source(guild, 11, history=False))
    elif failure == "history":
        guild.channels.append(source(guild, 11, error=error))
    elif failure == "archives":
        channel.archived_threads.side_effect = lambda **_: stream([], error)
    elif failure == "active":
        guild.active_threads.side_effect = error
    elif failure == "channels":
        guild.fetch_channels.side_effect = error
    else:
        guild.fetch_members.side_effect = lambda **_: stream([first], error)
    with pytest.raises((IncompleteActivityScan, discord.HTTPException)):
        asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert_no_updates(first, second)


@pytest.mark.parametrize("limit", ["members", "sources", "messages"])
def test_scan_limits_abort_before_role_mutations(limit: str) -> None:
    first, second = member(1), member(2)
    guild, role = guild_with(first, second)
    first.roles = [role]
    guild.channels = [
        source(guild, 10, [MetadataMessage(i) for i in range(1, 4)]),
        source(guild, 11),
    ]
    limits = ScanLimits(**{limit: 1})
    with pytest.raises(IncompleteActivityScan):
        asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW, limits=limits))
    assert_no_updates(first, second)


@pytest.mark.parametrize("failure", ["missing", "hierarchy", "permission"])
def test_role_preflight_happens_before_scanning(failure: str) -> None:
    person = member(1)
    guild, role = guild_with(person)
    if failure == "missing":
        guild.get_role.return_value = None
    elif failure == "hierarchy":
        role.is_assignable.return_value = False
    else:
        guild.me.guild_permissions.manage_roles = False
    with pytest.raises(ActivityRoleUnavailable):
        asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    guild.fetch_members.assert_not_called()
    assert_no_updates(person)


def test_bots_webhooks_system_messages_and_departed_members_do_not_qualify() -> None:
    person, robot = member(1), member(2, bot=True)
    guild, _ = guild_with(person, robot)
    guild.channels = [
        source(
            guild,
            10,
            [
                MetadataMessage(1),
                MetadataMessage(1),
                MetadataMessage(2, bot=True),
                MetadataMessage(3, webhook=True),
                MetadataMessage(4, message_type=discord.MessageType.thread_starter_message),
                *(MetadataMessage(i, 2) for i in range(5, 8)),
                *(MetadataMessage(i, 3) for i in range(8, 11)),
            ],
        )
    ]
    result = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert result.qualified == 0
    assert_no_updates(person, robot)


def test_successful_second_pass_does_not_repeat_role_updates() -> None:
    person = member(1)
    guild, role = guild_with(person)
    guild.channels = [source(guild, 10, [MetadataMessage(i) for i in range(1, 4)])]
    asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    person.roles = [role]
    result = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert result.granted == result.removed == 0
    person.add_roles.assert_awaited_once()


def test_role_failure_is_reported_and_retry_can_recover() -> None:
    person = member(1)
    guild, _ = guild_with(person)
    guild.channels = [source(guild, 10, [MetadataMessage(i) for i in range(1, 4)])]
    person.add_roles.side_effect = discord.Forbidden(Mock(status=403, reason="failure"), "detail")
    first = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert first.failed == 1 and first.granted == 0
    person.add_roles.side_effect = None
    second = asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert second.failed == 0 and second.granted == 1


def test_update_budget_defers_work_without_replacing_other_roles() -> None:
    first, second = member(1), member(2)
    guild, _ = guild_with(first, second)
    guild.channels = [
        source(
            guild,
            10,
            [
                *(MetadataMessage(i) for i in range(1, 4)),
                *(MetadataMessage(i, 2) for i in range(4, 7)),
            ],
        )
    ]
    result = asyncio.run(
        reconcile_active_role(guild, CONFIG, as_of=NOW, limits=ScanLimits(updates=1))
    )
    assert result.granted == 1 and result.deferred == 1
    assert_no_updates(second)


def test_foreign_guild_history_is_rejected() -> None:
    person = member(1)
    guild, _ = guild_with(person)
    foreign = source(Mock(id=456), 10)
    guild.active_threads.return_value = [foreign]
    with pytest.raises(IncompleteActivityScan, match="another server"):
        asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW))
    assert_no_updates(person)


def test_duplicate_thread_discovery_is_bounded() -> None:
    person = member(1)
    guild, _ = guild_with(person)
    thread = source(guild, 20, kind=discord.Thread)
    guild.active_threads.return_value = [thread] * 7
    with pytest.raises(IncompleteActivityScan, match="discovery limit"):
        asyncio.run(reconcile_active_role(guild, CONFIG, as_of=NOW, limits=ScanLimits(sources=1)))
    assert_no_updates(person)


def test_timeout_during_history_cannot_remove_roles() -> None:
    person = member(1)
    guild, role = guild_with(person)
    person.roles = [role]

    async def blocking() -> AsyncIterator[MetadataMessage]:
        await asyncio.Event().wait()
        yield MetadataMessage(1)

    channel = source(guild, 10)
    channel.history.side_effect = lambda **_: blocking()
    guild.channels = [channel]
    with pytest.raises(TimeoutError):
        asyncio.run(
            reconcile_active_role(guild, CONFIG, as_of=NOW, limits=ScanLimits(timeout_seconds=0.05))
        )
    assert_no_updates(person)
