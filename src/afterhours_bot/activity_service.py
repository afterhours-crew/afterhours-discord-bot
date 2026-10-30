# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bounded Discord history scan followed by idempotent changes to one role."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta

import discord

from afterhours_bot.activity import ActivityMessage, MemberActivity, plan_roles
from afterhours_bot.config import ActiveRoleSettings
from afterhours_bot.storage import ActivityStore

type HistorySource = (
    discord.TextChannel | discord.VoiceChannel | discord.StageChannel | discord.Thread
)
type ArchiveParent = discord.TextChannel | discord.ForumChannel


class IncompleteActivityScan(RuntimeError):
    """History or membership enumeration could not be completed."""


class ActivityRoleUnavailable(RuntimeError):
    """The role is missing or cannot be managed by the bot."""


@dataclass(frozen=True)
class ScanLimits:
    members: int = 100_000
    sources: int = 5_000
    messages: int = 250_000
    updates: int = 1_000
    timeout_seconds: float = 900


DEFAULT_SCAN_LIMITS = ScanLimits()


@dataclass(frozen=True)
class ActivityResult:
    sources: int
    messages: int
    qualified: int
    granted: int
    removed: int
    failed: int
    deferred: int


def _readable(source: discord.abc.GuildChannel | discord.Thread, me: discord.Member) -> bool:
    permissions = source.permissions_for(me)
    if not permissions.view_channel:
        return False
    if not permissions.read_message_history:
        raise IncompleteActivityScan("A visible channel lacks Read Message History.")
    return True


async def _history_sources(
    guild: discord.Guild, me: discord.Member, limits: ScanLimits
) -> AsyncIterator[HistorySource]:
    seen: set[int] = set()
    discovered = 0
    parents: list[ArchiveParent] = []

    def unseen(source: HistorySource) -> bool:
        nonlocal discovered
        discovered += 1
        if discovered > limits.sources * 3:
            raise IncompleteActivityScan("Thread discovery limit reached.")
        if source.guild.id != guild.id:
            raise IncompleteActivityScan("History belongs to another server.")
        if source.id in seen or not _readable(source, me):
            return False
        if len(seen) >= limits.sources:
            raise IncompleteActivityScan("History source limit reached.")
        seen.add(source.id)
        return True

    # Fetch the current channel list rather than relying on a potentially stale gateway cache.
    for channel in await guild.fetch_channels():
        if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            if _readable(channel, me):
                parents.append(channel)
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
            if unseen(channel):
                yield channel

    for thread in await guild.active_threads():
        if unseen(thread):
            yield thread

    for parent in parents:
        async for thread in parent.archived_threads(limit=limits.sources + 1):
            if unseen(thread):
                yield thread
        if isinstance(parent, discord.TextChannel) and not parent.is_news():
            # Manage Threads exposes all private archives; otherwise use joined ones.
            joined_only = not parent.permissions_for(me).manage_threads
            async for thread in parent.archived_threads(
                limit=limits.sources + 1, private=True, joined=joined_only
            ):
                if unseen(thread):
                    yield thread


async def reconcile_active_role(
    guild: discord.Guild,
    settings: ActiveRoleSettings,
    *,
    store: ActivityStore,
    as_of: datetime,
    limits: ScanLimits = DEFAULT_SCAN_LIMITS,
) -> ActivityResult:
    """No changes occur until every selected history and the member list have completed."""
    async with asyncio.timeout(limits.timeout_seconds):
        revision = await store.configure(guild.id, settings, as_of=as_of)
        me = guild.me
        role = guild.get_role(settings.role_id)
        if me is None or role is None or not me.guild_permissions.manage_roles:
            raise ActivityRoleUnavailable("Configure a role and grant Manage Roles.")
        if not role.is_assignable():
            raise ActivityRoleUnavailable(
                "The role must be unmanaged and below the bot's top role."
            )

        members: dict[int, discord.Member] = {}
        async for member in guild.fetch_members(limit=limits.members + 1):
            if len(members) >= limits.members:
                raise IncompleteActivityScan("Member limit reached.")
            members[member.id] = member

        cutoff = as_of - timedelta(days=settings.window_days)
        window_start = discord.utils.time_snowflake(cutoff, high=True)
        # Exclude the current millisecond; the following pass will include it without gaps.
        boundary = discord.utils.time_snowflake(as_of, high=False)
        if window_start < 0 or boundary <= window_start:
            raise IncompleteActivityScan("Activity clock is outside the supported history range.")
        source_ids: list[int] = []
        source_count = message_count = 0
        async for source in _history_sources(guild, me, limits):
            source_count += 1
            source_ids.append(source.id)
            position = await store.position(guild.id, source.id, revision, as_of=as_of)
            after = max(window_start, position.cursor_id if position is not None else 0)
            staged: list[ActivityMessage] = []
            if after < boundary - 1:
                async for message in source.history(
                    limit=limits.messages + 1,
                    after=discord.Object(id=after),
                    before=discord.Object(id=boundary),
                    oldest_first=True,
                ):
                    message_count += 1
                    if message_count > limits.messages:
                        raise IncompleteActivityScan("Message limit reached.")
                    if not after < message.id < boundary:
                        raise IncompleteActivityScan("History did not respect the scan cursor.")
                    if (
                        not message.author.bot
                        and message.webhook_id is None
                        and message.type in (discord.MessageType.default, discord.MessageType.reply)
                        and cutoff < message.created_at <= as_of
                    ):
                        staged.append(
                            ActivityMessage(message.id, message.author.id, message.created_at)
                        )
            # Commit metadata and the cursor together only after this source completes.
            await store.commit_source(
                guild.id,
                source.id,
                settings,
                revision,
                expected=position,
                cursor_id=boundary - 1,
                as_of=as_of,
                messages=staged,
                max_rows=limits.messages,
            )

        if not source_count:
            raise IncompleteActivityScan("No readable message histories are available.")
        qualified = await store.qualified(
            guild.id,
            settings,
            revision,
            source_ids=source_ids,
            member_ids=[member.id for member in members.values() if not member.bot],
            as_of=as_of,
        )
        plan = plan_roles(
            (
                MemberActivity(
                    member_id=member.id,
                    has_role=any(existing.id == role.id for existing in member.roles),
                    is_bot=member.bot,
                )
                for member in members.values()
            ),
            qualified,
            complete=True,
        )
        updates = [(member_id, True) for member_id in plan.grant]
        updates.extend((member_id, False) for member_id in plan.remove)
        granted = removed = failed = 0
        for member_id, grant in updates[: limits.updates]:
            member = members[member_id]
            try:
                if grant:
                    await member.add_roles(
                        role, reason="Nightshift activity qualification", atomic=True
                    )
                    granted += 1
                else:
                    await member.remove_roles(
                        role, reason="Nightshift activity window expired", atomic=True
                    )
                    removed += 1
            except discord.HTTPException:
                # Members can leave or role permissions can change during the pass.
                failed += 1
        return ActivityResult(
            sources=source_count,
            messages=message_count,
            qualified=len(qualified),
            granted=granted,
            removed=removed,
            failed=failed,
            deferred=max(0, len(updates) - limits.updates),
        )
