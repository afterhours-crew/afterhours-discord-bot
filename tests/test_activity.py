# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from datetime import UTC, datetime, timedelta

import pytest

from afterhours_bot.activity import ActivityCounter, ActivityMessage, MemberActivity, plan_roles
from afterhours_bot.config import ActiveRoleSettings

NOW = datetime(2026, 10, 30, 12, tzinfo=UTC)


def test_more_than_two_requires_three_distinct_messages_across_channels() -> None:
    counter = ActivityCounter(ActiveRoleSettings(99), as_of=NOW)
    for message_id in [1, 1, 2]:
        counter.add(ActivityMessage(message_id, 10, NOW))
    assert counter.qualifying_members() == frozenset()
    counter.add(ActivityMessage(3, 10, NOW))
    assert counter.qualifying_members() == frozenset({10})


@pytest.mark.parametrize(
    "offset,qualifies",
    [
        (timedelta(days=-14, microseconds=-1), False),
        (timedelta(days=-14), False),
        (timedelta(days=-14, microseconds=1), True),
        (timedelta(), True),
        (timedelta(microseconds=1), False),
    ],
)
def test_window_boundaries(offset: timedelta, qualifies: bool) -> None:
    counter = ActivityCounter(ActiveRoleSettings(99, min_messages=1), as_of=NOW)
    counter.add(ActivityMessage(1, 10, NOW + offset))
    assert (10 in counter.qualifying_members()) is qualifies


@pytest.mark.parametrize("excluded", ["is_bot", "is_webhook", "system"])
def test_only_ordinary_human_messages_count(excluded: str) -> None:
    counter = ActivityCounter(ActiveRoleSettings(99, min_messages=1), as_of=NOW)
    counter.add(
        ActivityMessage(
            1,
            10,
            NOW,
            is_bot=excluded == "is_bot",
            is_webhook=excluded == "is_webhook",
            is_user_message=excluded != "system",
        )
    )
    assert counter.qualifying_members() == frozenset()


def test_configurable_window_threshold_and_expiry() -> None:
    settings = ActiveRoleSettings(99, window_days=2, min_messages=2)
    earlier = ActivityCounter(settings, as_of=NOW)
    later = ActivityCounter(settings, as_of=NOW + timedelta(days=2))
    for counter in (earlier, later):
        for message_id in [1, 2]:
            counter.add(ActivityMessage(message_id, 10, NOW - timedelta(hours=1)))
    assert earlier.qualifying_members() == frozenset({10})
    assert later.qualifying_members() == frozenset()


def test_role_plan_keeps_bots_other_members_and_existing_roles_isolated() -> None:
    plan = plan_roles(
        [
            MemberActivity(1, False),
            MemberActivity(2, True),
            MemberActivity(3, True),
            MemberActivity(4, False),
            MemberActivity(5, True, is_bot=True),
            MemberActivity(6, False, is_bot=True),
        ],
        frozenset({1, 3, 6, 999}),
        complete=True,
    )
    assert plan.grant == (1,)
    assert plan.remove == (2,)


def test_partial_scans_cannot_plan_any_role_mutation() -> None:
    plan = plan_roles(
        [MemberActivity(1, True), MemberActivity(2, False)], frozenset({2}), complete=False
    )
    assert not plan.grant and not plan.remove


def test_counters_do_not_share_member_state() -> None:
    first = ActivityCounter(ActiveRoleSettings(99, min_messages=1), as_of=NOW)
    second = ActivityCounter(ActiveRoleSettings(99, min_messages=1), as_of=NOW)
    first.add(ActivityMessage(1, 10, NOW))
    assert first.qualifying_members() == frozenset({10})
    assert not second.qualifying_members()


def test_naive_clock_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ActivityCounter(ActiveRoleSettings(99), as_of=NOW.replace(tzinfo=None))
