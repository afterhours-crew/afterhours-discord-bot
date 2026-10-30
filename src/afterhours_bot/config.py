# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Validated configuration; secrets never appear in its representation."""

from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    guild_id: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        token = env.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigurationError("DISCORD_TOKEN is required.")
        raw_guild = env.get("DISCORD_GUILD_ID", "").strip()
        if not raw_guild.isascii() or not raw_guild.isdecimal() or len(raw_guild) > 20:
            raise ConfigurationError("DISCORD_GUILD_ID must be a positive Discord server ID.")
        guild_id = int(raw_guild)
        if not 0 < guild_id < 2**64:
            raise ConfigurationError("DISCORD_GUILD_ID must be a positive Discord server ID.")
        return cls(token=token, guild_id=guild_id)
