# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Rolling activity policy using only message identities and timestamps."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from afterhours_bot.config import ActiveRoleSettings


@dataclass(frozen=True)
class ActivityMessage:
    message_id: int
    author_id: int
    created_at: datetime
    is_bot: bool = False
    is_webhook: bool = False
    is_user_message: bool = True


class ActivityCounter:
    def __init__(self, settings: ActiveRoleSettings, *, as_of: datetime) -> None:
        if as_of.utcoffset() is None:
            raise ValueError("Activity time must include a timezone.")
        self.as_of = as_of
        self.cutoff = as_of - timedelta(days=settings.window_days)
        self.minimum = settings.min_messages
        self._messages: dict[int, set[int]] = {}

    def add(self, message: ActivityMessage) -> None:
        if (
            message.is_bot
            or message.is_webhook
            or not message.is_user_message
            or not self.cutoff < message.created_at <= self.as_of
        ):
            return
        messages = self._messages.setdefault(message.author_id, set())
        # Saturation bounds storage while retaining duplicate protection below the threshold.
        if len(messages) < self.minimum:
            messages.add(message.message_id)

    def qualifying_members(self) -> frozenset[int]:
        return frozenset(
            author_id
            for author_id, messages in self._messages.items()
            if len(messages) >= self.minimum
        )


@dataclass(frozen=True)
class MemberActivity:
    member_id: int
    has_role: bool
    is_bot: bool = False


@dataclass(frozen=True)
class RolePlan:
    grant: tuple[int, ...] = ()
    remove: tuple[int, ...] = ()


def plan_roles(
    members: Iterable[MemberActivity], qualified: frozenset[int], *, complete: bool
) -> RolePlan:
    if not complete:
        return RolePlan()
    grant, remove = [], []
    for member in members:
        if member.is_bot:
            continue
        if member.member_id in qualified and not member.has_role:
            grant.append(member.member_id)
        elif member.member_id not in qualified and member.has_role:
            remove.append(member.member_id)
    return RolePlan(grant=tuple(sorted(grant)), remove=tuple(sorted(remove)))
