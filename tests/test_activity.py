# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from afterhours_bot.activity import MemberActivity, plan_roles


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
