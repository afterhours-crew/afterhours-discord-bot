# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from pathlib import Path

import pytest

from afterhours_bot.config import ConfigurationError, Settings


def test_secret_is_not_in_representation() -> None:
    settings = Settings.from_env({"DISCORD_TOKEN": " example-secret ", "DISCORD_GUILD_ID": "123"})
    assert settings.token == "example-secret"  # noqa: S105
    assert settings.guild_id == 123
    assert "example-secret" not in repr(settings)


@pytest.mark.parametrize("token", ["", " ", "\n"])
def test_empty_token_is_rejected(token: str) -> None:
    with pytest.raises(ConfigurationError, match="DISCORD_TOKEN is required"):
        Settings.from_env({"DISCORD_TOKEN": token, "DISCORD_GUILD_ID": "123"})


@pytest.mark.parametrize(
    "guild_id", ["", "0", "-1", "abc", "1.0", "１２３", "18446744073709551616", "9" * 5000]
)
def test_invalid_guild_id_never_leaks_values(guild_id: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": guild_id})
    assert str(error.value) == "DISCORD_GUILD_ID must be a positive Discord server ID."


def test_missing_environment_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        Settings.from_env({})


def test_activity_is_optional_and_defaults_match_three_messages_in_fourteen_days() -> None:
    env = {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123"}
    assert Settings.from_env(env).active_role is None
    settings = Settings.from_env({**env, "ACTIVE_ROLE_ID": "456"})
    assert settings.active_role is not None
    assert settings.active_role.role_id == 456
    assert settings.active_role.window_days == 14
    assert settings.active_role.min_messages == 3


def test_activity_days_and_minimum_are_configurable() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "example-secret",
            "DISCORD_GUILD_ID": "123",
            "ACTIVE_ROLE_ID": "456",
            "ACTIVE_WINDOW_DAYS": "7",
            "ACTIVE_MIN_MESSAGES": "5",
        }
    )
    assert settings.active_role is not None
    assert (settings.active_role.window_days, settings.active_role.min_messages) == (7, 5)


@pytest.mark.parametrize(
    "name,value",
    [
        ("ACTIVE_ROLE_ID", "0"),
        ("ACTIVE_ROLE_ID", "not-an-id"),
        ("ACTIVE_ROLE_ID", "18446744073709551616"),
        ("ACTIVE_WINDOW_DAYS", "0"),
        ("ACTIVE_WINDOW_DAYS", "366"),
        ("ACTIVE_WINDOW_DAYS", "abc"),
        ("ACTIVE_WINDOW_DAYS", "1.5"),
        ("ACTIVE_MIN_MESSAGES", "0"),
        ("ACTIVE_MIN_MESSAGES", "10001"),
        ("ACTIVE_MIN_MESSAGES", "-1"),
        ("ACTIVE_MIN_MESSAGES", "３"),
        ("ACTIVE_MIN_MESSAGES", "9" * 5000),
    ],
)
def test_invalid_activity_configuration_fails_without_echoing_values(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(
            {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123", name: value}
        )
    assert name in str(error.value)
    assert "example-secret" not in str(error.value)


def test_database_path_is_configurable() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_TOKEN": "example-secret",
            "DISCORD_GUILD_ID": "123",
            "NIGHTSHIFT_DATABASE_PATH": "state/custom.sqlite3",
        }
    )
    assert settings.database_path == Path("state/custom.sqlite3")


@pytest.mark.parametrize("value", ["", " ", ":memory:", "bad\x00path"])
def test_database_path_rejects_empty_or_nonpersistent_values(value: str) -> None:
    with pytest.raises(ConfigurationError, match="NIGHTSHIFT_DATABASE_PATH"):
        Settings.from_env(
            {
                "DISCORD_TOKEN": "example-secret",
                "DISCORD_GUILD_ID": "123",
                "NIGHTSHIFT_DATABASE_PATH": value,
            }
        )
