# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import logging
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from discord import app_commands

from afterhours_bot.__main__ import main
from afterhours_bot.activity_service import ActivityResult
from afterhours_bot.bot import CommunityBot
from afterhours_bot.config import ActiveRoleSettings, Settings

SETTINGS = Settings(token="example-secret", guild_id=123)  # noqa: S106


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


def test_commands_are_guild_scoped_and_privileged_intents_are_disabled() -> None:
    async def scenario() -> None:
        async with CommunityBot(SETTINGS) as bot:
            assert bot.tree.get_commands() == []
            commands = bot.tree.get_commands(guild=discord.Object(id=123))
            assert {command.name for command in commands} == {
                "ping",
                "about",
                "moderation",
                "rules",
            }
            assert all(command.guild_only for command in commands)
            assert bot.intents.value == discord.Intents(guilds=True).value
            assert bot.allowed_mentions is not None
            assert bot.allowed_mentions.to_dict() == {"parse": []}
            assert bot.allowed_mentions.replied_user is False

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["ping", "about"])
def test_starter_commands_reply_privately(name: str) -> None:
    async def scenario() -> None:
        async with CommunityBot(SETTINGS) as bot:
            command = bot.tree.get_command(name, guild=discord.Object(id=123))
            assert isinstance(command, app_commands.Command)
            interaction = Mock()
            interaction.response.send_message = AsyncMock()
            await command.callback(interaction)
            interaction.response.send_message.assert_awaited_once()
            assert interaction.response.send_message.call_args.kwargs["ephemeral"]

    asyncio.run(scenario())


@pytest.mark.parametrize("sync_commands", [False, True])
def test_registration_is_explicit_and_only_targets_the_configured_guild(
    sync_commands: bool,
) -> None:
    async def scenario() -> None:
        async with CommunityBot(SETTINGS, sync_commands=sync_commands) as bot:
            with patch.object(bot.tree, "sync", new_callable=AsyncMock, return_value=[]) as sync:
                await bot.setup_hook()
                if sync_commands:
                    sync.assert_awaited_once_with(guild=discord.Object(id=123))
                else:
                    sync.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("guild_id,allowed", [(None, False), (456, False), (123, True)])
def test_commands_reject_dms_and_other_servers(guild_id: int | None, allowed: bool) -> None:
    async def scenario() -> None:
        async with CommunityBot(SETTINGS) as bot:
            interaction = Mock(guild_id=guild_id)
            interaction.response.send_message = AsyncMock()
            assert await bot.tree.interaction_check(interaction) is allowed
            if not allowed:
                interaction.response.send_message.assert_awaited_once()
                assert interaction.response.send_message.call_args.kwargs["ephemeral"]
            else:
                interaction.response.send_message.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("already_responded", [False, True])
def test_errors_reply_privately_and_do_not_log_details(
    already_responded: bool, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with CommunityBot(SETTINGS) as bot:
            interaction = Mock()
            interaction.response.is_done.return_value = already_responded
            interaction.response.send_message = AsyncMock()
            interaction.followup.send = AsyncMock()
            await bot.tree.on_error(interaction, app_commands.AppCommandError("example-secret"))
            responder = (
                interaction.followup.send
                if already_responded
                else interaction.response.send_message
            )
            responder.assert_awaited_once()
            assert responder.call_args.kwargs["ephemeral"]
            assert "example-secret" not in str(responder.call_args)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert "example-secret" not in caplog.text


def test_configuration_check_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    with patch("afterhours_bot.__main__.run_bot", new_callable=AsyncMock) as run:
        assert main(["--check-config"]) == 0
        run.assert_not_awaited()


def test_bad_configuration_exits_before_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with patch("afterhours_bot.__main__.run_bot", new_callable=AsyncMock) as run:
        with pytest.raises(SystemExit) as error:
            main([])
        assert error.value.code == 2
        run.assert_not_awaited()


def test_authentication_failure_does_not_log_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    with patch(
        "afterhours_bot.__main__.run_bot",
        new_callable=AsyncMock,
        side_effect=discord.LoginFailure("example-secret"),
    ):
        assert main([]) == 1
    assert "authentication failed" in caplog.text
    assert "example-secret" not in caplog.text


def test_activity_needs_members_but_never_message_content_or_presence_intents() -> None:
    async def scenario() -> None:
        async with CommunityBot(
            Settings(token="example-secret", guild_id=123, active_role=ActiveRoleSettings(99))  # noqa: S106
        ) as bot:
            assert bot.intents.members
            assert not bot.intents.message_content
            assert not bot.intents.presences
            assert not bot.intents.messages

    asyncio.run(scenario())


def test_activity_worker_starts_once_and_is_cancelled_during_shutdown() -> None:
    async def scenario() -> None:
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked_worker() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async with CommunityBot(
            Settings(token="example-secret", guild_id=123, active_role=ActiveRoleSettings(99))  # noqa: S106
        ) as bot:
            with patch.object(bot, "_activity_loop", side_effect=blocked_worker) as worker:
                await bot.setup_hook()
                await bot.setup_hook()
                await asyncio.wait_for(started.wait(), timeout=1)
                worker.assert_called_once()
                await bot.close()
                assert cancelled.is_set()

    asyncio.run(scenario())


def test_activity_worker_waits_for_ready_and_retries_without_leaking_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        ready, retried = asyncio.Event(), asyncio.Event()
        guild = Mock(unavailable=False)
        attempts = 0

        async def reconcile(*args: object, **kwargs: object) -> ActivityResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("sensitive member data")
            retried.set()
            return ActivityResult(1, 0, 0, 0, 0, 0, 0)

        async with CommunityBot(
            Settings(token="example-secret", guild_id=123, active_role=ActiveRoleSettings(99))  # noqa: S106
        ) as bot:
            with (
                patch.object(bot, "wait_until_ready", side_effect=ready.wait),
                patch.object(bot, "get_guild", return_value=guild),
                patch("afterhours_bot.bot.reconcile_active_role", side_effect=reconcile) as sync,
                patch("afterhours_bot.bot.ACTIVITY_INTERVAL_SECONDS", 0.01),
            ):
                await bot.setup_hook()
                sync.assert_not_called()
                ready.set()
                await asyncio.wait_for(retried.wait(), timeout=1)
                assert sync.call_count >= 2

    asyncio.run(scenario())
    assert "RuntimeError" in caplog.text
    assert "sensitive member data" not in caplog.text


def test_missing_member_intent_has_actionable_login_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    with patch(
        "afterhours_bot.__main__.run_bot",
        new_callable=AsyncMock,
        side_effect=discord.PrivilegedIntentsRequired(None),
    ):
        assert main([]) == 1
    assert "Enable Server Members Intent" in caplog.text


def test_database_initialization_error_is_actionable_and_redacted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    with patch(
        "afterhours_bot.__main__.run_bot",
        new_callable=AsyncMock,
        side_effect=sqlite3.OperationalError("sensitive database path"),
    ):
        assert main([]) == 1
    assert "Database initialization failed" in caplog.text
    assert "sensitive database path" not in caplog.text
