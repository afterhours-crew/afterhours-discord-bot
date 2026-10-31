# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Moderation actions taken on a staff decision. Outcomes are short, safe summaries."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta

import discord

from afterhours_bot.config import ModerationSettings

logger = logging.getLogger(__name__)
AUDIT_REASON = "Nightshift: staff decision on a review post"


@dataclass(frozen=True)
class ActionTarget:
    message_id: int
    channel_id: int
    author_id: int
    rules: tuple[int, ...]


@dataclass(frozen=True)
class Outcome:
    ok: bool
    summary: str


class ActionExecutor:
    """Performs delete, warn and timeout with the bot's permissions checked first."""

    def __init__(
        self,
        client: discord.Client,
        settings: ModerationSettings,
        *,
        guild_id: int,
        rule_titles: Callable[[], Mapping[int, str]] = lambda: {},
    ) -> None:
        self.client = client
        self.settings = settings
        self.guild_id = guild_id
        self.rule_titles = rule_titles

    def _guild(self) -> discord.Guild | None:
        guild = self.client.get_guild(self.guild_id)
        return guild if guild is not None and not guild.unavailable else None

    async def _member(self, guild: discord.Guild, member_id: int) -> discord.Member | None:
        member = guild.get_member(member_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(member_id)
        except discord.NotFound:
            return None

    async def delete(self, target: ActionTarget) -> Outcome:
        guild = self._guild()
        if guild is None:
            return Outcome(False, "the server is unavailable")
        channel = guild.get_channel_or_thread(target.channel_id)
        if channel is None:
            try:
                fetched = await self.client.fetch_channel(target.channel_id)
            except discord.NotFound, discord.Forbidden:
                return Outcome(False, "the channel is gone or not visible to the bot")
            channel = (
                fetched if isinstance(fetched, discord.abc.GuildChannel | discord.Thread) else None
            )
        if not isinstance(channel, discord.TextChannel | discord.Thread | discord.VoiceChannel):
            return Outcome(False, "the channel type does not hold messages")
        me = guild.me
        if me is not None and not channel.permissions_for(me).manage_messages:
            return Outcome(False, "the bot lacks Manage Messages in that channel")
        try:
            message = await channel.fetch_message(target.message_id)
            await message.delete()
        except discord.NotFound:
            return Outcome(True, "the message was already gone")
        except discord.Forbidden:
            return Outcome(False, "Discord refused the deletion (permissions)")
        except discord.HTTPException as error:
            logger.warning("Delete failed (%s).", type(error).__name__)
            return Outcome(False, "Discord did not accept the deletion; try again later")
        return Outcome(True, "message deleted")

    async def warn(self, target: ActionTarget) -> Outcome:
        guild = self._guild()
        if guild is None:
            return Outcome(False, "the server is unavailable")
        member = await self._member(guild, target.author_id)
        if member is None:
            return Outcome(False, "the member is no longer in the server")
        if member.bot:
            return Outcome(False, "the author is a bot")
        titles = self.rule_titles()
        rule = target.rules[0] if target.rules else 0
        text = self.settings.warn_template.format(
            server=guild.name,
            channel=f"<#{target.channel_id}>",
            rule=", ".join(str(number) for number in target.rules) or "?",
            title="; ".join(titles.get(number, f"rule {number}") for number in target.rules)
            or titles.get(rule, "the community rules"),
        )
        try:
            await member.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            return Outcome(False, "the member does not accept direct messages")
        except discord.HTTPException as error:
            logger.warning("Warn failed (%s).", type(error).__name__)
            return Outcome(False, "the direct message could not be sent; try again later")
        return Outcome(True, "warning sent by direct message")

    async def timeout(self, target: ActionTarget) -> Outcome:
        guild = self._guild()
        if guild is None:
            return Outcome(False, "the server is unavailable")
        me = guild.me
        if me is not None and not me.guild_permissions.moderate_members:
            return Outcome(False, "the bot lacks Moderate Members")
        member = await self._member(guild, target.author_id)
        if member is None:
            return Outcome(False, "the member is no longer in the server")
        if member.bot or member.guild_permissions.administrator:
            return Outcome(False, "the member cannot be timed out")
        if me is not None and member.top_role >= me.top_role:
            return Outcome(False, "the member's roles are not below the bot's")
        duration = timedelta(minutes=self.settings.timeout_minutes)
        try:
            await member.timeout(duration, reason=AUDIT_REASON)
        except discord.Forbidden:
            return Outcome(False, "Discord refused the timeout (permissions or role order)")
        except discord.HTTPException as error:
            logger.warning("Timeout failed (%s).", type(error).__name__)
            return Outcome(False, "Discord did not accept the timeout; try again later")
        return Outcome(True, f"timed out for {self.settings.timeout_minutes} minutes")

    async def perform(self, decision: str, target: ActionTarget) -> Outcome:
        if decision == "delete":
            return await self.delete(target)
        if decision == "warn":
            return await self.warn(target)
        if decision == "timeout":
            return await self.timeout(target)
        return Outcome(True, "dismissed")
