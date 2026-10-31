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

from afterhours_bot.config import DenyEntry, ModerationSettings
from afterhours_bot.filters import Flag
from afterhours_bot.moderation import IntakeMessage
from afterhours_bot.review import (
    LinkView,
    ReviewCounters,
    ReviewView,
    StaffReviewer,
    build_embed,
    excerpt,
    resolve_embed,
)
from afterhours_bot.storage import ActivityStateError, ActivityStore

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
SETTINGS = ModerationSettings(
    staff_channel_id=555,
    repeat_threshold=2,
    link_denylist=(DenyEntry("bad.example", 2),),
    attachment_denylist=(DenyEntry("exe", 2),),
    audit_retention_days=30,
    audit_max_rows=100,
    flags_per_minute=2,
)
FLAGS = (Flag(2, 0.9, "link-domain", "1 link to listed domain bad.example"),)


def intake(number: int, content: str | None = None) -> IntakeMessage:
    if content is None:
        content = f"see https://bad.example/secret-path-{number}"
    return IntakeMessage(
        number, 10, 42, NOW, content, f"https://discord.com/channels/123/10/{number}", ()
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ActivityStore]:
    instance = ActivityStore(tmp_path / "nightshift.sqlite3")
    yield instance
    asyncio.run(instance.close())


def staff_channel(*, send_messages: bool = True, guild_id: int = 123) -> Mock:
    channel = Mock(spec=discord.TextChannel)
    channel.id = 555
    channel.guild = SimpleNamespace(id=guild_id, me=object())
    channel.permissions_for = Mock(return_value=discord.Permissions(send_messages=send_messages))
    channel.send = AsyncMock(return_value=Mock(id=9001))
    return channel


def reviewer(
    store: ActivityStore, channel: Mock | None, *, settings: ModerationSettings = SETTINGS
) -> tuple[StaffReviewer, Mock]:
    client = Mock()
    client.get_channel = Mock(return_value=channel)
    client.fetch_channel = AsyncMock(return_value=channel)
    instance = StaffReviewer(
        client, settings, store, guild_id=123, rules_revision=lambda: 7, clock=lambda: NOW
    )
    return instance, client


def interaction(
    *, manage_messages: bool = True, post_id: int = 9001, user_id: int = 7, guild_id: int = 123
) -> Mock:
    result = Mock()
    result.guild_id = guild_id
    result.message = Mock(id=post_id, content="", embeds=[])
    result.permissions = discord.Permissions(manage_messages=manage_messages)
    result.user = Mock(id=user_id)
    result.response.send_message = AsyncMock()
    result.response.edit_message = AsyncMock()
    result.response.is_done = Mock(return_value=False)
    result.followup.send = AsyncMock()
    return result


# Post text


def test_post_embed_quotes_the_message_and_names_rule_reason_and_place() -> None:
    embed = build_embed(intake(1), FLAGS, 7, {2: "No piracy"})
    assert embed.title == "Rule 2 · No piracy"
    assert embed.url == "https://discord.com/channels/123/10/1"
    assert embed.description == "> see https://bad.example/secret-path-1"
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Author"] == "<@42>" and fields["Channel"] == "<#10>"
    assert fields["Confidence"] == "90% (0.90) · link-domain"
    assert fields["Why"] == "1 link to listed domain bad.example (rule 2, score 0.90, link-domain)"
    assert "Details" in fields
    detailed = build_embed(
        intake(1), FLAGS, 7, {}, flagged_at=NOW, models={"link-domain": "n/a", "llm": "qwen"}
    )
    details = {field.name: field.value for field in detailed.fields}["Details"].splitlines()
    assert details[0] == "Score 0.90 · source link-domain (n/a)"
    assert details[1] == "Message 1 · author 42 · channel 10"
    assert details[2].startswith("Sent <t:") and "flagged <t:" in details[2]
    assert details[3] == "Rules revision 7"
    assert embed.footer.text == "rules revision 7 · nothing done automatically"
    assert embed.color is not None and embed.color.value == 0xE74C3C
    hidden = build_embed(intake(1), FLAGS, None, {}, show_excerpt=False)
    assert hidden.description == "*(no text)*" and hidden.title == "Rule 2"
    assert hidden.footer.text is not None and hidden.footer.text.startswith("rules unknown")


def test_excerpt_is_quoted_capped_and_mention_safe() -> None:
    assert (
        excerpt("hi @everyone\n\nsecond <@123456789012345678>")
        == "> hi @\u200beveryone\n>\n> second <@\u200b123456789012345678>"
    )
    long = excerpt("x" * 2000)
    assert long.startswith("> ") and long.endswith("…") and len(long) <= 1003
    assert excerpt("   ") == ""


def test_resolve_embed_marks_the_decision_and_outcome() -> None:
    from afterhours_bot.actions import Outcome

    embed = resolve_embed(build_embed(intake(1), FLAGS, 7, {}), "dismiss", 7, None)
    assert embed.fields[-1].name == "Resolved" and embed.fields[-1].value == "Dismissed by <@7>"
    assert embed.color is not None and embed.color.value == 0x2ECC71
    acted = resolve_embed(build_embed(intake(1), FLAGS, 7, {}), "delete", 8, Outcome(True, "gone"))
    assert acted.fields[-1].value == "Delete by <@8>\ngone"
    assert acted.color is not None and acted.color.value == 0x3498DB
    failed = resolve_embed(build_embed(intake(1), FLAGS, 7, {}), "warn", 8, Outcome(False, "no"))
    assert failed.color is not None and failed.color.value == 0xD35400


# Posting and audit


def test_flagged_messages_are_posted_and_audited_without_content(
    store: ActivityStore, tmp_path: Path
) -> None:
    channel = staff_channel()

    async def scenario() -> None:
        instance, _ = reviewer(store, channel)
        await instance.review(intake(1, "nothing wrong here"))
        channel.send.assert_not_awaited()
        await instance.review(intake(2))
        channel.send.assert_awaited_once()
        kwargs = channel.send.call_args.kwargs
        assert isinstance(kwargs["view"], ReviewView)
        assert kwargs["silent"] is True
        assert kwargs["allowed_mentions"].to_dict() == {"parse": []}
        assert channel.send.call_args.args == ()
        assert "secret-path-2" in kwargs["embed"].description  # Quoted for staff, by design.
        links = [item for item in kwargs["view"].children if isinstance(item, discord.ui.Button)]
        assert links[-1].url == "https://discord.com/channels/123/10/2"
        assert instance.counters == ReviewCounters(flagged=1, posted=1)
        assert await store.flag_summary(123) == (1, 1)

    asyncio.run(scenario())
    with sqlite3.connect(tmp_path / "nightshift.sqlite3") as connection:
        rows = connection.execute(
            "SELECT message_id, source, channel_id, author_id, rule, score, reason, "
            "rules_revision, post_message_id, decision FROM review_flags"
        ).fetchall()
    assert rows == [("2", "link-domain", "10", "42", 2, 0.9, FLAGS[0].reason, 7, "9001", None)]
    dump = str(rows)
    assert "secret-path" not in dump and "nothing wrong" not in dump


def test_rate_limited_flags_stay_in_the_audit_and_log_once(
    store: ActivityStore, caplog: pytest.LogCaptureFixture
) -> None:
    channel = staff_channel()

    async def scenario() -> None:
        instance, _ = reviewer(store, channel)
        for number in range(1, 5):
            await instance.review(intake(number))
        assert channel.send.await_count == 2
        assert instance.counters == ReviewCounters(flagged=4, posted=2, suppressed=2)
        assert await store.flag_summary(123) == (4, 4)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert sum("rate limited" in record.getMessage() for record in caplog.records) == 1
    assert "secret-path" not in caplog.text


@pytest.mark.parametrize(
    "channel",
    [
        None,
        staff_channel(send_messages=False),
        staff_channel(guild_id=456),
        Mock(spec=discord.VoiceChannel),
    ],
)
def test_unusable_staff_channel_pauses_posting_with_one_warning(
    store: ActivityStore, caplog: pytest.LogCaptureFixture, channel: Mock | None
) -> None:
    async def scenario() -> None:
        instance, client = reviewer(store, channel)
        if channel is None:
            client.fetch_channel = AsyncMock(return_value=None)
        await instance.review(intake(1))
        await instance.review(intake(2))
        assert instance.posting_disabled
        assert instance.counters == ReviewCounters(flagged=2, unposted=2)
        assert await store.flag_summary(123) == (2, 2)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert sum("Review posts disabled" in record.getMessage() for record in caplog.records) == 1


def test_send_failure_pauses_posting_and_keeps_the_bot_running(
    store: ActivityStore, caplog: pytest.LogCaptureFixture
) -> None:
    channel = staff_channel()
    response = SimpleNamespace(status=403, reason="Forbidden")
    channel.send = AsyncMock(side_effect=discord.Forbidden(response, "secret-detail"))  # type: ignore[arg-type]

    async def scenario() -> None:
        instance, _ = reviewer(store, channel)
        await instance.review(intake(1))
        await instance.review(intake(2))
        channel.send.assert_awaited_once()
        assert instance.counters == ReviewCounters(flagged=2, unposted=2)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert "Forbidden" in caplog.text
    assert "secret-detail" not in caplog.text


# Decisions


def test_dismiss_records_the_decision_and_removes_the_buttons(store: ActivityStore) -> None:
    channel = staff_channel()

    async def scenario() -> None:
        instance, _ = reviewer(store, channel)
        await instance.review(intake(1))
        click = interaction()
        await instance.decide(click, "dismiss")
        click.response.edit_message.assert_awaited_once()
        kwargs = click.response.edit_message.call_args.kwargs
        assert isinstance(kwargs["view"], LinkView)
        assert kwargs["embed"].fields[-1].value == "Dismissed by <@7>"
        click.followup.send.assert_not_awaited()
        assert instance.counters.decisions == 1
        assert await store.flag_summary(123) == (1, 0)

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["delete", "warn", "timeout"])
def test_other_decisions_are_recorded_and_report_when_the_action_cannot_run(
    store: ActivityStore, decision: str
) -> None:
    channel = staff_channel()

    async def scenario() -> None:
        instance, client = reviewer(store, channel)
        client.get_guild = Mock(return_value=None)  # No server available to act in.
        await instance.review(intake(1))
        click = interaction(manage_messages=True)
        click.permissions = discord.Permissions(manage_messages=True, moderate_members=True)
        await instance.decide(click, decision)
        click.response.edit_message.assert_awaited_once()
        resolved = click.response.edit_message.call_args.kwargs["embed"].fields[-1].value
        assert resolved == f"{decision.capitalize()} by <@7>\nthe server is unavailable"
        click.followup.send.assert_awaited_once_with(
            "Not done: the server is unavailable.", ephemeral=True
        )
        assert await store.flag_summary(123) == (1, 0)

    asyncio.run(scenario())


def test_second_decision_reports_the_first_without_overwriting(store: ActivityStore) -> None:
    channel = staff_channel()

    async def scenario() -> None:
        instance, _ = reviewer(store, channel)
        await instance.review(intake(1))
        await instance.decide(interaction(user_id=7), "dismiss")
        second = interaction(user_id=8)
        await instance.decide(second, "delete")
        second.response.edit_message.assert_not_awaited()
        second.response.send_message.assert_awaited_once()
        assert (
            "Already resolved as dismiss by <@7>" in second.response.send_message.call_args.args[0]
        )
        assert second.response.send_message.call_args.kwargs["ephemeral"]
        assert instance.counters.decisions == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "click",
    [interaction(manage_messages=False), interaction(guild_id=456)],
)
def test_non_staff_and_foreign_clicks_do_nothing(store: ActivityStore, click: Mock) -> None:
    channel = staff_channel()

    async def scenario() -> None:
        instance, _ = reviewer(store, channel)
        await instance.review(intake(1))
        await instance.decide(click, "dismiss")
        click.response.edit_message.assert_not_awaited()
        click.response.send_message.assert_awaited_once()
        assert click.response.send_message.call_args.kwargs["ephemeral"]
        assert await store.flag_summary(123) == (1, 1)

    asyncio.run(scenario())


def test_unknown_post_and_unknown_decision_are_rejected(store: ActivityStore) -> None:
    async def scenario() -> None:
        instance, _ = reviewer(store, staff_channel())
        click = interaction(post_id=4242)
        await instance.decide(click, "dismiss")
        click.response.send_message.assert_awaited_once()
        assert "no longer in the audit" in click.response.send_message.call_args.args[0]
        with pytest.raises(ValueError, match="Unknown review decision"):
            await instance.decide(interaction(), "ban")

    asyncio.run(scenario())


def test_review_view_is_persistent_with_stable_custom_ids(store: ActivityStore) -> None:
    async def scenario() -> None:
        instance, _ = reviewer(store, staff_channel())
        view = ReviewView(instance)
        assert view.is_persistent()
        ids = [item.custom_id for item in view.children if isinstance(item, discord.ui.Button)]
        assert ids == [
            f"nightshift:review:{name}" for name in ("dismiss", "delete", "warn", "timeout")
        ]

    asyncio.run(scenario())


def test_view_errors_reply_privately_without_details(
    store: ActivityStore, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        instance, _ = reviewer(store, staff_channel())
        view = ReviewView(instance)
        click = interaction()
        await view.on_error(click, RuntimeError("secret-detail"), Mock())
        click.response.send_message.assert_awaited_once()
        assert click.response.send_message.call_args.kwargs["ephemeral"]
        assert "secret-detail" not in str(click.response.send_message.call_args)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert "RuntimeError" in caplog.text and "secret-detail" not in caplog.text


# Audit storage


def test_audit_rows_expire_and_are_capped(store: ActivityStore) -> None:
    async def record(number: int, as_of: datetime, *, max_rows: int = 100) -> None:
        await store.record_flags(
            123,
            message_id=number,
            channel_id=10,
            author_id=42,
            flags=FLAGS,
            rules_revision=1,
            post_message_id=None,
            as_of=as_of,
            retention_days=1,
            max_rows=max_rows,
        )

    async def scenario() -> None:
        await record(1, NOW)
        await record(2, NOW + timedelta(hours=1))
        assert await store.flag_summary(123) == (2, 2)
        await record(3, NOW + timedelta(days=1, minutes=1))  # Row 1 has expired.
        assert await store.flag_summary(123) == (2, 2)
        for number in range(4, 10):
            await record(number, NOW + timedelta(days=1, minutes=number), max_rows=3)
        assert await store.flag_summary(123) == (3, 3)

    asyncio.run(scenario())


def test_audit_updates_the_post_id_once_and_isolates_servers(store: ActivityStore) -> None:
    async def scenario() -> None:
        common = dict(
            message_id=1,
            channel_id=10,
            author_id=42,
            flags=FLAGS,
            rules_revision=1,
            as_of=NOW,
            retention_days=30,
            max_rows=100,
        )
        await store.record_flags(123, post_message_id=None, **common)  # type: ignore[arg-type]
        await store.record_flags(123, post_message_id=9001, **common)  # type: ignore[arg-type]
        await store.record_flags(456, post_message_id=9002, **common)  # type: ignore[arg-type]
        assert await store.flag_summary(123) == (1, 1)
        assert await store.flag_summary(456) == (1, 1)
        decision = await store.record_decision(
            123, 9001, decision="dismiss", decider_id=7, as_of=NOW
        )
        assert decision is not None
        assert (decision.flags, decision.decision, decision.decider_id) == (1, "dismiss", 7)
        assert not decision.already_decided
        assert (decision.message_id, decision.channel_id, decision.author_id) == (1, 10, 42)
        assert decision.rules == (2,)
        assert (
            await store.record_decision(456, 9001, decision="dismiss", decider_id=7, as_of=NOW)
            is None
        )
        assert await store.flag_summary(456) == (1, 1)
        with pytest.raises(ActivityStateError):
            await store.record_decision(123, 9001, decision="x" * 33, decider_id=7, as_of=NOW)

    asyncio.run(scenario())


def test_schema_version_one_databases_are_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE configurations (guild_id TEXT NOT NULL, key TEXT NOT NULL, "
            "value_json TEXT NOT NULL, revision INTEGER NOT NULL, updated_us INTEGER NOT NULL, "
            "PRIMARY KEY (guild_id, key))"
        )
        connection.execute(
            "CREATE TABLE scan_positions (guild_id TEXT NOT NULL, source_id TEXT NOT NULL, "
            "cursor_id TEXT NOT NULL, scanned_at_us INTEGER NOT NULL, "
            "PRIMARY KEY (guild_id, source_id))"
        )
        connection.execute(
            "CREATE TABLE activity (guild_id TEXT NOT NULL, source_id TEXT NOT NULL, "
            "message_id TEXT NOT NULL, author_id TEXT NOT NULL, created_us INTEGER NOT NULL, "
            "expires_us INTEGER NOT NULL, PRIMARY KEY (guild_id, message_id))"
        )
        connection.execute("INSERT INTO configurations VALUES ('123', 'active_role', 'null', 1, 5)")
        connection.execute("PRAGMA user_version=1")

    async def scenario() -> None:
        store = ActivityStore(path)
        try:
            assert await store.flag_summary(123) == (0, 0)
            assert await store.configure(123, None, as_of=NOW) == 1  # Existing row preserved.
        finally:
            await store.close()

    asyncio.run(scenario())
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
