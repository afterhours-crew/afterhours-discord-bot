# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from discord import app_commands

from afterhours_bot.__main__ import main
from afterhours_bot.bot import CommunityBot
from afterhours_bot.config import ActiveRoleSettings, ModerationSettings, Settings
from afterhours_bot.rules import ModerationRulesError

RULES = Path(__file__).resolve().parents[1] / "rules" / "community-rules.md"
GUILD = discord.Object(id=123)


def settings(active_role: ActiveRoleSettings | None = None) -> Settings:
    return Settings(
        token="example-secret",  # noqa: S106
        guild_id=123,
        active_role=active_role,
        moderation=ModerationSettings(staff_channel_id=555, rules_path=RULES, queue_size=3),
    )


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


def test_moderation_requests_message_intents_and_nothing_else_new() -> None:
    async def scenario() -> None:
        async with CommunityBot(settings()) as bot:
            expected = discord.Intents(guilds=True, guild_messages=True, message_content=True)
            assert bot.intents.value == expected.value
            assert not bot.intents.presences
            assert not bot.intents.members
            assert not bot.intents.dm_messages
        async with CommunityBot(settings(ActiveRoleSettings(99))) as bot:
            assert bot.intents.members and bot.intents.message_content
        async with CommunityBot(Settings(token="example-secret", guild_id=123)) as bot:  # noqa: S106
            assert bot.intents.value == discord.Intents(guilds=True).value
            assert bot.moderation is None

    asyncio.run(scenario())


def test_moderation_command_is_staff_only_guild_scoped_and_private() -> None:
    async def scenario() -> None:
        async with CommunityBot(settings()) as bot:
            command = bot.tree.get_command("moderation", guild=GUILD)
            assert isinstance(command, app_commands.Command)
            assert command.guild_only
            assert command.default_permissions is not None
            assert command.default_permissions.manage_messages
            interaction = Mock()
            interaction.response.send_message = AsyncMock()
            await command.callback(interaction)
            interaction.response.send_message.assert_awaited_once()
            kwargs = interaction.response.send_message.call_args.kwargs
            assert kwargs["ephemeral"]
            text = interaction.response.send_message.call_args.args[0]
            assert "shadow mode" in text
            assert "<#555>" in text
            assert "example-secret" not in text

    asyncio.run(scenario())


def test_moderation_command_reports_when_the_feature_is_off() -> None:
    async def scenario() -> None:
        async with CommunityBot(Settings(token="example-secret", guild_id=123)) as bot:  # noqa: S106
            assert bot.tree.get_command("moderation", guild=GUILD) is not None
            assert (await bot.moderation_status()).startswith("Moderation is off")

    asyncio.run(scenario())


def test_setup_loads_rules_once_starts_one_worker_and_shutdown_cancels_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with CommunityBot(settings()) as bot:
            assert bot.moderation is not None
            await bot.setup_hook()
            await bot.setup_hook()
            assert bot.rules_revision == 1
            assert bot.rules_digest is not None and len(bot.rules_digest) == 64
            assert await bot.store.rules_revision(123) == (1, bot.rules_digest)
            task = bot._moderation_task
            assert task is not None and not task.done()
            status = await bot.moderation_status()
            assert "revision 1" in status and "Review worker: running" in status
            assert "Flags: 0 flagged" in status and "Audit: 0 stored flags" in status
            await bot.close()
            assert task.cancelled()
            assert bot._moderation_task is None

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Moderation rules loaded: revision 1" in caplog.text


def test_rule_revision_persists_across_restarts_and_changes_with_content(tmp_path: Path) -> None:
    rules = tmp_path / "rules.md"
    rules.write_text("# 1. Be respectful\n", encoding="utf-8")
    database = tmp_path / "state.sqlite3"
    config = Settings(
        token="example-secret",  # noqa: S106
        guild_id=123,
        database_path=database,
        moderation=ModerationSettings(staff_channel_id=555, rules_path=rules),
    )

    async def start() -> int | None:
        async with CommunityBot(config) as bot:
            await bot.setup_hook()
            return bot.rules_revision

    assert asyncio.run(start()) == 1
    assert asyncio.run(start()) == 1
    rules.write_text("# 1. Be respectful\n# 2. No piracy\n", encoding="utf-8")
    assert asyncio.run(start()) == 2


def test_messages_are_selected_and_queued_from_the_event_handler() -> None:
    def message(**overrides: object) -> discord.Message:
        fields: dict[str, object] = {
            "id": 1000,
            "guild": SimpleNamespace(id=123),
            "author": SimpleNamespace(id=42, bot=False),
            "webhook_id": None,
            "type": discord.MessageType.default,
            "channel": SimpleNamespace(id=10, parent_id=None),
            "created_at": datetime.now(UTC),
            "content": "hello",
            "jump_url": "https://discord.com/channels/123/10/1000",
            "attachments": [],
        }
        fields.update(overrides)
        return Mock(spec=discord.Message, **fields)

    async def scenario() -> None:
        async with CommunityBot(settings()) as bot:
            assert bot.moderation is not None
            await bot.on_message(message())
            await bot.on_message(message(author=SimpleNamespace(id=1, bot=True)))
            await bot.on_message(message(channel=SimpleNamespace(id=555, parent_id=None)))
            assert bot.moderation.counters.queued == 1
            assert bot.moderation.counters.submitted == 1
        async with CommunityBot(Settings(token="example-secret", guild_id=123)) as bot:  # noqa: S106
            await bot.on_message(message())  # No intake exists; the event is ignored.

    asyncio.run(scenario())


def test_missing_rule_file_fails_startup_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("MODERATION_STAFF_CHANNEL_ID", "555")
    monkeypatch.setenv("MODERATION_RULES_PATH", "missing/secret-rules.md")
    with (
        patch(
            "afterhours_bot.__main__.run_bot",
            new_callable=AsyncMock,
            side_effect=ModerationRulesError("The rule file does not exist."),
        ),
        caplog.at_level(logging.ERROR),
    ):
        assert main([]) == 1
    assert "Moderation rules could not be loaded" in caplog.text
    assert "does not exist" in caplog.text
    assert "secret-rules" not in caplog.text


def test_configuration_check_reads_rules_offline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("MODERATION_STAFF_CHANNEL_ID", "555")
    monkeypatch.setenv("MODERATION_RULES_PATH", str(RULES))
    with patch("afterhours_bot.__main__.run_bot", new_callable=AsyncMock) as run:
        with caplog.at_level(logging.INFO):
            assert main(["--check-config"]) == 0
        run.assert_not_awaited()
    assert "Moderation rules readable" in caplog.text
    assert "example-secret" not in caplog.text


def test_configuration_check_reports_an_unreadable_rule_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("MODERATION_STAFF_CHANNEL_ID", "555")
    monkeypatch.setenv("MODERATION_RULES_PATH", "missing.md")
    with pytest.raises(SystemExit) as error:
        main(["--check-config"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "env,expected",
    [
        ({}, "Enable Server Members Intent for Active roles in the Discord Developer Portal."),
        (
            {"MODERATION_STAFF_CHANNEL_ID": "555"},
            "Enable Server Members Intent for Active roles and Message Content Intent "
            "for moderation in the Discord Developer Portal.",
        ),
    ],
)
def test_missing_privileged_intent_names_each_required_portal_setting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("ACTIVE_ROLE_ID", "99")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with patch(
        "afterhours_bot.__main__.run_bot",
        new_callable=AsyncMock,
        side_effect=discord.PrivilegedIntentsRequired(None),
    ):
        assert main([]) == 1
    assert expected in caplog.text


def test_moderation_only_intent_advice_names_message_content(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("MODERATION_STAFF_CHANNEL_ID", "555")
    with patch(
        "afterhours_bot.__main__.run_bot",
        new_callable=AsyncMock,
        side_effect=discord.PrivilegedIntentsRequired(None),
    ):
        assert main([]) == 1
    assert "Enable Message Content Intent for moderation" in caplog.text
    assert "Server Members" not in caplog.text
