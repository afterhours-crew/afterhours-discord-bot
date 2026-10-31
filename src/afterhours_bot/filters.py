# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Deterministic review filters. Reasons never quote message text, only configured values."""

import hashlib
import re
from collections import OrderedDict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from afterhours_bot.config import DenyEntry, ModerationSettings
from afterhours_bot.moderation import IntakeMessage

SPAM_RULE = 6
INVITE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite|discord\.com/invites)"
    r"/[A-Za-z0-9-]+",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]\"']+", re.IGNORECASE)
MAX_URLS = 50
MAX_ATTACHMENTS = 50


@dataclass(frozen=True)
class Flag:
    """One reason a message needs staff review; `reason` is safe to show and store."""

    rule: int
    score: float
    source: str
    reason: str


def find_invites(content: str) -> Flag | None:
    count = len(INVITE_PATTERN.findall(content))
    if not count:
        return None
    return Flag(
        rule=SPAM_RULE,
        score=0.6,
        source="invite",
        reason=f"{count} Discord server invite link{'s' if count != 1 else ''}",
    )


def _host(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host.lower().rstrip(".")


def _matches(host: str, entry: DenyEntry) -> bool:
    return host == entry.value or host.endswith("." + entry.value)


def find_denied_links(content: str, denylist: Sequence[DenyEntry]) -> tuple[Flag, ...]:
    if not denylist:
        return ()
    hits: dict[DenyEntry, int] = {}
    for url in URL_PATTERN.findall(content)[:MAX_URLS]:
        host = _host(url)
        if host is None:
            continue
        for entry in denylist:
            if _matches(host, entry):
                hits[entry] = hits.get(entry, 0) + 1
                break
    return tuple(
        Flag(
            rule=entry.rule,
            score=0.9,
            source="link-domain",
            reason=f"{count} link{'s' if count != 1 else ''} to listed domain {entry.value}",
        )
        for entry, count in hits.items()
    )


def find_denied_attachments(
    names: Iterable[str], denylist: Sequence[DenyEntry]
) -> tuple[Flag, ...]:
    if not denylist:
        return ()
    by_extension = {entry.value: entry for entry in denylist}
    hits: dict[DenyEntry, int] = {}
    for name in list(names)[:MAX_ATTACHMENTS]:
        stem, separator, extension = name.rpartition(".")
        if not separator or not stem:
            continue
        entry = by_extension.get(extension.lower())
        if entry is not None:
            hits[entry] = hits.get(entry, 0) + 1
    return tuple(
        Flag(
            rule=entry.rule,
            score=0.8,
            source="attachment",
            reason=f"{count} attachment{'s' if count != 1 else ''} with extension .{entry.value}",
        )
        for entry, count in hits.items()
    )


class RepeatTracker:
    """Counts identical messages per author inside a rolling window, with bounded memory."""

    def __init__(
        self,
        window_seconds: int,
        threshold: int,
        *,
        max_authors: int = 10_000,
        max_per_author: int = 50,
    ) -> None:
        self.window_seconds = window_seconds
        self.threshold = threshold
        self.max_authors = max_authors
        self.max_per_author = max_per_author
        self._authors: OrderedDict[int, deque[tuple[str, float]]] = OrderedDict()

    def observe(self, author_id: int, content: str, at: datetime) -> Flag | None:
        normalized = " ".join(content.split()).lower()
        if not normalized:
            return None
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = at.timestamp()
        recent = self._authors.get(author_id)
        if recent is None:
            while len(self._authors) >= self.max_authors:
                self._authors.popitem(last=False)
            recent = deque(maxlen=self.max_per_author)
            self._authors[author_id] = recent
        else:
            self._authors.move_to_end(author_id)
        while recent and now - recent[0][1] > self.window_seconds:
            recent.popleft()
        recent.append((digest, now))
        count = sum(1 for saved, _ in recent if saved == digest)
        if count < self.threshold:
            return None
        return Flag(
            rule=SPAM_RULE,
            score=min(1.0, 0.5 + 0.1 * (count - self.threshold + 1)),
            source="repeat",
            reason=f"{count} identical messages within {self.window_seconds} s",
        )

    def forget_expired(self, at: datetime) -> None:
        now = at.timestamp()
        for author_id in list(self._authors):
            recent = self._authors[author_id]
            while recent and now - recent[0][1] > self.window_seconds:
                recent.popleft()
            if not recent:
                del self._authors[author_id]


def evaluate(
    message: IntakeMessage, settings: ModerationSettings, tracker: RepeatTracker
) -> tuple[Flag, ...]:
    flags: list[Flag] = []
    repeat = tracker.observe(message.author_id, message.content, message.created_at)
    if repeat is not None:
        flags.append(repeat)
    invite = find_invites(message.content)
    if invite is not None:
        flags.append(invite)
    flags.extend(find_denied_links(message.content, settings.link_denylist))
    flags.extend(find_denied_attachments(message.attachment_names, settings.attachment_denylist))
    return tuple(flags)
