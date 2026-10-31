# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bounded message intake between the Discord edge and review. Text stays in memory only."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime

import discord

from afterhours_bot.config import ModerationSettings

logger = logging.getLogger(__name__)
DROP_LOG_INTERVAL_SECONDS = 60.0
USER_MESSAGE_TYPES = (discord.MessageType.default, discord.MessageType.reply)


@dataclass(frozen=True)
class IntakeMessage:
    """One member message selected for review. Never persist or log `content`."""

    message_id: int
    channel_id: int
    author_id: int
    created_at: datetime
    content: str
    jump_url: str = ""
    attachment_names: tuple[str, ...] = ()


def select_message(
    message: discord.Message, settings: ModerationSettings, guild_id: int
) -> IntakeMessage | None:
    """Return the message for review, or None when it is out of scope."""
    guild = message.guild
    if guild is None or guild.id != guild_id:
        return None
    if message.author.bot or message.webhook_id is not None:
        return None
    if message.type not in USER_MESSAGE_TYPES:
        return None
    channel = message.channel
    if channel.id == settings.staff_channel_id:
        return None
    if getattr(channel, "parent_id", None) == settings.staff_channel_id:
        return None
    return IntakeMessage(
        message_id=message.id,
        channel_id=channel.id,
        author_id=message.author.id,
        created_at=message.created_at,
        content=message.content,
        jump_url=message.jump_url,
        attachment_names=tuple(attachment.filename for attachment in message.attachments),
    )


@dataclass(frozen=True)
class IntakeCounters:
    submitted: int = 0
    queued: int = 0
    dropped: int = 0
    reviewed: int = 0
    failed: int = 0

    @property
    def pending(self) -> int:
        return self.queued - self.reviewed - self.failed


type Reviewer = Callable[[IntakeMessage], Awaitable[None]]


async def no_review(message: IntakeMessage) -> None:
    """Placeholder reviewer; the message is discarded."""


class ModerationIntake:
    """Bounded queue drained by one worker; a full queue drops rather than blocks the edge."""

    def __init__(self, settings: ModerationSettings, *, reviewer: Reviewer = no_review) -> None:
        self.settings = settings
        self._reviewer = reviewer
        self._queue: asyncio.Queue[IntakeMessage] = asyncio.Queue(maxsize=settings.queue_size)
        self._counters = IntakeCounters()
        self._last_drop_log: float | None = None

    @property
    def counters(self) -> IntakeCounters:
        return self._counters

    def submit(self, message: IntakeMessage) -> bool:
        self._counters = replace(self._counters, submitted=self._counters.submitted + 1)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._counters = replace(self._counters, dropped=self._counters.dropped + 1)
            now = asyncio.get_running_loop().time()
            if (
                self._last_drop_log is None
                or now - self._last_drop_log >= DROP_LOG_INTERVAL_SECONDS
            ):
                self._last_drop_log = now
                logger.warning(
                    "Moderation intake queue is full; %d messages dropped so far.",
                    self._counters.dropped,
                )
            return False
        self._counters = replace(self._counters, queued=self._counters.queued + 1)
        return True

    async def run(self) -> None:
        """Review queued messages one at a time until cancelled."""
        while True:
            message = await self._queue.get()
            try:
                await self._reviewer(message)
                self._counters = replace(self._counters, reviewed=self._counters.reviewed + 1)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Never log the message; exception text can quote it.
                self._counters = replace(self._counters, failed=self._counters.failed + 1)
                logger.warning("Moderation review failed (%s).", type(error).__name__)
            finally:
                self._queue.task_done()
