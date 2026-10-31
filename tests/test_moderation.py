# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import hashlib
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import discord
import pytest

from afterhours_bot.config import ConfigurationError, ModerationSettings, Settings
from afterhours_bot.moderation import (
    IntakeCounters,
    IntakeMessage,
    ModerationIntake,
    select_message,
)
from afterhours_bot.rules import ModerationRulesError, RuleSet, load_rules
from afterhours_bot.storage import ActivityStateError, ActivityStore

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
BASE_ENV = {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123"}
MODERATION = ModerationSettings(staff_channel_id=555, queue_size=2)
REPOSITORY_RULES = Path(__file__).resolve().parents[1] / "rules" / "community-rules.md"


# Configuration


def test_moderation_is_off_without_a_staff_channel() -> None:
    settings = Settings.from_env(BASE_ENV)
    assert settings.moderation is None


def test_staff_channel_enables_moderation_with_defaults() -> None:
    settings = Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555"})
    assert settings.moderation is not None
    assert settings.moderation.staff_channel_id == 555
    assert settings.moderation.rules_path == Path("rules/community-rules.md")
    assert settings.moderation.queue_size == 1000
    assert settings.moderation.link_denylist == ()
    assert settings.moderation.attachment_denylist  # Executables and scripts by default.


def test_moderation_rules_path_and_queue_size_are_configurable() -> None:
    settings = Settings.from_env(
        {
            **BASE_ENV,
            "MODERATION_STAFF_CHANNEL_ID": "555",
            "MODERATION_RULES_PATH": " custom/rules.md ",
            "MODERATION_QUEUE_SIZE": "7",
        }
    )
    assert settings.moderation is not None
    assert settings.moderation.rules_path == Path("custom/rules.md")
    assert settings.moderation.queue_size == 7


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODERATION_STAFF_CHANNEL_ID", "0"),
        ("MODERATION_STAFF_CHANNEL_ID", "not-an-id"),
        ("MODERATION_STAFF_CHANNEL_ID", "18446744073709551616"),
        ("MODERATION_RULES_PATH", ""),
        ("MODERATION_RULES_PATH", "bad\x00path"),
        ("MODERATION_QUEUE_SIZE", "0"),
        ("MODERATION_QUEUE_SIZE", "100001"),
        ("MODERATION_QUEUE_SIZE", "abc"),
    ],
)
def test_invalid_moderation_configuration_fails_without_echoing_values(
    name: str, value: str
) -> None:
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555", name: value})
    assert name in str(error.value)
    assert "example-secret" not in str(error.value)


# Rule file


def test_repository_rules_load_and_name_every_rule() -> None:
    rules = load_rules(REPOSITORY_RULES)
    assert len(rules.sha256) == 64
    assert rules.short_digest == rules.sha256[:12]
    for number in range(1, 10):
        assert f"# {number}." in rules.text


def test_rule_digest_ignores_line_endings_and_surrounding_whitespace(tmp_path: Path) -> None:
    unix, windows = tmp_path / "unix.md", tmp_path / "windows.md"
    unix.write_bytes(b"# 1. Be respectful\nArgue with ideas.\n")
    windows.write_bytes(b"\r\n# 1. Be respectful\r\nArgue with ideas.\r\n\r\n")
    assert load_rules(unix) == load_rules(windows)
    assert load_rules(unix).text == "# 1. Be respectful\nArgue with ideas.\n"


def test_rule_digest_changes_with_content(tmp_path: Path) -> None:
    path = tmp_path / "rules.md"
    path.write_text("# 1. Be respectful\n", encoding="utf-8")
    first = load_rules(path)
    path.write_text("# 1. Be respectful\n# 2. No piracy\n", encoding="utf-8")
    assert load_rules(path).sha256 != first.sha256


@pytest.mark.parametrize("content", [b"", b"   \n\n", b"\xff\xfe\x00bad"])
def test_empty_or_undecodable_rules_are_rejected(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "rules.md"
    path.write_bytes(content)
    with pytest.raises(ModerationRulesError):
        load_rules(path)


def test_missing_and_oversized_rule_files_are_rejected_without_the_path(tmp_path: Path) -> None:
    with pytest.raises(ModerationRulesError) as missing:
        load_rules(tmp_path / "secret-location.md")
    assert "secret-location" not in str(missing.value)
    large = tmp_path / "large.md"
    large.write_bytes(b"a" * 65)
    with pytest.raises(ModerationRulesError, match="exceeds"):
        load_rules(large, max_bytes=64)
    assert load_rules(large, max_bytes=65).text == "a" * 65 + "\n"


# Message selection


def message(**overrides: object) -> discord.Message:
    author = SimpleNamespace(id=42, bot=False)
    channel = SimpleNamespace(id=10, parent_id=None)
    fields: dict[str, object] = {
        "id": 1000,
        "guild": SimpleNamespace(id=123),
        "author": author,
        "webhook_id": None,
        "type": discord.MessageType.default,
        "channel": channel,
        "created_at": NOW,
        "content": "hello",
        "jump_url": "https://discord.com/channels/123/10/1000",
        "attachments": [
            SimpleNamespace(
                filename="photo.png",
                content_type="image/png",
                size=10,
                url="https://cdn.example/photo.png",
            )
        ],
    }
    fields.update(overrides)
    return Mock(spec=discord.Message, **fields)


def test_ordinary_member_messages_are_selected_with_their_identities() -> None:
    selected = select_message(message(), MODERATION, 123)
    assert selected == IntakeMessage(
        message_id=1000,
        channel_id=10,
        author_id=42,
        created_at=NOW,
        content="hello",
        jump_url="https://discord.com/channels/123/10/1000",
        attachment_names=("photo.png",),
        image_urls=("https://cdn.example/photo.png",),
    )


def test_replies_are_selected() -> None:
    assert select_message(message(type=discord.MessageType.reply), MODERATION, 123) is not None


@pytest.mark.parametrize(
    "overrides",
    [
        {"guild": None},
        {"guild": SimpleNamespace(id=456)},
        {"author": SimpleNamespace(id=42, bot=True)},
        {"webhook_id": 77},
        {"type": discord.MessageType.pins_add},
        {"type": discord.MessageType.new_member},
        {"channel": SimpleNamespace(id=555, parent_id=None)},
        {"channel": SimpleNamespace(id=900, parent_id=555)},
    ],
)
def test_out_of_scope_messages_are_skipped(overrides: dict[str, object]) -> None:
    assert select_message(message(**overrides), MODERATION, 123) is None


def test_channels_without_parents_are_handled() -> None:
    channel = SimpleNamespace(id=10)
    assert select_message(message(channel=channel), MODERATION, 123) is not None


# Intake queue


def intake_message(number: int) -> IntakeMessage:
    return IntakeMessage(number, 10, 42, NOW, f"sensitive text {number}")


def test_full_queue_drops_and_counts_without_logging_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        intake = ModerationIntake(MODERATION)
        assert intake.submit(intake_message(1))
        assert intake.submit(intake_message(2))
        assert not intake.submit(intake_message(3))
        assert not intake.submit(intake_message(4))
        assert intake.counters == IntakeCounters(submitted=4, queued=2, dropped=2)
        assert intake.counters.pending == 2

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    drop_lines = [record for record in caplog.records if "dropped" in record.getMessage()]
    assert len(drop_lines) == 1  # Repeated drops within the interval are not logged again.
    assert "sensitive text" not in caplog.text


def test_worker_reviews_in_order_survives_failures_and_cancels_cleanly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reviewed: list[int] = []
    finished = asyncio.Event()

    async def reviewer(item: IntakeMessage) -> None:
        if item.message_id == 2:
            raise RuntimeError(item.content)
        reviewed.append(item.message_id)
        if item.message_id == 3:
            finished.set()

    async def scenario() -> None:
        intake = ModerationIntake(ModerationSettings(staff_channel_id=555), reviewer=reviewer)
        for number in (1, 2, 3):
            assert intake.submit(intake_message(number))
        task = asyncio.create_task(intake.run())
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert reviewed == [1, 3]
        assert intake.counters == IntakeCounters(submitted=3, queued=3, reviewed=2, failed=1)
        assert intake.counters.pending == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert "RuntimeError" in caplog.text
    assert "sensitive text" not in caplog.text


def test_default_reviewer_discards_messages() -> None:
    async def scenario() -> None:
        intake = ModerationIntake(ModerationSettings(staff_channel_id=555))
        intake.submit(intake_message(1))
        task = asyncio.create_task(intake.run())
        for _ in range(50):
            if intake.counters.reviewed:
                break
            await asyncio.sleep(0.01)
        assert intake.counters.reviewed == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


# Rule revision storage


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ActivityStore]:
    instance = ActivityStore(tmp_path / "nightshift.sqlite3")
    yield instance
    asyncio.run(instance.close())


def ruleset(text: str) -> RuleSet:
    return RuleSet(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())


def test_rule_revision_increments_only_when_content_changes(store: ActivityStore) -> None:
    async def scenario() -> None:
        assert await store.rules_revision(123) is None
        first = ruleset("# 1. Be respectful\n")
        assert await store.configure_rules(123, first, as_of=NOW) == 1
        assert await store.configure_rules(123, first, as_of=NOW) == 1
        second = ruleset("# 1. Be respectful\n# 2. No piracy\n")
        assert await store.configure_rules(123, second, as_of=NOW) == 2
        assert await store.rules_revision(123) == (2, second.sha256)
        # Another server keeps its own revision.
        assert await store.configure_rules(456, first, as_of=NOW) == 1
        assert await store.rules_revision(123) == (2, second.sha256)

    asyncio.run(scenario())


def test_rule_revision_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "nightshift.sqlite3"
    rules = ruleset("# 1. Be respectful\n")

    async def first_run() -> None:
        store = ActivityStore(path)
        try:
            assert await store.configure_rules(123, rules, as_of=NOW) == 1
        finally:
            await store.close()

    async def second_run() -> None:
        store = ActivityStore(path)
        try:
            assert await store.rules_revision(123) == (1, rules.sha256)
            assert await store.configure_rules(123, rules, as_of=NOW) == 1
        finally:
            await store.close()

    asyncio.run(first_run())
    asyncio.run(second_run())


def test_rule_storage_is_independent_of_activity_configuration(store: ActivityStore) -> None:
    async def scenario() -> None:
        rules = ruleset("# 1. Be respectful\n")
        assert await store.configure_rules(123, rules, as_of=NOW) == 1
        assert await store.configure(123, None, as_of=NOW) == 1
        assert await store.rules_revision(123) == (1, rules.sha256)

    asyncio.run(scenario())


def test_corrupt_saved_rules_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "nightshift.sqlite3"

    async def prepare() -> None:
        store = ActivityStore(path)
        try:
            await store.configure_rules(123, ruleset("# 1.\n"), as_of=NOW)
        finally:
            await store.close()

    asyncio.run(prepare())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE configurations SET value_json='[]' WHERE key='moderation_rules'")

    async def check() -> None:
        store = ActivityStore(path)
        try:
            with pytest.raises(ActivityStateError):
                await store.rules_revision(123)
            with pytest.raises(ActivityStateError):
                await store.configure_rules(123, ruleset("# 1.\n"), as_of=NOW)
        finally:
            await store.close()

    asyncio.run(check())
