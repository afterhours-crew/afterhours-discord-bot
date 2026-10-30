# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Validated configuration; secrets never appear in its representation."""

from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigurationError(ValueError):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class ActiveRoleSettings:
    role_id: int
    window_days: int = 14
    min_messages: int = 3


def _positive_integer(env: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    raw = env.get(name, str(default)).strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > len(str(maximum)):
        raise ConfigurationError(f"{name} must be an integer between 1 and {maximum}.")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise ConfigurationError(f"{name} must be an integer between 1 and {maximum}.")
    return value


def _snowflake(raw: str, name: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        raise ConfigurationError(f"{name} must be a positive Discord ID.")
    value = int(raw)
    if not 0 < value < 2**64:
        raise ConfigurationError(f"{name} must be a positive Discord ID.")
    return value


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    guild_id: int
    active_role: ActiveRoleSettings | None = None

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
        window_days = _positive_integer(env, "ACTIVE_WINDOW_DAYS", 14, 365)
        min_messages = _positive_integer(env, "ACTIVE_MIN_MESSAGES", 3, 10_000)
        raw_role = env.get("ACTIVE_ROLE_ID", "").strip()
        active_role = (
            ActiveRoleSettings(
                role_id=_snowflake(raw_role, "ACTIVE_ROLE_ID"),
                window_days=window_days,
                min_messages=min_messages,
            )
            if raw_role
            else None
        )
        return cls(token=token, guild_id=guild_id, active_role=active_role)
