# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shadow-mode staff review: flags become posts with buttons; humans decide, the bot records."""

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import discord

from afterhours_bot.actions import ActionExecutor, ActionTarget, Outcome
from afterhours_bot.classifier import Classifier
from afterhours_bot.config import ModerationSettings
from afterhours_bot.filters import Flag, RepeatTracker, evaluate
from afterhours_bot.moderation import IntakeMessage
from afterhours_bot.storage import ActivityStore

logger = logging.getLogger(__name__)
RATE_WINDOW_SECONDS = 60.0
POSTING_RETRY_SECONDS = 600.0
LOG_INTERVAL_SECONDS = 60.0
CUSTOM_ID_PREFIX = "nightshift:review:"
DECISIONS = ("dismiss", "delete", "warn", "timeout")

type StaffChannel = discord.TextChannel | discord.Thread


class StaffChannelUnavailable(RuntimeError):
    """The staff channel is missing, in another server, not a text channel or not writable."""


@dataclass(frozen=True)
class ReviewCounters:
    flagged: int = 0
    posted: int = 0
    suppressed: int = 0
    unposted: int = 0
    decisions: int = 0
    actions: int = 0


def format_post(message: IntakeMessage, flags: tuple[Flag, ...], rules_revision: int | None) -> str:
    """Staff post text: rule, confidence, reason and a jump link. Never the message text."""
    lines = ["**Review needed** (shadow mode: nothing was done automatically)"]
    for flag in flags:
        lines.append(
            f"Rule {flag.rule}: {flag.reason} (confidence {flag.score:.2f}, {flag.source})"
        )
    link = message.jump_url or f"message {message.message_id}"
    revision = f"rules revision {rules_revision}" if rules_revision is not None else "rules unknown"
    lines.append(f"Message: {link} by <@{message.author_id}> in <#{message.channel_id}>")
    lines.append(f"Reviewed against {revision}.")
    return "\n".join(lines)


class StaffReviewer:
    """Evaluates intake messages, posts flags to the staff channel and audits them."""

    def __init__(
        self,
        client: discord.Client,
        settings: ModerationSettings,
        store: ActivityStore,
        *,
        guild_id: int,
        rules_revision: Callable[[], int | None],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        classifier: Classifier | None = None,
        rule_titles: Callable[[], Mapping[int, str]] = lambda: {},
    ) -> None:
        self.client = client
        self.settings = settings
        self.store = store
        self.guild_id = guild_id
        self.rules_revision = rules_revision
        self.clock = clock
        self.classifier = classifier
        self.executor = ActionExecutor(client, settings, guild_id=guild_id, rule_titles=rule_titles)
        self.tracker = RepeatTracker(settings.repeat_window_seconds, settings.repeat_threshold)
        self._counters = ReviewCounters()
        self._recent_posts: deque[float] = deque()
        self._posting_disabled_until: float | None = None
        self._last_suppressed_log: float | None = None

    @property
    def counters(self) -> ReviewCounters:
        return self._counters

    @property
    def posting_disabled(self) -> bool:
        return (
            self._posting_disabled_until is not None
            and asyncio.get_running_loop().time() < self._posting_disabled_until
        )

    def _classifiable(self, message: IntakeMessage) -> bool:
        llm = self.settings.llm
        if llm is None or self.classifier is None or not message.content.strip():
            return False
        return not (llm.skip_staff and message.author_manages_messages)

    async def review(self, message: IntakeMessage) -> None:
        flags = evaluate(message, self.settings, self.tracker)
        if not flags and self._classifiable(message):
            # The model only sees what the deterministic filters let through.
            flags = await self._classify(message)
        if not flags:
            return
        self._counters = replace(self._counters, flagged=self._counters.flagged + 1)
        post_id = await self._post(message, flags)
        await self.store.record_flags(
            self.guild_id,
            message_id=message.message_id,
            channel_id=message.channel_id,
            author_id=message.author_id,
            flags=flags,
            rules_revision=self.rules_revision(),
            post_message_id=post_id,
            as_of=self.clock(),
            retention_days=self.settings.audit_retention_days,
            max_rows=self.settings.audit_max_rows,
        )

    async def _classify(self, message: IntakeMessage) -> tuple[Flag, ...]:
        llm = self.settings.llm
        if llm is None or self.classifier is None:
            return ()
        verdict = await self.classifier.classify(message.content)
        if verdict is None or verdict.rule is None or verdict.score < llm.min_score:
            return ()
        return (Flag(verdict.rule, verdict.score, "llm", f"model: {verdict.reason}"),)

    def _rate_limited(self, now: float) -> bool:
        while self._recent_posts and now - self._recent_posts[0] >= RATE_WINDOW_SECONDS:
            self._recent_posts.popleft()
        return len(self._recent_posts) >= self.settings.flags_per_minute

    async def _staff_channel(self) -> StaffChannel | None:
        channel = self.client.get_channel(self.settings.staff_channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(self.settings.staff_channel_id)
        if not isinstance(channel, discord.TextChannel | discord.Thread):
            return None
        if channel.guild.id != self.guild_id:
            return None
        me = channel.guild.me
        if me is not None and not channel.permissions_for(me).send_messages:
            return None
        return channel

    async def _post(self, message: IntakeMessage, flags: tuple[Flag, ...]) -> int | None:
        now = asyncio.get_running_loop().time()
        if self.posting_disabled:
            self._counters = replace(self._counters, unposted=self._counters.unposted + 1)
            return None
        if self._rate_limited(now):
            self._counters = replace(self._counters, suppressed=self._counters.suppressed + 1)
            if (
                self._last_suppressed_log is None
                or now - self._last_suppressed_log >= LOG_INTERVAL_SECONDS
            ):
                self._last_suppressed_log = now
                logger.warning(
                    "Review posts are rate limited; %d flags kept in the audit without a post.",
                    self._counters.suppressed,
                )
            return None
        try:
            channel = await self._staff_channel()
            if channel is None:
                raise StaffChannelUnavailable
            post = await channel.send(
                format_post(message, flags, self.rules_revision()),
                view=ReviewView(self),
                allowed_mentions=discord.AllowedMentions.none(),
                silent=True,
            )
        except (discord.DiscordException, StaffChannelUnavailable) as error:
            self._posting_disabled_until = now + POSTING_RETRY_SECONDS
            self._counters = replace(self._counters, unposted=self._counters.unposted + 1)
            logger.warning(
                "Review posts disabled for %d s: the staff channel is missing or not writable "
                "(%s). Flags stay in the audit.",
                int(POSTING_RETRY_SECONDS),
                type(error).__name__,
            )
            return None
        self._recent_posts.append(now)
        self._counters = replace(self._counters, posted=self._counters.posted + 1)
        return post.id

    def _allowed(self, interaction: discord.Interaction, decision: str) -> bool:
        permissions = interaction.permissions
        if decision == "timeout":
            return bool(permissions.moderate_members)
        return bool(permissions.manage_messages)

    async def decide(self, interaction: discord.Interaction, decision: str) -> None:
        """Handle a button click: staff only, first decision wins, action performed and audited."""
        if decision not in DECISIONS:
            raise ValueError("Unknown review decision.")
        if (
            interaction.guild_id != self.guild_id
            or interaction.message is None
            or not self._allowed(interaction, decision)
        ):
            needed = "Moderate Members" if decision == "timeout" else "Manage Messages"
            await interaction.response.send_message(
                f"Only staff with {needed} can use this button.", ephemeral=True
            )
            return
        now = self.clock()
        result = await self.store.record_decision(
            self.guild_id,
            interaction.message.id,
            decision=decision,
            decider_id=interaction.user.id,
            as_of=now,
        )
        if result is None:
            await interaction.response.send_message(
                "This review post is no longer in the audit record.", ephemeral=True
            )
            return
        if result.already_decided:
            note = f" ({result.outcome})" if result.outcome else ""
            await interaction.response.send_message(
                f"Already resolved as {result.decision} by <@{result.decider_id}>{note}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self._counters = replace(self._counters, decisions=self._counters.decisions + 1)
        age = now - datetime.fromtimestamp(result.created_us / 1_000_000, tz=UTC)
        if decision != "dismiss" and age > timedelta(hours=self.settings.button_expiry_hours):
            outcome = Outcome(
                False,
                f"buttons expire after {self.settings.button_expiry_hours} hours; act manually",
            )
        else:
            target = ActionTarget(
                result.message_id, result.channel_id, result.author_id, result.rules
            )
            outcome = await self.executor.perform(decision, target)
        await self.store.record_outcome(self.guild_id, interaction.message.id, outcome.summary)
        if outcome.ok:
            self._counters = replace(self._counters, actions=self._counters.actions + 1)
        status = "" if decision == "dismiss" else f" — {outcome.summary}"
        content = (
            f"{interaction.message.content}\n**Resolved:** {decision} by "
            f"<@{interaction.user.id}>{status}"
        )
        await interaction.response.edit_message(
            content=content, view=None, allowed_mentions=discord.AllowedMentions.none()
        )
        if decision != "dismiss":
            prefix = "Done:" if outcome.ok else "Not done:"
            await interaction.followup.send(f"{prefix} {outcome.summary}.", ephemeral=True)


class ReviewView(discord.ui.View):
    """Persistent buttons; the post message ID links clicks back to the audit."""

    def __init__(self, reviewer: StaffReviewer) -> None:
        super().__init__(timeout=None)
        self.reviewer = reviewer

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[ReviewView]
    ) -> None:
        logger.error("Review button failed (%s).", type(error).__name__)
        message = "The decision could not be recorded. Please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(
        label="Dismiss", style=discord.ButtonStyle.secondary, custom_id=CUSTOM_ID_PREFIX + "dismiss"
    )
    async def dismiss(
        self, interaction: discord.Interaction, button: discord.ui.Button[ReviewView]
    ) -> None:
        await self.reviewer.decide(interaction, "dismiss")

    @discord.ui.button(
        label="Delete", style=discord.ButtonStyle.danger, custom_id=CUSTOM_ID_PREFIX + "delete"
    )
    async def delete(
        self, interaction: discord.Interaction, button: discord.ui.Button[ReviewView]
    ) -> None:
        await self.reviewer.decide(interaction, "delete")

    @discord.ui.button(
        label="Warn", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID_PREFIX + "warn"
    )
    async def warn(
        self, interaction: discord.Interaction, button: discord.ui.Button[ReviewView]
    ) -> None:
        await self.reviewer.decide(interaction, "warn")

    @discord.ui.button(
        label="Timeout", style=discord.ButtonStyle.danger, custom_id=CUSTOM_ID_PREFIX + "timeout"
    )
    async def timeout_member(
        self, interaction: discord.Interaction, button: discord.ui.Button[ReviewView]
    ) -> None:
        await self.reviewer.decide(interaction, "timeout")
