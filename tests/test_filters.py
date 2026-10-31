# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
from datetime import UTC, datetime, timedelta

import pytest

from afterhours_bot.config import (
    DEFAULT_ATTACHMENT_DENYLIST,
    ConfigurationError,
    DenyEntry,
    ModerationSettings,
    Settings,
)
from afterhours_bot.filters import (
    Flag,
    RepeatTracker,
    evaluate,
    find_denied_attachments,
    find_denied_links,
    find_invites,
)
from afterhours_bot.moderation import IntakeMessage

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
BASE_ENV = {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123"}
LINKS = (DenyEntry("bad.example", 2), DenyEntry("nsfw.example", 5))
ATTACHMENTS = (DenyEntry("exe", 2), DenyEntry("bat", 9))


# Configuration


def test_filter_settings_have_defaults_and_parse_deny_lists() -> None:
    settings = Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555"})
    assert settings.moderation is not None
    assert settings.moderation.repeat_window_seconds == 60
    assert settings.moderation.repeat_threshold == 3
    assert settings.moderation.link_denylist == ()
    assert DenyEntry("exe", 2) in settings.moderation.attachment_denylist
    assert DenyEntry("bat", 9) in settings.moderation.attachment_denylist
    assert settings.moderation.audit_retention_days == 90
    assert settings.moderation.audit_max_rows == 100_000
    assert settings.moderation.flags_per_minute == 20
    custom = Settings.from_env(
        {
            **BASE_ENV,
            "MODERATION_STAFF_CHANNEL_ID": "555",
            "MODERATION_LINK_DENYLIST": " Bad.Example:2, nsfw.example.:5 ,,bad.example:2",
            "MODERATION_ATTACHMENT_DENYLIST": "",
            "MODERATION_REPEAT_WINDOW_SECONDS": "120",
            "MODERATION_REPEAT_THRESHOLD": "2",
            "MODERATION_AUDIT_RETENTION_DAYS": "30",
            "MODERATION_AUDIT_MAX_ROWS": "500",
            "MODERATION_FLAGS_PER_MINUTE": "5",
        }
    )
    assert custom.moderation is not None
    assert custom.moderation.link_denylist == LINKS
    assert custom.moderation.attachment_denylist == ()
    assert (custom.moderation.repeat_window_seconds, custom.moderation.repeat_threshold) == (120, 2)
    assert custom.moderation.audit_retention_days == 30
    assert custom.moderation.audit_max_rows == 500
    assert custom.moderation.flags_per_minute == 5


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODERATION_LINK_DENYLIST", "bad.example"),
        ("MODERATION_LINK_DENYLIST", "bad.example:0"),
        ("MODERATION_LINK_DENYLIST", "bad.example:100"),
        ("MODERATION_LINK_DENYLIST", ":2"),
        ("MODERATION_LINK_DENYLIST", "bad example:2"),
        ("MODERATION_LINK_DENYLIST", "bad.example/path:2"),
        ("MODERATION_LINK_DENYLIST", "bäd.example:2"),
        ("MODERATION_LINK_DENYLIST", ",".join(f"h{index}.example:2" for index in range(1001))),
        ("MODERATION_ATTACHMENT_DENYLIST", "exe"),
        ("MODERATION_REPEAT_WINDOW_SECONDS", "0"),
        ("MODERATION_REPEAT_WINDOW_SECONDS", "3601"),
        ("MODERATION_REPEAT_THRESHOLD", "1"),
        ("MODERATION_REPEAT_THRESHOLD", "101"),
        ("MODERATION_AUDIT_RETENTION_DAYS", "0"),
        ("MODERATION_AUDIT_MAX_ROWS", "99"),
        ("MODERATION_FLAGS_PER_MINUTE", "601"),
    ],
)
def test_invalid_filter_configuration_fails_without_echoing_values(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555", name: value})
    assert name in str(error.value)
    assert "example-secret" not in str(error.value)
    assert "bad.example" not in str(error.value)


def test_default_attachment_list_names_executables_and_scripts() -> None:
    assert "exe:2" in DEFAULT_ATTACHMENT_DENYLIST
    assert "ps1:9" in DEFAULT_ATTACHMENT_DENYLIST


# Invites


@pytest.mark.parametrize(
    "content",
    [
        "join discord.gg/abc123",
        "https://discord.gg/abc-123 now",
        "DISCORD.COM/invite/xyz",
        "https://www.discordapp.com/invite/old",
    ],
)
def test_invite_links_are_flagged_under_the_spam_rule(content: str) -> None:
    flag = find_invites(content)
    assert flag is not None
    assert (flag.rule, flag.source) == (6, "invite")
    assert "1 Discord server invite link" in flag.reason
    assert "abc" not in flag.reason and "xyz" not in flag.reason


def test_invite_count_is_reported_and_plain_text_is_not_flagged() -> None:
    flag = find_invites("discord.gg/a and discord.gg/b")
    assert flag is not None and flag.reason.startswith("2 Discord server invite links")
    assert find_invites("talk about discord in general") is None
    assert find_invites("https://discord.com/channels/1/2/3") is None


# Link domains


@pytest.mark.parametrize(
    "content,rule",
    [
        ("see https://bad.example/file", 2),
        ("see http://Mirror.BAD.example:8080/x?y=1", 2),
        ("see <https://nsfw.example/>", 5),
    ],
)
def test_listed_domains_and_subdomains_are_flagged(content: str, rule: int) -> None:
    (flag,) = find_denied_links(content, LINKS)
    assert (flag.rule, flag.source, flag.score) == (rule, "link-domain", 0.9)
    assert "listed domain" in flag.reason
    assert "file" not in flag.reason and "Mirror" not in flag.reason


def test_unlisted_lookalike_and_malformed_links_are_not_flagged() -> None:
    assert find_denied_links("https://notbad.example/ https://bad.example.org/", LINKS) == ()
    assert find_denied_links("https://[::1/ https://", LINKS) == ()
    assert find_denied_links("https://bad.example/", ()) == ()


def test_links_are_grouped_per_domain_and_bounded() -> None:
    content = " ".join(["https://bad.example/a"] * 60 + ["https://nsfw.example/b"])
    flags = find_denied_links(content, LINKS)
    assert [(flag.rule, flag.reason[:2]) for flag in flags] == [(2, "50")]


# Attachments


def test_denied_attachment_extensions_are_flagged_case_insensitively() -> None:
    flags = find_denied_attachments(["Setup.EXE", "readme.txt", "run.bat", "tool.exe"], ATTACHMENTS)
    assert {(flag.rule, flag.reason) for flag in flags} == {
        (2, "2 attachments with extension .exe"),
        (9, "1 attachment with extension .bat"),
    }
    assert all(flag.source == "attachment" and flag.score == 0.8 for flag in flags)


def test_attachments_without_extensions_or_lists_are_ignored() -> None:
    assert find_denied_attachments(["exe", ".exe", "noext"], ATTACHMENTS) == ()
    assert find_denied_attachments(["a.exe"], ()) == ()


# Repeats


def test_identical_messages_within_the_window_reach_the_threshold() -> None:
    tracker = RepeatTracker(60, 3)
    assert tracker.observe(1, "Buy now!!", NOW) is None
    assert tracker.observe(1, "buy   NOW!!", NOW + timedelta(seconds=10)) is None
    flag = tracker.observe(1, "Buy now!!", NOW + timedelta(seconds=20))
    assert flag == Flag(6, 0.6, "repeat", "3 identical messages within 60 s")
    stronger = tracker.observe(1, "Buy now!!", NOW + timedelta(seconds=30))
    assert stronger is not None and stronger.score == pytest.approx(0.7)
    assert "Buy" not in flag.reason


def test_repeats_expire_differ_per_author_and_ignore_empty_content() -> None:
    tracker = RepeatTracker(60, 2)
    assert tracker.observe(1, "same", NOW) is None
    assert tracker.observe(2, "same", NOW) is None  # Another author does not count.
    assert tracker.observe(1, "other", NOW) is None
    assert tracker.observe(1, "same", NOW + timedelta(seconds=61)) is None  # Expired.
    assert tracker.observe(1, "same", NOW + timedelta(seconds=70)) is not None
    assert tracker.observe(3, "   ", NOW) is None


def test_repeat_tracker_memory_is_bounded() -> None:
    tracker = RepeatTracker(60, 2, max_authors=2, max_per_author=3)
    for author in (1, 2, 3):
        tracker.observe(author, "x", NOW)
    assert list(tracker._authors) == [2, 3]
    for index in range(10):
        tracker.observe(3, f"m{index}", NOW)
    assert len(tracker._authors[3]) == 3
    tracker.forget_expired(NOW + timedelta(seconds=120))
    assert tracker._authors == {}


# Evaluation


def test_evaluate_combines_filters_in_a_stable_order() -> None:
    settings = ModerationSettings(
        staff_channel_id=555,
        repeat_threshold=2,
        link_denylist=LINKS,
        attachment_denylist=ATTACHMENTS,
    )
    tracker = RepeatTracker(settings.repeat_window_seconds, settings.repeat_threshold)
    content = "discord.gg/abc https://bad.example/x"
    first = IntakeMessage(
        1, 10, 42, NOW, content, "https://discord.com/channels/1/10/1", ("a.exe",)
    )
    second = IntakeMessage(2, 10, 42, NOW, content, "https://discord.com/channels/1/10/2", ())
    assert [flag.source for flag in evaluate(first, settings, tracker)] == [
        "invite",
        "link-domain",
        "attachment",
    ]
    assert [flag.source for flag in evaluate(second, settings, tracker)] == [
        "repeat",
        "invite",
        "link-domain",
    ]
    clean = IntakeMessage(3, 10, 42, NOW, "hello everyone", "", ())
    assert evaluate(clean, settings, tracker) == ()
