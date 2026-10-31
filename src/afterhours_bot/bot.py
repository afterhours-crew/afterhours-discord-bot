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
from afterhours_bot.classifier import Classifier
from afterhours_bot.config import Settings
from afterhours_bot.moderation import ModerationIntake, select_message
from afterhours_bot.review import ReviewView, StaffReviewer
from afterhours_bot.rules import (
    MAX_RULES_BYTES,
    ModerationRulesError,
    RuleSet,
    load_rules,
    parse_rules,
)
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
        self.rules: RuleSet | None = None
        self.rules_source = "file"
        self.file_rules_digest: str | None = None
        self.guidance: RuleSet | None = None
        self.reviewer: StaffReviewer | None = None
        self.moderation: ModerationIntake | None = None
        self.classifier: Classifier | None = None
        if settings.moderation is not None:
            if settings.moderation.llm is not None:
                self.classifier = Classifier(
                    settings.moderation.llm,
                    rules_text=lambda: self.rules.text if self.rules is not None else None,
                    rule_numbers=lambda: self.rules.numbers if self.rules is not None else (),
                    guidance_text=lambda: self.guidance.text if self.guidance is not None else None,
                )
            self.reviewer = StaffReviewer(
                self,
                settings.moderation,
                self.store,
                guild_id=settings.guild_id,
                rules_revision=lambda: self.rules_revision,
                classifier=self.classifier,
                rule_titles=lambda: self.rules.titles if self.rules is not None else {},
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

        rules_group = app_commands.Group(
            name="rules",
            description="Staff only: the rule text the moderation feature uses.",
            guild_only=True,
            default_permissions=discord.Permissions(manage_messages=True),
        )

        @rules_group.command(name="show", description="Show the active rule revision.")
        async def rules_show(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.send_message(self.rules_status(), ephemeral=True)

        @rules_group.command(name="reload", description="Re-read the rule file from disk.")
        async def rules_reload(interaction: discord.Interaction[CommunityBot]) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await self.reload_rules(), ephemeral=True)

        @rules_group.command(
            name="upload", description="Replace the rule text with an uploaded .md or .txt file."
        )
        @app_commands.describe(file="UTF-8 Markdown or text, at most 64 KiB.")
        async def rules_upload(
            interaction: discord.Interaction[CommunityBot], file: discord.Attachment
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await self.upload_rules(file), ephemeral=True)

        self.tree.add_command(rules_group, guild=guild)

    def rules_status(self) -> str:
        if self.settings.moderation is None or self.rules is None:
            return "Moderation is off; no rule text is active."
        numbers = ", ".join(str(number) for number in self.rules.numbers) or "none"
        return (
            f"Rules revision {self.rules_revision} ({self.rules.short_digest}), "
            f"source: {self.rules_source}, rule numbers: {numbers}."
        )

    async def _apply_rules(self, rules: RuleSet, *, source: str) -> str:
        if self.settings.moderation is None:
            return "Moderation is off."
        if not rules.numbers:
            return "Rejected: the text has no numbered rule headings such as `# 1. Title`."
        self.rules_revision = await self.store.configure_rules(
            self.settings.guild_id,
            rules,
            as_of=datetime.now(UTC),
            source=source,
            file_sha256=self.file_rules_digest,
        )
        self.rules = rules
        self.rules_digest = rules.sha256
        self.rules_source = source
        logger.info(
            "Moderation rules applied from %s: revision %d (%s).",
            source,
            self.rules_revision,
            rules.short_digest,
        )
        return self.rules_status()

    async def reload_rules(self) -> str:
        if self.settings.moderation is None:
            return "Moderation is off."
        try:
            rules = await asyncio.to_thread(load_rules, self.settings.moderation.rules_path)
        except ModerationRulesError as error:
            return f"The rule file could not be loaded: {error}"
        self.file_rules_digest = rules.sha256
        return await self._apply_rules(rules, source="file")

    async def upload_rules(self, file: discord.Attachment) -> str:
        if self.settings.moderation is None:
            return "Moderation is off."
        name = file.filename.lower()
        if not (name.endswith(".md") or name.endswith(".txt")):
            return "Rejected: upload a .md or .txt file."
        if file.size > MAX_RULES_BYTES:
            return f"Rejected: the file exceeds {MAX_RULES_BYTES} bytes."
        try:
            raw = await file.read()
            rules = parse_rules(raw)
        except discord.HTTPException as error:
            logger.warning("Rule upload could not be read (%s).", type(error).__name__)
            return "The attachment could not be downloaded; try again."
        except ModerationRulesError as error:
            return f"Rejected: {error}"
        return await self._apply_rules(rules, source="upload")

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
        mode = (
            f"{len(settings.auto_actions)} automatic action rule(s) configured"
            if settings.auto_actions
            else "shadow mode: no automatic actions"
        )
        return (
            f"Moderation is on ({mode}).\n"
            f"Staff channel: <#{settings.staff_channel_id}>\n"
            f"Rules: {rules}, source {self.rules_source}\n"
            f"Message Content intent: {'granted' if self.intents.message_content else 'off'}\n"
            f"Review worker: {worker}\n"
            f"Intake: {counters.submitted} submitted, {counters.queued} queued, "
            f"{counters.dropped} dropped, {counters.reviewed} reviewed, "
            f"{counters.failed} failed, {counters.pending} pending.\n"
            f"Flags: {review.flagged} flagged, {review.posted} posted, "
            f"{review.suppressed} rate limited, {review.unposted} unposted, "
            f"{review.decisions} decided, {review.automatic} automatic, "
            f"{review.actions} actions done, {review.reflagged} edits re-flagged this run; "
            f"staff posting {posting}.\n"
            f"Audit: {stored} stored flags, {open_flags} awaiting a decision.\n"
            f"Classifier: {self.classifier_status()}"
        )

    def classifier_status(self) -> str:
        llm = self.settings.moderation.llm if self.settings.moderation is not None else None
        if llm is None or self.classifier is None:
            return "off (set MODERATION_LLM_URL and MODERATION_LLM_MODEL to enable)."
        counts = self.classifier.counters
        state = "available" if self.classifier.available else "unavailable (circuit open)"
        guidance = "with guidance" if self.guidance is not None else "without guidance"
        vision = f", images via {llm.vision_model}" if llm.vision_model else ""
        return (
            f"{state}, model {llm.model} {guidance}{vision}; {counts.requests} answers, "
            f"{counts.verdicts} verdicts, {counts.invalid} off-schema, "
            f"{counts.failures} failures, {counts.skipped_open} skipped while open."
        )

    async def setup_hook(self) -> None:
        await self.store.configure(
            self.settings.guild_id, self.settings.active_role, as_of=datetime.now(UTC)
        )
        if self.settings.moderation is not None:
            file_rules = await asyncio.to_thread(load_rules, self.settings.moderation.rules_path)
            self.file_rules_digest = file_rules.sha256
            saved = await self.store.active_rules(self.settings.guild_id)
            if (
                saved is not None
                and saved.source == "upload"
                and saved.file_sha256 == file_rules.sha256
            ):
                # An uploaded text stays active until the file itself changes.
                self.rules = RuleSet(text=saved.text, sha256=saved.sha256)
                self.rules_revision = saved.revision
                self.rules_digest = saved.sha256
                self.rules_source = "upload"
                logger.info(
                    "Moderation rules loaded from an earlier upload: revision %d (%s).",
                    saved.revision,
                    self.rules.short_digest,
                )
            else:
                await self._apply_rules(file_rules, source="file")
            llm = self.settings.moderation.llm
            if llm is not None and llm.guidance_path is not None:
                try:
                    self.guidance = await asyncio.to_thread(load_rules, llm.guidance_path)
                except ModerationRulesError as error:
                    logger.warning("Classifier guidance not loaded: %s", error)
                else:
                    logger.info("Classifier guidance loaded (%s).", self.guidance.short_digest)
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

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Edited messages are reviewed again; the message cache is off, so use the raw event."""
        if self.moderation is None or self.settings.moderation is None:
            return
        selected = select_message(
            payload.message, self.settings.moderation, self.settings.guild_id, edited=True
        )
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
            if self.classifier is not None:
                await self.classifier.close()
            await self.store.close()
        finally:
            await super().close()

    async def on_ready(self) -> None:
        logger.info("Nightshift connected.")
