# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest
from discord import app_commands

from afterhours_bot.__main__ import main
from afterhours_bot.bot import CommunityBot
from afterhours_bot.config import Settings

SETTINGS = Settings(token="example-secret", guild_id=123)  # noqa: S106


def test_commands_are_guild_scoped_and_privileged_intents_are_disabled() -> None:
    async def scenario() -> None:
        async with CommunityBot(SETTINGS) as bot:
            assert bot.tree.get_commands() == []
            commands = bot.tree.get_commands(guild=discord.Object(id=123))
            assert {command.name for command in commands} == {"ping", "about"}
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
