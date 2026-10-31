# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Shadow-mode staff review: flags become posts with buttons; humans decide, the bot records."""

import asyncio
import logging
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import discord

from afterhours_bot.actions import ActionExecutor, ActionTarget, Outcome
from afterhours_bot.classifier import Classifier
from afterhours_bot.config import AutoAction, ModerationSettings
from afterhours_bot.filters import Flag, RepeatTracker, evaluate
from afterhours_bot.moderation import IntakeMessage
from afterhours_bot.storage import ActivityStore

logger = logging.getLogger(__name__)
RATE_WINDOW_SECONDS = 60.0
POSTING_RETRY_SECONDS = 600.0
LOG_INTERVAL_SECONDS = 60.0
CUSTOM_ID_PREFIX = "nightshift:review:"
DECISIONS = ("dismiss", "delete", "warn", "timeout")
CONTEXT_CHARS = 200
MAX_CONTEXT_CHANNELS = 500

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
    automatic: int = 0
    reflagged: int = 0


MAX_EXCERPT_CHARS = 1000
MAX_LISTED_ATTACHMENTS = 5
COLOR_HIGH = 0xE74C3C
COLOR_MEDIUM = 0xE67E22
COLOR_LOW = 0x95A5A6
COLOR_DISMISSED = 0x2ECC71
COLOR_ACTED = 0x3498DB
COLOR_FAILED = 0xD35400


def excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    """A quoted, mention-safe copy of the message text for staff to read in place."""
    cleaned = discord.utils.escape_mentions(text.strip())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return "\n".join(f"> {line}" if line else ">" for line in cleaned.splitlines())


def build_embed(
    message: IntakeMessage,
    flags: tuple[Flag, ...],
    rules_revision: int | None,
    titles: Mapping[int, str],
    *,
    show_excerpt: bool = True,
    automatic: str | None = None,
    models: Mapping[str, str] | None = None,
) -> discord.Embed:
    """One review post: what was said, which rule, why, and where to find it."""
    numbers = sorted({flag.rule for flag in flags})
    if len(numbers) == 1:
        title = f"Rule {numbers[0]}"
        if numbers[0] in titles:
            title += f" · {titles[numbers[0]]}"
    else:
        title = "Rules " + ", ".join(str(number) for number in numbers)
    best = max(flag.score for flag in flags)
    color = COLOR_HIGH if best >= 0.8 else COLOR_MEDIUM if best >= 0.5 else COLOR_LOW
    embed = discord.Embed(title=title, color=color, url=message.jump_url or None)
    body = excerpt(message.content) if show_excerpt else ""
    embed.description = body or "*(no text)*"
    embed.add_field(name="Author", value=f"<@{message.author_id}>", inline=True)
    embed.add_field(name="Channel", value=f"<#{message.channel_id}>", inline=True)
    sources = sorted({flag.source for flag in flags})
    embed.add_field(
        name="Confidence", value=f"{best:.0%} ({best:.2f}) · " + ", ".join(sources), inline=True
    )
    embed.add_field(
        name="Why",
        value="\n".join(
            f"{flag.reason} (rule {flag.rule}, score {flag.score:.2f}, {flag.source})"
            for flag in flags
        ),
        inline=False,
    )
    named = models or {}
    labelled = [f"{source} ({named[source]})" if source in named else source for source in sources]
    details = [
        f"Score {best:.2f} · source {', '.join(labelled)}"
        + (" · flagged after an edit" if message.edited else ""),
        f"Rules revision {rules_revision if rules_revision is not None else 'unknown'}",
    ]
    embed.add_field(name="Details", value="\n".join(details)[:1024], inline=False)
    if message.attachment_names:
        names = list(message.attachment_names)
        listed = ", ".join(names[:MAX_LISTED_ATTACHMENTS])
        if len(names) > MAX_LISTED_ATTACHMENTS:
            listed += f" and {len(names) - MAX_LISTED_ATTACHMENTS} more"
        embed.add_field(name="Attachments", value=listed[:1024], inline=False)
    if message.image_urls and show_excerpt:
        embed.set_image(url=message.image_urls[0])
    if automatic is not None:
        embed.add_field(name="Automatic action", value=automatic, inline=False)
    revision = f"rules revision {rules_revision}" if rules_revision is not None else "rules unknown"
    mode = "automatic action taken" if automatic is not None else "nothing done automatically"
    embed.set_footer(text=f"{revision} · {mode}")
    embed.timestamp = message.created_at
    return embed


def resolve_embed(
    embed: discord.Embed, decision: str, decider_id: int, outcome: Outcome | None
) -> discord.Embed:
    """Mark a review post as handled: colour and a Resolved field, no appended text."""
    if decision == "dismiss":
        embed.color = COLOR_DISMISSED
        value = f"Dismissed by <@{decider_id}>"
    else:
        embed.color = COLOR_ACTED if outcome is not None and outcome.ok else COLOR_FAILED
        value = f"{decision.capitalize()} by <@{decider_id}>"
        if outcome is not None:
            value += f"\n{outcome.summary}"
    embed.add_field(name="Resolved", value=value, inline=False)
    return embed


class LinkView(discord.ui.View):
    """Only the link to the original message; used once a post is resolved or automatic."""

    def __init__(self, jump_url: str | None) -> None:
        super().__init__(timeout=None)
        if jump_url:
            self.add_item(
                discord.ui.Button(
                    label="Open message", style=discord.ButtonStyle.link, url=jump_url
                )
            )


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
        self.rule_titles = rule_titles
        self.executor = ActionExecutor(client, settings, guild_id=guild_id, rule_titles=rule_titles)
        self._context: OrderedDict[int, deque[tuple[int, str]]] = OrderedDict()
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

    def _model_allowed(self, message: IntakeMessage) -> bool:
        llm = self.settings.llm
        if llm is None or self.classifier is None:
            return False
        return not (llm.skip_staff and message.author_manages_messages)

    def _context_for(self, message: IntakeMessage) -> tuple[str, ...]:
        llm = self.settings.llm
        if llm is None or llm.context_messages == 0:
            return ()
        recent = self._context.get(message.channel_id)
        if not recent:
            return ()
        return tuple(line for _, line in recent)

    def _remember(self, message: IntakeMessage) -> None:
        """Keep a bounded, in-memory tail of each channel for model context only."""
        llm = self.settings.llm
        if llm is None or llm.context_messages == 0 or message.edited:
            return
        text = " ".join(message.content.split())[:CONTEXT_CHARS]
        if not text:
            return
        recent = self._context.get(message.channel_id)
        if recent is None:
            while len(self._context) >= MAX_CONTEXT_CHANNELS:
                self._context.popitem(last=False)
            recent = deque(maxlen=llm.context_messages)
            self._context[message.channel_id] = recent
        else:
            self._context.move_to_end(message.channel_id)
        recent.append((message.author_id, f"member {message.author_id % 1000:03d}: {text}"))

    async def review(self, message: IntakeMessage) -> None:
        flags = evaluate(message, self.settings, self.tracker)
        if not flags and self._model_allowed(message):
            # The model only sees what the deterministic filters let through.
            if message.content.strip():
                flags = await self._classify(message, context=self._context_for(message))
            if not flags and message.image_urls:
                flags = await self._classify(message, image_urls=message.image_urls)
        self._remember(message)
        existing_count, existing_post = 0, None
        if message.edited:
            existing_count, existing_post = await self.store.message_flags(
                self.guild_id, message.message_id
            )
            if existing_post is not None:
                await self._refresh_post(existing_post, message, flags)
        if not flags:
            return
        self._counters = replace(self._counters, flagged=self._counters.flagged + 1)
        auto = None if existing_count else self._auto_action(message, flags)
        outcome: Outcome | None = None
        if auto is not None:
            target = ActionTarget(
                message.message_id, message.channel_id, message.author_id, (auto.rule,)
            )
            outcome = await self.executor.perform(auto.action, target)
            note = f"{auto.action} ({auto.source}, rule {auto.rule}): {outcome.summary}"
        else:
            note = None
        if existing_count:
            # An edited message that was already flagged keeps its first review post.
            self._counters = replace(self._counters, reflagged=self._counters.reflagged + 1)
            post_id = existing_post
        else:
            post_id = await self._post(message, flags, note=note)
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
        if auto is not None and outcome is not None:
            await self.store.record_auto_decision(
                self.guild_id,
                message.message_id,
                decision=f"auto-{auto.action}",
                decider_id=self._bot_id(),
                outcome=outcome.summary,
                as_of=self.clock(),
            )
            self._counters = replace(self._counters, automatic=self._counters.automatic + 1)
            if outcome.ok:
                self._counters = replace(self._counters, actions=self._counters.actions + 1)

    async def _refresh_post(
        self, post_id: int, message: IntakeMessage, flags: tuple[Flag, ...]
    ) -> None:
        """Show the edited text on the existing review post, keeping the original excerpt."""
        try:
            channel = await self._staff_channel()
            if channel is None:
                return
            post = await channel.fetch_message(post_id)
            if not post.embeds:
                return
            embed = post.embeds[0]
            names = [field.name for field in embed.fields]
            if self.settings.post_excerpt:
                if "Original" not in names:
                    original = (embed.description or "")[:1024] or "*(no text)*"
                    embed.add_field(name="Original", value=original, inline=False)
                embed.description = excerpt(message.content) or "*(no text)*"
            for index, name in reversed(list(enumerate(names))):
                if name == "Edited":
                    embed.remove_field(index)
            value = f"edited {discord.utils.format_dt(self.clock(), 'R')}"
            if flags:
                value += "; now: " + "; ".join(
                    f"{flag.reason} (rule {flag.rule}, {flag.score:.0%})" for flag in flags
                )
            else:
                value += "; the filters find nothing in the new text"
            embed.add_field(name="Edited", value=value[:1024], inline=False)
            await post.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException as error:
            logger.warning("Review post could not follow an edit (%s).", type(error).__name__)

    def _bot_id(self) -> int:
        user = self.client.user
        return user.id if user is not None else self.guild_id

    def _auto_action(self, message: IntakeMessage, flags: tuple[Flag, ...]) -> AutoAction | None:
        """The first configured automatic action a deterministic flag satisfies, if any."""
        if message.author_manages_messages:
            return None
        for configured in self.settings.auto_actions:
            for flag in flags:
                if (
                    flag.source == configured.source
                    and flag.rule == configured.rule
                    and flag.score >= configured.min_score
                ):
                    return configured
        return None

    async def _classify(
        self,
        message: IntakeMessage,
        *,
        context: Sequence[str] = (),
        image_urls: Sequence[str] = (),
    ) -> tuple[Flag, ...]:
        llm = self.settings.llm
        if llm is None or self.classifier is None:
            return ()
        if image_urls and llm.vision_model is None:
            return ()
        verdict = await self.classifier.classify(
            message.content, context=context, image_urls=image_urls
        )
        if verdict is None or verdict.rule is None or verdict.score < llm.min_score:
            return ()
        source = "llm-image" if image_urls else "llm"
        prefix = "model (image)" if image_urls else "model"
        return (Flag(verdict.rule, verdict.score, source, f"{prefix}: {verdict.reason}"),)

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

    async def _post(
        self, message: IntakeMessage, flags: tuple[Flag, ...], *, note: str | None = None
    ) -> int | None:
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
            llm = self.settings.llm
            models: dict[str, str] = {}
            if llm is not None:
                models["llm"] = llm.model
                if llm.vision_model:
                    models["llm-image"] = llm.vision_model
            embed = build_embed(
                message,
                flags,
                self.rules_revision(),
                self.rule_titles(),
                show_excerpt=self.settings.post_excerpt,
                automatic=note,
                models=models,
            )
            view: discord.ui.View = (
                ReviewView(self, jump_url=message.jump_url)
                if note is None
                else LinkView(message.jump_url)
            )
            post = await channel.send(
                embed=embed,
                view=view,
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
        embeds = interaction.message.embeds
        embed = embeds[0] if embeds else discord.Embed(title="Review")
        resolved = resolve_embed(
            embed, decision, interaction.user.id, None if decision == "dismiss" else outcome
        )
        await interaction.response.edit_message(
            embed=resolved,
            view=LinkView(embed.url),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if decision != "dismiss":
            prefix = "Done:" if outcome.ok else "Not done:"
            await interaction.followup.send(f"{prefix} {outcome.summary}.", ephemeral=True)


class ReviewView(discord.ui.View):
    """Persistent buttons; the post message ID links clicks back to the audit."""

    def __init__(self, reviewer: StaffReviewer, *, jump_url: str | None = None) -> None:
        super().__init__(timeout=None)
        self.reviewer = reviewer
        if jump_url:
            self.add_item(
                discord.ui.Button(
                    label="Open message", style=discord.ButtonStyle.link, url=jump_url
                )
            )

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
