# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Guild-scoped slash commands and the Discord connection edge."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime

import discord
from discord import app_commands

from afterhours_bot.activity_service import (
    ActivityRoleUnavailable,
    IncompleteActivityScan,
    reconcile_active_role,
)
from afterhours_bot.config import Settings
from afterhours_bot.moderation import ModerationIntake, select_message
from afterhours_bot.review import ReviewView, StaffReviewer
from afterhours_bot.rules import load_rules
from afterhours_bot.storage import ActivityStateError, ActivityStore

logger = logging.getLogger(__name__)
ACTIVITY_INTERVAL_SECONDS = 3600


class CommunityTree(app_commands.CommandTree["CommunityBot"]):
    async def interaction_check(self, interaction: discord.Interaction[CommunityBot]) -> bool:
        if interaction.guild_id != self.client.settings.guild_id:
            await interaction.response.send_message(
                "This bot is available in the Afterhours community server.", ephemeral=True
            )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction[CommunityBot],
        error: app_commands.AppCommandError,
    ) -> None:
        # Exception text can contain user input or HTTP details; log only the class.
        logger.error("Command failed (%s).", type(error).__name__)
        message = "The command could not be completed. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


class CommunityBot(discord.Client):
    def __init__(self, settings: Settings, *, sync_commands: bool = False) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = settings.active_role is not None
        if settings.moderation is not None:
            # Reviewing messages needs their events and the privileged content intent.
            intents.guild_messages = True
            intents.message_content = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
        )
        self.settings = settings
        self.sync_commands = sync_commands
        self.tree = CommunityTree(self)
        self._activity_task: asyncio.Task[None] | None = None
        self._moderation_task: asyncio.Task[None] | None = None
        self.store = ActivityStore(settings.database_path)
        self.rules_revision: int | None = None
        self.rules_digest: str | None = None
        self.reviewer: StaffReviewer | None = None
        self.moderation: ModerationIntake | None = None
        if settings.moderation is not None:
            self.reviewer = StaffReviewer(
                self,
                settings.moderation,
                self.store,
                guild_id=settings.guild_id,
                rules_revision=lambda: self.rules_revision,
            )
            self.moderation = ModerationIntake(settings.moderation, reviewer=self.reviewer.review)
        guild = discord.Object(id=settings.guild_id)

        @self.tree.command(
            name="ping", description="Check whether the bot is responding.", guild=guild
        )
        @app_commands.guild_only()
        async def ping(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.send_message("Pong!", ephemeral=True)

        @self.tree.command(
            name="about", description="About the Afterhours community bot.", guild=guild
        )
        @app_commands.guild_only()
        async def about(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.send_message(
                "Nightshift, the Afterhours community bot.\n"
                "Source: https://github.com/afterhours-crew/afterhours-discord-bot",
                ephemeral=True,
            )

        @self.tree.command(
            name="moderation",
            description="Staff only: moderation feature status.",
            guild=guild,
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_messages=True)
        async def moderation(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.send_message(await self.moderation_status(), ephemeral=True)

    async def moderation_status(self) -> str:
        """Feature state for staff; it never includes message text, members or secrets."""
        settings = self.settings.moderation
        if settings is None or self.moderation is None or self.reviewer is None:
            return "Moderation is off. Set MODERATION_STAFF_CHANNEL_ID to enable message review."
        counters = self.moderation.counters
        review = self.reviewer.counters
        stored, open_flags = await self.store.flag_summary(self.settings.guild_id)
        posting = "paused" if self.reviewer.posting_disabled else "enabled"
        rules = (
            f"revision {self.rules_revision} ({self.rules_digest[:12]})"
            if self.rules_revision is not None and self.rules_digest is not None
            else "not loaded"
        )
        worker = (
            "running"
            if self._moderation_task is not None and not self._moderation_task.done()
            else "stopped"
        )
        return (
            "Moderation is on (shadow mode: no automatic actions).\n"
            f"Staff channel: <#{settings.staff_channel_id}>\n"
            f"Rules: {rules}\n"
            f"Message Content intent: {'granted' if self.intents.message_content else 'off'}\n"
            f"Review worker: {worker}\n"
            f"Intake: {counters.submitted} submitted, {counters.queued} queued, "
            f"{counters.dropped} dropped, {counters.reviewed} reviewed, "
            f"{counters.failed} failed, {counters.pending} pending.\n"
            f"Flags: {review.flagged} flagged, {review.posted} posted, "
            f"{review.suppressed} rate limited, {review.unposted} unposted, "
            f"{review.decisions} decided this run; staff posting {posting}.\n"
            f"Audit: {stored} stored flags, {open_flags} awaiting a decision."
        )

    async def setup_hook(self) -> None:
        await self.store.configure(
            self.settings.guild_id, self.settings.active_role, as_of=datetime.now(UTC)
        )
        if self.settings.moderation is not None:
            rules = await asyncio.to_thread(load_rules, self.settings.moderation.rules_path)
            self.rules_revision = await self.store.configure_rules(
                self.settings.guild_id, rules, as_of=datetime.now(UTC)
            )
            self.rules_digest = rules.sha256
            logger.info(
                "Moderation rules loaded: revision %d (%s).",
                self.rules_revision,
                rules.short_digest,
            )
        if self.sync_commands:
            commands = await self.tree.sync(guild=discord.Object(id=self.settings.guild_id))
            logger.info("Registered %d community commands.", len(commands))
        if self.settings.active_role is not None and self._activity_task is None:
            self._activity_task = asyncio.create_task(self._activity_loop(), name="active-role")
        if self.moderation is not None and self._moderation_task is None:
            if self.reviewer is not None:
                # Buttons on earlier review posts keep working across restarts.
                self.add_view(ReviewView(self.reviewer))
            self._moderation_task = asyncio.create_task(self.moderation.run(), name="moderation")

    async def on_message(self, message: discord.Message) -> None:
        if self.moderation is None or self.settings.moderation is None:
            return
        selected = select_message(message, self.settings.moderation, self.settings.guild_id)
        if selected is not None:
            self.moderation.submit(selected)

    async def _activity_loop(self) -> None:
        settings = self.settings.active_role
        if settings is None:
            return
        while not self.is_closed():
            await self.wait_until_ready()
            cycle_started = asyncio.get_running_loop().time()
            try:
                guild = self.get_guild(self.settings.guild_id)
                if guild is None or guild.unavailable:
                    logger.warning("Active-role server is unavailable; no roles changed.")
                else:
                    logger.info("Active-role scan started.")
                    result = await reconcile_active_role(
                        guild, settings, store=self.store, as_of=datetime.now(UTC)
                    )
                    logger.info(
                        "Active-role scan: sources=%d messages=%d qualified=%d "
                        "granted=%d removed=%d failed=%d deferred=%d.",
                        result.sources,
                        result.messages,
                        result.qualified,
                        result.granted,
                        result.removed,
                        result.failed,
                        result.deferred,
                    )
                    if result.failed or result.deferred:
                        logger.warning("Some active-role updates remain pending for the next scan.")
            except (ActivityRoleUnavailable, IncompleteActivityScan, ActivityStateError) as error:
                logger.warning("Active-role scan skipped; no roles changed: %s", error)
            except Exception as error:
                # Keep retries alive without logging member data, tokens or raw HTTP details.
                logger.warning("Active-role scan did not complete (%s).", type(error).__name__)
            elapsed = asyncio.get_running_loop().time() - cycle_started
            await asyncio.sleep(max(0, ACTIVITY_INTERVAL_SECONDS - elapsed))

    async def close(self) -> None:
        for task in (self._activity_task, self._moderation_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._activity_task = None
        self._moderation_task = None
        try:
            await self.store.close()
        finally:
            await super().close()

    async def on_ready(self) -> None:
        logger.info("Nightshift connected.")
