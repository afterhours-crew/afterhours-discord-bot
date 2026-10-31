# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from afterhours_bot.actions import ActionExecutor, ActionTarget, Outcome
from afterhours_bot.config import ConfigurationError, DenyEntry, ModerationSettings, Settings
from afterhours_bot.moderation import IntakeMessage
from afterhours_bot.review import StaffReviewer
from afterhours_bot.rules import load_rules
from afterhours_bot.storage import ActivityStore

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "rules" / "community-rules.md")
BASE_ENV = {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123"}
SETTINGS = ModerationSettings(
    staff_channel_id=555,
    repeat_threshold=50,
    link_denylist=(DenyEntry("bad.example", 2),),
    timeout_minutes=15,
    button_expiry_hours=2,
)
TARGET = ActionTarget(message_id=1000, channel_id=10, author_id=42, rules=(2,))
V2_SCHEMA = """
CREATE TABLE configurations (guild_id TEXT NOT NULL, key TEXT NOT NULL,
    value_json TEXT NOT NULL, revision INTEGER NOT NULL, updated_us INTEGER NOT NULL,
    PRIMARY KEY (guild_id, key));
CREATE TABLE scan_positions (guild_id TEXT NOT NULL, source_id TEXT NOT NULL,
    cursor_id TEXT NOT NULL, scanned_at_us INTEGER NOT NULL, PRIMARY KEY (guild_id, source_id));
CREATE TABLE activity (guild_id TEXT NOT NULL, source_id TEXT NOT NULL, message_id TEXT NOT NULL,
    author_id TEXT NOT NULL, created_us INTEGER NOT NULL, expires_us INTEGER NOT NULL,
    PRIMARY KEY (guild_id, message_id));
CREATE TABLE review_flags (guild_id TEXT NOT NULL, message_id TEXT NOT NULL, source TEXT NOT NULL,
    channel_id TEXT NOT NULL, author_id TEXT NOT NULL, rule INTEGER NOT NULL, score REAL NOT NULL,
    reason TEXT NOT NULL, rules_revision INTEGER, post_message_id TEXT, created_us INTEGER NOT NULL,
    expires_us INTEGER NOT NULL, decision TEXT, decider_id TEXT, decided_us INTEGER,
    PRIMARY KEY (guild_id, message_id, source));
INSERT INTO review_flags VALUES ('123', '1', 'invite', '10', '42', 6, 0.6, 'r', 1, '9001', 1,
    9999999999999999, NULL, NULL, NULL);
"""


def http_error(
    status: int, cls: type[discord.HTTPException] = discord.HTTPException
) -> discord.HTTPException:
    return cls(SimpleNamespace(status=status, reason="x"), "secret-detail")  # type: ignore[arg-type]


def member(*, bot: bool = False, admin: bool = False, top_role: int = 1) -> Mock:
    result = Mock(spec=discord.Member)
    result.bot = bot
    result.guild_permissions = discord.Permissions(administrator=admin)
    result.top_role = top_role
    result.send = AsyncMock()
    result.timeout = AsyncMock()
    return result


def guild(
    *, channel: Mock | None, target_member: Mock | None, bot_perms: discord.Permissions
) -> Mock:
    result = Mock(spec=discord.Guild)
    result.unavailable = False
    result.name = "Afterhours"
    me = Mock(spec=discord.Member)
    me.guild_permissions = bot_perms
    me.top_role = 5
    result.me = me
    result.get_channel_or_thread = Mock(return_value=channel)
    result.get_member = Mock(return_value=target_member)
    result.fetch_member = AsyncMock(
        side_effect=http_error(404, discord.NotFound) if target_member is None else None,
        return_value=target_member,
    )
    return result


def channel(*, manage_messages: bool = True) -> Mock:
    result = Mock(spec=discord.TextChannel)
    result.permissions_for = Mock(return_value=discord.Permissions(manage_messages=manage_messages))
    message = Mock(spec=discord.Message)
    message.delete = AsyncMock()
    result.fetch_message = AsyncMock(return_value=message)
    return result


def executor(guild_mock: Mock | None) -> ActionExecutor:
    client = Mock()
    client.get_guild = Mock(return_value=guild_mock)
    client.fetch_channel = AsyncMock(side_effect=http_error(404, discord.NotFound))
    return ActionExecutor(client, SETTINGS, guild_id=123, rule_titles=lambda: RULES.titles)


# Configuration


def test_action_settings_have_defaults_and_validate_the_template() -> None:
    settings = Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555"})
    assert settings.moderation is not None
    assert settings.moderation.timeout_minutes == 60
    assert settings.moderation.button_expiry_hours == 72
    assert "{rule}" in settings.moderation.warn_template
    custom = Settings.from_env(
        {
            **BASE_ENV,
            "MODERATION_STAFF_CHANNEL_ID": "555",
            "MODERATION_TIMEOUT_MINUTES": "1440",
            "MODERATION_BUTTON_EXPIRY_HOURS": "24",
            "MODERATION_WARN_TEMPLATE": "Rule {rule} ({title}) in {channel} on {server}.",
        }
    )
    assert custom.moderation is not None
    assert custom.moderation.timeout_minutes == 1440
    assert custom.moderation.button_expiry_hours == 24
    assert custom.moderation.warn_template.startswith("Rule {rule}")


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODERATION_TIMEOUT_MINUTES", "0"),
        ("MODERATION_TIMEOUT_MINUTES", "40321"),
        ("MODERATION_BUTTON_EXPIRY_HOURS", "8761"),
        ("MODERATION_WARN_TEMPLATE", "Hello {member}"),
        ("MODERATION_WARN_TEMPLATE", "Hello {rule"),
        ("MODERATION_WARN_TEMPLATE", "x" * 1501),
    ],
)
def test_invalid_action_settings_are_rejected(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555", name: value})
    assert name in str(error.value)


def test_rule_titles_come_from_headings() -> None:
    titles = RULES.titles
    assert titles[2] == "No piracy"
    assert titles[5] == "No NSFW content"
    assert titles[9].startswith("Follow Discord's Terms")


# Delete


def test_delete_removes_the_message_when_the_bot_may() -> None:
    chan = channel()

    async def scenario() -> Outcome:
        return await executor(
            guild(channel=chan, target_member=member(), bot_perms=discord.Permissions())
        ).delete(TARGET)

    outcome = asyncio.run(scenario())
    assert outcome == Outcome(True, "message deleted")
    chan.fetch_message.assert_awaited_once_with(1000)


@pytest.mark.parametrize(
    "error,expected",
    [
        (http_error(404, discord.NotFound), Outcome(True, "the message was already gone")),
        (
            http_error(403, discord.Forbidden),
            Outcome(False, "Discord refused the deletion (permissions)"),
        ),
        (http_error(500), Outcome(False, "Discord did not accept the deletion; try again later")),
    ],
)
def test_delete_reports_discord_errors_without_details(
    error: discord.HTTPException, expected: Outcome, caplog: pytest.LogCaptureFixture
) -> None:
    chan = channel()
    chan.fetch_message = AsyncMock(side_effect=error)

    async def scenario() -> Outcome:
        return await executor(
            guild(channel=chan, target_member=None, bot_perms=discord.Permissions())
        ).delete(TARGET)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(scenario()) == expected
    assert "secret-detail" not in caplog.text


def test_delete_requires_bot_permission_channel_and_server() -> None:
    async def scenario() -> list[Outcome]:
        return [
            await executor(
                guild(
                    channel=channel(manage_messages=False),
                    target_member=None,
                    bot_perms=discord.Permissions(),
                )
            ).delete(TARGET),
            await executor(
                guild(channel=None, target_member=None, bot_perms=discord.Permissions())
            ).delete(TARGET),
            await executor(None).delete(TARGET),
        ]

    outcomes = asyncio.run(scenario())
    assert [outcome.ok for outcome in outcomes] == [False, False, False]
    assert "Manage Messages" in outcomes[0].summary
    assert "channel" in outcomes[1].summary
    assert "server" in outcomes[2].summary


# Warn


def test_warn_sends_the_template_with_rule_title_and_channel() -> None:
    target_member = member()

    async def scenario() -> Outcome:
        return await executor(
            guild(channel=None, target_member=target_member, bot_perms=discord.Permissions())
        ).warn(TARGET)

    assert asyncio.run(scenario()) == Outcome(True, "warning sent by direct message")
    text = target_member.send.call_args.args[0]
    assert "Afterhours" in text and "<#10>" in text and "rule 2 (No piracy)" in text
    assert target_member.send.call_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}


def test_warn_handles_closed_dms_missing_members_and_bots() -> None:
    closed = member()
    closed.send = AsyncMock(side_effect=http_error(403, discord.Forbidden))

    async def scenario() -> list[Outcome]:
        return [
            await executor(
                guild(channel=None, target_member=closed, bot_perms=discord.Permissions())
            ).warn(TARGET),
            await executor(
                guild(channel=None, target_member=None, bot_perms=discord.Permissions())
            ).warn(TARGET),
            await executor(
                guild(channel=None, target_member=member(bot=True), bot_perms=discord.Permissions())
            ).warn(TARGET),
        ]

    outcomes = asyncio.run(scenario())
    assert [outcome.ok for outcome in outcomes] == [False, False, False]
    assert "direct messages" in outcomes[0].summary
    assert "no longer" in outcomes[1].summary
    assert "bot" in outcomes[2].summary


# Timeout


def test_timeout_applies_the_configured_duration() -> None:
    target_member = member()

    async def scenario() -> Outcome:
        g = guild(
            channel=None,
            target_member=target_member,
            bot_perms=discord.Permissions(moderate_members=True),
        )
        return await executor(g).timeout(TARGET)

    assert asyncio.run(scenario()) == Outcome(True, "timed out for 15 minutes")
    assert target_member.timeout.call_args.args[0] == timedelta(minutes=15)
    assert "Nightshift" in target_member.timeout.call_args.kwargs["reason"]


def test_timeout_checks_bot_permission_role_order_and_targets() -> None:
    async def scenario() -> list[Outcome]:
        can = discord.Permissions(moderate_members=True)
        refused = member()
        refused.timeout = AsyncMock(side_effect=http_error(403, discord.Forbidden))
        return [
            await executor(
                guild(channel=None, target_member=member(), bot_perms=discord.Permissions())
            ).timeout(TARGET),
            await executor(
                guild(channel=None, target_member=member(top_role=9), bot_perms=can)
            ).timeout(TARGET),
            await executor(
                guild(channel=None, target_member=member(admin=True), bot_perms=can)
            ).timeout(TARGET),
            await executor(guild(channel=None, target_member=None, bot_perms=can)).timeout(TARGET),
            await executor(guild(channel=None, target_member=refused, bot_perms=can)).timeout(
                TARGET
            ),
        ]

    outcomes = asyncio.run(scenario())
    assert all(not outcome.ok for outcome in outcomes)
    assert "Moderate Members" in outcomes[0].summary
    assert "roles" in outcomes[1].summary
    assert "cannot be timed out" in outcomes[2].summary
    assert "no longer" in outcomes[3].summary
    assert "refused" in outcomes[4].summary


# Buttons end to end


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ActivityStore]:
    instance = ActivityStore(tmp_path / "nightshift.sqlite3")
    yield instance
    asyncio.run(instance.close())


def staff_channel() -> Mock:
    result = Mock(spec=discord.TextChannel)
    result.id = 555
    result.guild = SimpleNamespace(id=123, me=object())
    result.permissions_for = Mock(return_value=discord.Permissions(send_messages=True))
    result.send = AsyncMock(return_value=Mock(id=9001))
    return result


def interaction(*, permissions: discord.Permissions, user_id: int = 7) -> Mock:
    result = Mock()
    result.guild_id = 123
    result.message = Mock(id=9001, content="**Review needed**")
    result.permissions = permissions
    result.user = Mock(id=user_id)
    result.response.send_message = AsyncMock()
    result.response.edit_message = AsyncMock()
    result.followup.send = AsyncMock()
    return result


def reviewer(store: ActivityStore, *, clock: datetime = NOW) -> tuple[StaffReviewer, Mock, Mock]:
    chan = staff_channel()
    target_member = member()
    g = guild(
        channel=channel(),
        target_member=target_member,
        bot_perms=discord.Permissions(moderate_members=True),
    )
    client = Mock()
    client.get_channel = Mock(return_value=chan)
    client.get_guild = Mock(return_value=g)
    review = StaffReviewer(
        client,
        SETTINGS,
        store,
        guild_id=123,
        rules_revision=lambda: 1,
        clock=lambda: clock,
        rule_titles=lambda: RULES.titles,
    )
    return review, g, target_member


def flagged(number: int = 1) -> IntakeMessage:
    return IntakeMessage(
        number,
        10,
        42,
        NOW,
        "see https://bad.example/x",
        "https://discord.com/channels/123/10/1",
        (),
    )


def test_delete_button_deletes_records_the_outcome_and_edits_the_post(
    store: ActivityStore, tmp_path: Path
) -> None:
    async def scenario() -> None:
        review, g, _ = reviewer(store)
        await review.review(flagged())
        click = interaction(permissions=discord.Permissions(manage_messages=True))
        await review.decide(click, "delete")
        g.get_channel_or_thread.return_value.fetch_message.return_value.delete.assert_awaited_once()
        content = click.response.edit_message.call_args.kwargs["content"]
        assert content.endswith("**Resolved:** delete by <@7> — message deleted")
        click.followup.send.assert_awaited_once_with("Done: message deleted.", ephemeral=True)
        assert review.counters.decisions == 1 and review.counters.actions == 1

    asyncio.run(scenario())
    with sqlite3.connect(tmp_path / "nightshift.sqlite3") as connection:
        row = connection.execute(
            "SELECT decision, decider_id, outcome FROM review_flags"
        ).fetchone()
    assert row == ("delete", "7", "message deleted")


def test_warn_and_timeout_buttons_act_and_report(store: ActivityStore) -> None:
    async def scenario() -> None:
        review, _, target_member = reviewer(store)
        await review.review(flagged(1))
        warn = interaction(permissions=discord.Permissions(manage_messages=True))
        await review.decide(warn, "warn")
        target_member.send.assert_awaited_once()
        warn.followup.send.assert_awaited_once_with(
            "Done: warning sent by direct message.", ephemeral=True
        )
        review.client.get_channel.return_value.send.return_value = Mock(id=9002)
        await review.review(flagged(2))
        timeout = interaction(permissions=discord.Permissions(moderate_members=True))
        timeout.message = Mock(id=9002, content="post")
        await review.decide(timeout, "timeout")
        target_member.timeout.assert_awaited_once()
        assert "Done: timed out for 15 minutes." in timeout.followup.send.call_args.args[0]

    asyncio.run(scenario())


def test_timeout_button_needs_moderate_members_and_failures_are_reported(
    store: ActivityStore,
) -> None:
    async def scenario() -> None:
        review, _, target_member = reviewer(store)
        target_member.send = AsyncMock(side_effect=http_error(403, discord.Forbidden))
        await review.review(flagged())
        denied = interaction(permissions=discord.Permissions(manage_messages=True))
        await review.decide(denied, "timeout")
        denied.response.edit_message.assert_not_awaited()
        assert "Moderate Members" in denied.response.send_message.call_args.args[0]
        warn = interaction(permissions=discord.Permissions(manage_messages=True))
        await review.decide(warn, "warn")
        content = warn.response.edit_message.call_args.kwargs["content"]
        assert "— the member does not accept direct messages" in content
        warn.followup.send.assert_awaited_once_with(
            "Not done: the member does not accept direct messages.", ephemeral=True
        )
        assert review.counters.actions == 0
        again = interaction(permissions=discord.Permissions(manage_messages=True), user_id=8)
        await review.decide(again, "delete")
        text = again.response.send_message.call_args.args[0]
        assert (
            "Already resolved as warn by <@7> (the member does not accept direct messages)" in text
        )

    asyncio.run(scenario())


def test_expired_buttons_do_not_act(store: ActivityStore) -> None:
    async def scenario() -> None:
        review, g, _ = reviewer(store)
        await review.review(flagged())
        review.clock = lambda: NOW + timedelta(hours=3)
        click = interaction(permissions=discord.Permissions(manage_messages=True))
        await review.decide(click, "delete")
        g.get_channel_or_thread.return_value.fetch_message.assert_not_awaited()
        assert (
            "buttons expire after 2 hours"
            in click.response.edit_message.call_args.kwargs["content"]
        )
        late_dismiss = reviewer(store)[0]
        late_dismiss.clock = lambda: NOW + timedelta(hours=3)
        await late_dismiss.review(flagged(2))

    asyncio.run(scenario())


def test_dismiss_still_resolves_without_an_action(store: ActivityStore) -> None:
    async def scenario() -> None:
        review, g, target_member = reviewer(store)
        await review.review(flagged())
        click = interaction(permissions=discord.Permissions(manage_messages=True))
        await review.decide(click, "dismiss")
        click.followup.send.assert_not_awaited()
        target_member.send.assert_not_awaited()
        g.get_channel_or_thread.return_value.fetch_message.assert_not_awaited()
        assert click.response.edit_message.call_args.kwargs["content"].endswith("dismiss by <@7>")

    asyncio.run(scenario())


def test_schema_version_two_databases_gain_the_outcome_column(tmp_path: Path) -> None:
    path = tmp_path / "v2.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(V2_SCHEMA)
        connection.execute("PRAGMA user_version=2")

    async def scenario() -> None:
        store = ActivityStore(path)
        try:
            decision = await store.record_decision(
                123, 9001, decision="dismiss", decider_id=7, as_of=NOW
            )
            assert decision is not None and decision.rules == (6,) and decision.message_id == 1
            assert await store.record_outcome(123, 9001, "dismissed") == 1
            assert await store.record_outcome(123, 4242, "x") == 0
        finally:
            await store.close()

    asyncio.run(scenario())
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT outcome FROM review_flags").fetchone()[0] == "dismissed"
