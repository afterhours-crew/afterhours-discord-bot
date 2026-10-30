# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
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
