# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Edited messages, model context, image review, automatic actions and runtime rule updates."""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from afterhours_bot.bot import CommunityBot
from afterhours_bot.classifier import Classifier, build_messages
from afterhours_bot.config import (
    AutoAction,
    ConfigurationError,
    DenyEntry,
    LlmSettings,
    ModerationSettings,
    Settings,
)
from afterhours_bot.moderation import IntakeMessage, select_message
from afterhours_bot.review import LinkView, ReviewView, StaffReviewer
from afterhours_bot.rules import load_rules, parse_rules
from afterhours_bot.storage import ActivityStore

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / "rules" / "community-rules.md"
RULES = load_rules(RULES_PATH)
BASE_ENV = {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123"}
MODERATION_ENV = {**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555"}


def verdict_json(rule: int | None, score: float, reason: str = "because") -> str:
    return json.dumps({"rule": rule, "score": score, "reason": reason})


def completion(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@dataclass
class FakeEndpoint:
    answers: list[str] = field(default_factory=list)
    requests: list[dict[str, object]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(await request.json())
        answer = self.answers.pop(0) if self.answers else verdict_json(None, 0)
        return web.json_response(completion(answer))


@asynccontextmanager
async def fake_server(endpoint: FakeEndpoint) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("/v1"))
    finally:
        await server.close()


def run_with_server(endpoint: FakeEndpoint, scenario: Callable[[str], Awaitable[None]]) -> None:
    async def wrapper() -> None:
        async with fake_server(endpoint) as url:
            await scenario(url)

    asyncio.run(wrapper())


@pytest.fixture
def store(tmp_path: Path) -> Iterator[ActivityStore]:
    instance = ActivityStore(tmp_path / "nightshift.sqlite3")
    yield instance
    asyncio.run(instance.close())


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


# Configuration


def test_context_vision_and_auto_action_settings_parse() -> None:
    settings = Settings.from_env(
        {
            **MODERATION_ENV,
            "MODERATION_LLM_URL": "http://localhost:11434/v1",
            "MODERATION_LLM_MODEL": "m",
            "MODERATION_LLM_VISION_MODEL": "llava:7b",
            "MODERATION_LLM_CONTEXT_MESSAGES": "0",
            "MODERATION_AUTO_ACTIONS": " invite:6:delete:0.5, Link-Domain:2:timeout:1 ,",
        }
    )
    assert settings.moderation is not None and settings.moderation.llm is not None
    assert settings.moderation.llm.vision_model == "llava:7b"
    assert settings.moderation.llm.context_messages == 0
    assert settings.moderation.auto_actions == (
        AutoAction("invite", 6, "delete", 0.5),
        AutoAction("link-domain", 2, "timeout", 1.0),
    )
    defaults = Settings.from_env(MODERATION_ENV)
    assert defaults.moderation is not None and defaults.moderation.auto_actions == ()


@pytest.mark.parametrize(
    "value",
    [
        "invite:6:delete",
        "llm:2:delete:0.9",
        "llm-image:5:delete:0.9",
        "invite:0:delete:0.5",
        "invite:6:ban:0.5",
        "invite:6:delete:2",
        ",".join(f"invite:6:delete:0.{index % 9 + 1}" for index in range(101)),
    ],
)
def test_invalid_or_unsafe_auto_actions_are_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError, match="MODERATION_AUTO_ACTIONS"):
        Settings.from_env({**MODERATION_ENV, "MODERATION_AUTO_ACTIONS": value})


def test_invalid_context_and_vision_settings_are_rejected() -> None:
    base = {**MODERATION_ENV, "MODERATION_LLM_URL": "http://h/v1", "MODERATION_LLM_MODEL": "m"}
    with pytest.raises(ConfigurationError, match="MODERATION_LLM_CONTEXT_MESSAGES"):
        Settings.from_env({**base, "MODERATION_LLM_CONTEXT_MESSAGES": "11"})
    with pytest.raises(ConfigurationError, match="MODERATION_LLM_VISION_MODEL"):
        Settings.from_env({**base, "MODERATION_LLM_VISION_MODEL": "two words"})


# Intake: images and edits


def message(**overrides: object) -> discord.Message:
    fields: dict[str, object] = {
        "id": 1000,
        "guild": SimpleNamespace(id=123),
        "author": SimpleNamespace(id=42, bot=False),
        "webhook_id": None,
        "type": discord.MessageType.default,
        "channel": SimpleNamespace(id=10, parent_id=None),
        "created_at": NOW,
        "content": "hello",
        "jump_url": "https://discord.com/channels/123/10/1000",
        "attachments": [],
    }
    fields.update(overrides)
    return Mock(spec=discord.Message, **fields)


def attachment(
    name: str, content_type: str | None, size: int = 1000, scheme: str = "https"
) -> Mock:
    return SimpleNamespace(
        filename=name, content_type=content_type, size=size, url=f"{scheme}://cdn.example/{name}"
    )


def test_image_attachments_are_selected_with_bounds() -> None:
    attachments = [
        attachment("a.png", "image/png"),
        attachment("b.JPG", "IMAGE/JPEG"),
        attachment("c.txt", "text/plain"),
        attachment("d.png", "image/png", size=11 * 1024 * 1024),
        attachment("e.png", None),
        attachment("f.png", "image/png", scheme="http"),
        attachment("g.png", "image/png"),
        attachment("h.png", "image/png"),
        attachment("i.png", "image/png"),
    ]
    selected = select_message(message(attachments=attachments), ModerationSettings(555), 123)
    assert selected is not None
    assert selected.image_urls == tuple(
        f"https://cdn.example/{n}" for n in ("a.png", "b.JPG", "g.png", "h.png")
    )
    assert selected.attachment_names[:3] == ("a.png", "b.JPG", "c.txt")
    assert selected.edited is False
    edited = select_message(message(), ModerationSettings(555), 123, edited=True)
    assert edited is not None and edited.edited is True


def test_edited_messages_are_submitted_through_the_raw_event() -> None:
    config = Settings(
        token="example-secret",  # noqa: S106
        guild_id=123,
        moderation=ModerationSettings(staff_channel_id=555, rules_path=RULES_PATH),
    )

    async def scenario() -> None:
        async with CommunityBot(config) as bot:
            assert bot.moderation is not None
            await bot.on_raw_message_edit(SimpleNamespace(message=message()))  # type: ignore[arg-type]
            await bot.on_raw_message_edit(
                SimpleNamespace(message=message(author=SimpleNamespace(id=1, bot=True)))  # type: ignore[arg-type]
            )
            assert bot.moderation.counters.queued == 1

    asyncio.run(scenario())


# Reviewer helpers


def staff_channel() -> Mock:
    result = Mock(spec=discord.TextChannel)
    result.id = 555
    result.guild = SimpleNamespace(id=123, me=object())
    result.permissions_for = Mock(return_value=discord.Permissions(send_messages=True))
    result.send = AsyncMock(side_effect=lambda *a, **k: Mock(id=9000 + result.send.await_count))
    return result


def target_guild() -> Mock:
    result = Mock(spec=discord.Guild)
    result.unavailable = False
    result.name = "Afterhours"
    me = Mock(spec=discord.Member)
    me.guild_permissions = discord.Permissions(moderate_members=True)
    me.top_role = 5
    result.me = me
    channel = Mock(spec=discord.TextChannel)
    channel.permissions_for = Mock(return_value=discord.Permissions(manage_messages=True))
    deleted = Mock(spec=discord.Message)
    deleted.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=deleted)
    result.get_channel_or_thread = Mock(return_value=channel)
    member = Mock(spec=discord.Member)
    member.bot = False
    member.guild_permissions = discord.Permissions()
    member.top_role = 1
    member.send = AsyncMock()
    member.timeout = AsyncMock()
    result.get_member = Mock(return_value=member)
    return result


def make_reviewer(
    store: ActivityStore,
    settings: ModerationSettings,
    *,
    classifier: Classifier | None = None,
) -> tuple[StaffReviewer, Mock, Mock]:
    channel = staff_channel()
    guild = target_guild()
    client = Mock()
    client.user = Mock(id=99)
    client.get_channel = Mock(return_value=channel)
    client.get_guild = Mock(return_value=guild)
    review = StaffReviewer(
        client,
        settings,
        store,
        guild_id=123,
        rules_revision=lambda: 1,
        clock=lambda: NOW,
        classifier=classifier,
        rule_titles=lambda: RULES.titles,
    )
    return review, channel, guild


def intake(number: int, content: str, **overrides: object) -> IntakeMessage:
    fields: dict[str, object] = {
        "message_id": number,
        "channel_id": 10,
        "author_id": 42,
        "created_at": NOW,
        "content": content,
        "jump_url": f"https://discord.com/channels/123/10/{number}",
    }
    fields.update(overrides)
    return IntakeMessage(**fields)  # type: ignore[arg-type]


# Edited messages


def test_an_edited_message_that_was_already_flagged_keeps_and_updates_its_post(
    store: ActivityStore,
) -> None:
    settings = ModerationSettings(
        555, repeat_threshold=50, link_denylist=(DenyEntry("bad.example", 2),)
    )

    async def scenario() -> None:
        review, channel, _ = make_reviewer(store, settings)
        await review.review(intake(1, "see https://bad.example/x"))
        posted = Mock(spec=discord.Message)
        posted.embeds = [channel.send.call_args.kwargs["embed"]]
        posted.edit = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=posted)
        await review.review(intake(1, "see https://bad.example/x discord.gg/abc", edited=True))
        assert channel.send.await_count == 1
        assert review.counters.reflagged == 1
        count, post = await store.message_flags(123, 1)
        assert (count, post) == (2, 9001)
        channel.fetch_message.assert_awaited_with(9001)
        embed = posted.edit.call_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert embed.description == "> see https://bad.example/x discord.gg/abc"
        assert fields["Original"] == "> see https://bad.example/x"
        assert fields["Edited"].startswith("edited <t:") and "invite" in fields["Edited"]
        await review.review(intake(1, "all clean now", edited=True))
        embed = posted.edit.call_args.kwargs["embed"]
        names = [field.name for field in embed.fields]
        assert names.count("Original") == 1 and names.count("Edited") == 1
        assert "find nothing" in embed.fields[-1].value
        assert embed.description == "> all clean now"
        await review.review(intake(2, "clean then edited to discord.gg/abc", edited=True))
        assert channel.send.await_count == 2  # Previously clean message gets a post.

    asyncio.run(scenario())


# Context


def test_channel_context_reaches_the_model_bounded_and_anonymized(store: ActivityStore) -> None:
    endpoint = FakeEndpoint([verdict_json(None, 0)] * 6)

    async def scenario(url: str) -> None:
        llm = LlmSettings(url=url, model="m", context_messages=2)
        settings = ModerationSettings(555, repeat_threshold=50, llm=llm)
        classifier = Classifier(
            llm, rules_text=lambda: RULES.text, rule_numbers=lambda: RULES.numbers
        )
        review, _, _ = make_reviewer(store, settings, classifier=classifier)
        try:
            await review.review(intake(1, "qwx1 " + "x" * 300, author_id=1042))
            await review.review(intake(2, "qwx2", author_id=2077))
            await review.review(intake(3, "qwx3", author_id=1042))
            await review.review(intake(4, "elsewhere", channel_id=11))
            await review.review(intake(5, "qwx5", edited=True))
        finally:
            await classifier.close()
        prompts = [str(request["messages"]) for request in endpoint.requests]
        assert "Earlier messages" not in prompts[0]
        assert "member 042: qwx1" in prompts[1]
        assert "member 042: qwx1" in prompts[2] and "member 077: qwx2" in prompts[2]
        assert "qwx1" not in prompts[3] and "Earlier messages" not in prompts[3]
        assert "qwx2" in prompts[4] and "qwx3" in prompts[4] and "qwx1" not in prompts[4]
        assert "x" * 201 not in prompts[1]
        assert "1042" not in prompts[2] and "2077" not in prompts[2]

    run_with_server(endpoint, scenario)


def test_context_is_off_when_configured_to_zero() -> None:
    messages = build_messages(RULES.text, None, "x", 10, context=("member 001: a",))
    assert "Earlier messages" in messages[1]["content"]
    assert "Earlier messages" not in build_messages(RULES.text, None, "x", 10)[1]["content"]


# Images


def test_images_go_to_the_vision_model_as_image_parts(store: ActivityStore) -> None:
    endpoint = FakeEndpoint([verdict_json(None, 0), verdict_json(5, 0.95, "explicit image")])

    async def scenario(url: str) -> None:
        llm = LlmSettings(
            url=url, model="text-model", vision_model="vision-model", context_messages=0
        )
        settings = ModerationSettings(555, repeat_threshold=50, llm=llm)
        classifier = Classifier(
            llm, rules_text=lambda: RULES.text, rule_numbers=lambda: RULES.numbers
        )
        review, channel, _ = make_reviewer(store, settings, classifier=classifier)
        try:
            await review.review(intake(1, "look", image_urls=("https://cdn.example/a.png",)))
        finally:
            await classifier.close()
        assert [request["model"] for request in endpoint.requests] == ["text-model", "vision-model"]
        parts = endpoint.requests[1]["messages"][1]["content"]  # type: ignore[index]
        assert parts[0]["type"] == "text" and "attached images" in parts[0]["text"]
        assert parts[1] == {"type": "image_url", "image_url": {"url": "https://cdn.example/a.png"}}
        embed = channel.send.call_args.kwargs["embed"]
        why = {field.name: field.value for field in embed.fields}["Why"]
        assert why == "model (image): explicit image (rule 5, score 0.95, llm-image)"
        details = {field.name: field.value for field in embed.fields}["Details"]
        assert "source llm-image (vision-model)" in details
        assert embed.image.url == "https://cdn.example/a.png"

    run_with_server(endpoint, scenario)


def test_images_are_skipped_without_a_vision_model(store: ActivityStore) -> None:
    endpoint = FakeEndpoint([verdict_json(None, 0)])

    async def scenario(url: str) -> None:
        llm = LlmSettings(url=url, model="text-model", context_messages=0)
        settings = ModerationSettings(555, repeat_threshold=50, llm=llm)
        classifier = Classifier(
            llm, rules_text=lambda: RULES.text, rule_numbers=lambda: RULES.numbers
        )
        review, channel, _ = make_reviewer(store, settings, classifier=classifier)
        try:
            await review.review(intake(1, "", image_urls=("https://cdn.example/a.png",)))
            await review.review(intake(2, "text", image_urls=("https://cdn.example/a.png",)))
        finally:
            await classifier.close()
        assert len(endpoint.requests) == 1  # Only the text request for message 2.
        channel.send.assert_not_awaited()

    run_with_server(endpoint, scenario)


# Automatic actions


def test_configured_automatic_action_runs_posts_without_buttons_and_is_audited(
    store: ActivityStore, tmp_path: Path
) -> None:
    settings = ModerationSettings(
        555,
        repeat_threshold=50,
        auto_actions=(AutoAction("invite", 6, "delete", 0.5),),
    )

    async def scenario() -> None:
        review, channel, guild = make_reviewer(store, settings)
        await review.review(intake(1, "join discord.gg/abc"))
        guild.get_channel_or_thread.return_value.fetch_message.return_value.delete.assert_awaited_once()
        kwargs = channel.send.call_args.kwargs
        assert isinstance(kwargs["view"], LinkView)
        fields = {field.name: field.value for field in kwargs["embed"].fields}
        assert fields["Automatic action"] == "delete (invite, rule 6): message deleted"
        assert kwargs["embed"].footer.text.endswith("automatic action taken")
        assert review.counters.automatic == 1 and review.counters.actions == 1
        assert await store.flag_summary(123) == (1, 0)

    asyncio.run(scenario())
    with sqlite3.connect(tmp_path / "nightshift.sqlite3") as connection:
        row = connection.execute(
            "SELECT decision, decider_id, outcome FROM review_flags"
        ).fetchone()
    assert row == ("auto-delete", "99", "message deleted")


def test_automatic_actions_skip_staff_and_unmatched_flags(store: ActivityStore) -> None:
    settings = ModerationSettings(
        555,
        repeat_threshold=50,
        link_denylist=(DenyEntry("bad.example", 2),),
        auto_actions=(
            AutoAction("invite", 6, "delete", 0.9),
            AutoAction("link-domain", 2, "warn", 0.5),
        ),
    )

    async def scenario() -> None:
        review, channel, guild = make_reviewer(store, settings)
        await review.review(intake(1, "discord.gg/abc"))  # Score 0.6 below 0.9: no action.
        await review.review(intake(2, "https://bad.example/x", author_manages_messages=True))
        assert review.counters.automatic == 0
        assert isinstance(channel.send.call_args_list[0].kwargs["view"], ReviewView)
        await review.review(intake(3, "https://bad.example/y"))
        guild.get_member.return_value.send.assert_awaited_once()
        assert review.counters.automatic == 1
        assert await store.flag_summary(123) == (3, 2)

    asyncio.run(scenario())


# Runtime rule updates


def bot_settings(rules_path: Path, database: Path) -> Settings:
    return Settings(
        token="example-secret",  # noqa: S106
        guild_id=123,
        database_path=database,
        moderation=ModerationSettings(staff_channel_id=555, rules_path=rules_path),
    )


def test_rules_commands_exist_and_are_staff_only() -> None:
    async def scenario() -> None:
        async with CommunityBot(bot_settings(RULES_PATH, Path("db.sqlite3"))) as bot:
            group = bot.tree.get_command("rules", guild=discord.Object(id=123))
            assert isinstance(group, discord.app_commands.Group)
            assert group.guild_only
            assert (
                group.default_permissions is not None and group.default_permissions.manage_messages
            )
            assert {command.name for command in group.commands} == {"show", "reload", "upload"}

    asyncio.run(scenario())


def test_upload_replaces_rules_until_the_file_changes_and_reload_restores_the_file(
    tmp_path: Path,
) -> None:
    rules_file = tmp_path / "rules.md"
    rules_file.write_text("# 1. Be kind\n", encoding="utf-8")
    database = tmp_path / "state.sqlite3"
    uploaded = b"# 1. Be kind\n# 2. No piracy\n"

    def upload(name: str = "rules.md", data: bytes = uploaded, size: int | None = None) -> Mock:
        file = Mock(spec=discord.Attachment)
        file.filename = name
        file.size = len(data) if size is None else size
        file.read = AsyncMock(return_value=data)
        return file

    async def scenario() -> None:
        async with CommunityBot(bot_settings(rules_file, database)) as bot:
            await bot.setup_hook()
            assert (bot.rules_revision, bot.rules_source) == (1, "file")
            assert "Rejected: upload a .md" in await bot.upload_rules(upload("rules.exe"))
            assert "exceeds" in await bot.upload_rules(upload(size=70_000))
            assert "no numbered rule" in await bot.upload_rules(upload(data=b"just prose"))
            assert "Rejected: The rule text is empty" in await bot.upload_rules(upload(data=b"  "))
            reply = await bot.upload_rules(upload())
            assert "revision 2" in reply and "source: upload" in reply
            assert bot.rules is not None and bot.rules.numbers == (1, 2)
        async with CommunityBot(bot_settings(rules_file, database)) as bot:
            await bot.setup_hook()  # Same file: the upload stays active.
            assert (bot.rules_revision, bot.rules_source) == (2, "upload")
            assert bot.rules is not None and bot.rules.numbers == (1, 2)
            reply = await bot.reload_rules()
            assert "revision 3" in reply and "source: file" in reply
            assert bot.rules.numbers == (1,)
            assert "revision 3" in bot.rules_status()
        rules_file.write_text("# 1. Be kind\n# 3. No cheats\n", encoding="utf-8")
        async with CommunityBot(bot_settings(rules_file, database)) as bot:
            await bot.setup_hook()  # The file changed: it wins again.
            assert (bot.rules_revision, bot.rules_source) == (4, "file")
            assert bot.rules is not None and bot.rules.numbers == (1, 3)

    asyncio.run(scenario())


def test_parse_rules_matches_load_rules() -> None:
    assert parse_rules(RULES_PATH.read_bytes()) == RULES
