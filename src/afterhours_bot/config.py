# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Validated configuration; secrets never appear in its representation."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """Configuration is missing or invalid."""


@dataclass(frozen=True)
class ActiveRoleSettings:
    role_id: int
    window_days: int = 14
    min_messages: int = 3


def _bounded_integer(
    env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = env.get(name, str(default)).strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > len(str(maximum)):
        raise ConfigurationError(f"{name} must be an integer between {minimum} and {maximum}.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _positive_integer(env: Mapping[str, str], name: str, default: int, maximum: int) -> int:
    return _bounded_integer(env, name, default, 1, maximum)


def _snowflake(raw: str, name: str) -> int:
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 20:
        raise ConfigurationError(f"{name} must be a positive Discord ID.")
    value = int(raw)
    if not 0 < value < 2**64:
        raise ConfigurationError(f"{name} must be a positive Discord ID.")
    return value


@dataclass(frozen=True)
class DenyEntry:
    """A lowercase host or file extension mapped to the community rule it breaks."""

    value: str
    rule: int


DEFAULT_ATTACHMENT_DENYLIST = (
    "exe:2,msi:2,dll:2,bat:9,cmd:9,scr:9,com:9,pif:9,vbs:9,vbe:9,js:9,jse:9,ps1:9,jar:9,reg:9,lnk:9"
)


@dataclass(frozen=True)
class ModerationSettings:
    """Review is enabled by naming the staff channel; it needs the Message Content intent."""

    staff_channel_id: int
    rules_path: Path = Path("rules/community-rules.md")
    queue_size: int = 1000
    repeat_window_seconds: int = 60
    repeat_threshold: int = 3
    link_denylist: tuple[DenyEntry, ...] = ()
    attachment_denylist: tuple[DenyEntry, ...] = ()
    audit_retention_days: int = 90
    audit_max_rows: int = 100_000
    flags_per_minute: int = 20


def _file_path(env: Mapping[str, str], name: str, default: str) -> Path:
    raw = env.get(name, default).strip()
    if not raw or "\x00" in raw:
        raise ConfigurationError(f"{name} must name a file.")
    return Path(raw)


def _deny_list(env: Mapping[str, str], name: str, default: str) -> tuple[DenyEntry, ...]:
    """Parse `value:rule` pairs separated by commas; values are hosts or extensions."""
    raw = env.get(name, default)
    entries: dict[str, int] = {}
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        value, separator, rule = item.rpartition(":")
        if (
            not separator
            or not value
            or len(value) > 253
            or not value.isascii()
            or any(character.isspace() or character == "/" for character in value)
            or not rule.isdecimal()
            or not 1 <= int(rule) <= 99
        ):
            raise ConfigurationError(f"{name} entries must look like value:rule with rule 1-99.")
        entries[value.strip(".")] = int(rule)
    if len(entries) > 1000:
        raise ConfigurationError(f"{name} supports at most 1000 entries.")
    return tuple(DenyEntry(value, rule) for value, rule in sorted(entries.items()))


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    guild_id: int
    active_role: ActiveRoleSettings | None = None
    database_path: Path = Path("data/nightshift.sqlite3")
    moderation: ModerationSettings | None = None

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
        database_path = env.get("NIGHTSHIFT_DATABASE_PATH", "data/nightshift.sqlite3").strip()
        if not database_path or "\x00" in database_path or database_path == ":memory:":
            raise ConfigurationError("NIGHTSHIFT_DATABASE_PATH must name a database file.")
        rules_path = _file_path(env, "MODERATION_RULES_PATH", "rules/community-rules.md")
        queue_size = _positive_integer(env, "MODERATION_QUEUE_SIZE", 1000, 100_000)
        raw_staff_channel = env.get("MODERATION_STAFF_CHANNEL_ID", "").strip()
        moderation = (
            ModerationSettings(
                staff_channel_id=_snowflake(raw_staff_channel, "MODERATION_STAFF_CHANNEL_ID"),
                rules_path=rules_path,
                queue_size=queue_size,
                repeat_window_seconds=_positive_integer(
                    env, "MODERATION_REPEAT_WINDOW_SECONDS", 60, 3600
                ),
                repeat_threshold=_bounded_integer(env, "MODERATION_REPEAT_THRESHOLD", 3, 2, 100),
                link_denylist=_deny_list(env, "MODERATION_LINK_DENYLIST", ""),
                attachment_denylist=_deny_list(
                    env, "MODERATION_ATTACHMENT_DENYLIST", DEFAULT_ATTACHMENT_DENYLIST
                ),
                audit_retention_days=_positive_integer(
                    env, "MODERATION_AUDIT_RETENTION_DAYS", 90, 3650
                ),
                audit_max_rows=_bounded_integer(
                    env, "MODERATION_AUDIT_MAX_ROWS", 100_000, 100, 10_000_000
                ),
                flags_per_minute=_positive_integer(env, "MODERATION_FLAGS_PER_MINUTE", 20, 600),
            )
            if raw_staff_channel
            else None
        )
        return cls(
            token=token,
            guild_id=guild_id,
            active_role=active_role,
            database_path=Path(database_path),
            moderation=moderation,
        )
