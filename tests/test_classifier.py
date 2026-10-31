# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Classifier tests against a local fake OpenAI-compatible server; no network, no model."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
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

from afterhours_bot.classifier import (
    Classifier,
    ClassifierCounters,
    Verdict,
    build_messages,
    completion_text,
    parse_verdict,
    sanitize_reason,
)
from afterhours_bot.config import ConfigurationError, LlmSettings, ModerationSettings, Settings
from afterhours_bot.evaluate import Example, ExamplesError, evaluate_examples, load_examples
from afterhours_bot.evaluate import main as evaluate_main
from afterhours_bot.moderation import IntakeMessage
from afterhours_bot.review import StaffReviewer
from afterhours_bot.rules import load_rules
from afterhours_bot.storage import ActivityStore

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules(ROOT / "rules" / "community-rules.md")
GUIDANCE = load_rules(ROOT / "rules" / "classifier-guidance.md")
EXAMPLES_PATH = ROOT / "rules" / "classifier-examples.json"
BASE_ENV = {"DISCORD_TOKEN": "example-secret", "DISCORD_GUILD_ID": "123"}
RULE_NUMBERS = (1, 2, 3, 4, 5, 6, 7, 8, 9)


def verdict_json(rule: int | None, score: float, reason: str = "because") -> str:
    return json.dumps({"rule": rule, "score": score, "reason": reason})


def completion(text: str) -> dict[str, object]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@dataclass
class FakeEndpoint:
    """Replays scripted answers: a completion text, an HTTP status, 'garbage' or 'slow'."""

    answers: list[object] = field(default_factory=list)
    requests: list[dict[str, object]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(await request.json())
        self.headers.append(dict(request.headers))
        answer = self.answers.pop(0) if self.answers else verdict_json(None, 0)
        if isinstance(answer, int):
            return web.Response(status=answer, text="error")
        if answer == "garbage":
            return web.Response(status=200, text="<html>not json</html>")
        if answer == "slow":
            await asyncio.sleep(5)
            return web.json_response(completion(verdict_json(None, 0)))
        if answer == "huge":
            return web.json_response(completion("x" * 1_100_000))
        return web.json_response(completion(str(answer)))


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


def classifier(url: str, **overrides: object) -> Classifier:
    settings = LlmSettings(url=url, model="test-model", **overrides)  # type: ignore[arg-type]
    return Classifier(
        settings,
        rules_text=lambda: RULES.text,
        rule_numbers=lambda: RULES.numbers,
        guidance_text=lambda: GUIDANCE.text,
    )


def run_with_server(endpoint: FakeEndpoint, scenario: Callable[[str], Awaitable[None]]) -> None:
    async def wrapper() -> None:
        async with fake_server(endpoint) as url:
            await scenario(url)

    asyncio.run(wrapper())


# Configuration


def test_llm_is_off_without_a_url_and_requires_a_model() -> None:
    settings = Settings.from_env({**BASE_ENV, "MODERATION_STAFF_CHANNEL_ID": "555"})
    assert settings.moderation is not None and settings.moderation.llm is None
    with pytest.raises(ConfigurationError, match="MODERATION_LLM_MODEL"):
        Settings.from_env(
            {
                **BASE_ENV,
                "MODERATION_STAFF_CHANNEL_ID": "555",
                "MODERATION_LLM_URL": "http://localhost:11434/v1",
            }
        )


def test_llm_settings_parse_with_defaults_and_hide_the_key() -> None:
    settings = Settings.from_env(
        {
            **BASE_ENV,
            "MODERATION_STAFF_CHANNEL_ID": "555",
            "MODERATION_LLM_URL": "http://ollama:11434/v1/",
            "MODERATION_LLM_MODEL": "qwen2.5:7b-instruct",
            "MODERATION_LLM_API_KEY": "key-secret",
        }
    )
    assert settings.moderation is not None and settings.moderation.llm is not None
    llm = settings.moderation.llm
    assert llm.url == "http://ollama:11434/v1"
    assert llm.model == "qwen2.5:7b-instruct"
    assert llm.api_key == "key-secret"
    assert "key-secret" not in repr(settings)
    assert llm.guidance_path == Path("rules/classifier-guidance.md")
    assert (llm.timeout_seconds, llm.concurrency, llm.max_chars) == (20, 1, 2000)
    assert (llm.failure_threshold, llm.cooldown_seconds) == (3, 300)
    assert llm.min_score == 0.5 and llm.skip_staff is True


def test_llm_settings_are_configurable() -> None:
    settings = Settings.from_env(
        {
            **BASE_ENV,
            "MODERATION_STAFF_CHANNEL_ID": "555",
            "MODERATION_LLM_URL": "https://llm.internal:8443/api",
            "MODERATION_LLM_MODEL": "m",
            "MODERATION_LLM_GUIDANCE_PATH": "",
            "MODERATION_LLM_TIMEOUT_SECONDS": "5",
            "MODERATION_LLM_CONCURRENCY": "2",
            "MODERATION_LLM_MAX_CHARS": "500",
            "MODERATION_LLM_FAILURE_THRESHOLD": "1",
            "MODERATION_LLM_COOLDOWN_SECONDS": "10",
            "MODERATION_LLM_MIN_SCORE": "0.75",
            "MODERATION_LLM_SKIP_STAFF": "no",
        }
    )
    assert settings.moderation is not None and settings.moderation.llm is not None
    llm = settings.moderation.llm
    assert llm.guidance_path is None
    assert (llm.timeout_seconds, llm.concurrency, llm.max_chars) == (5, 2, 500)
    assert (llm.failure_threshold, llm.cooldown_seconds) == (1, 10)
    assert llm.min_score == 0.75 and llm.skip_staff is False


@pytest.mark.parametrize(
    "name,value",
    [
        ("MODERATION_LLM_URL", "ftp://host/v1"),
        ("MODERATION_LLM_URL", "http://user:secret-pass@host/v1"),
        ("MODERATION_LLM_URL", "http://host/v1?key=secret-pass"),
        ("MODERATION_LLM_URL", "http:///v1"),
        ("MODERATION_LLM_URL", "http://host/v1 extra"),
        ("MODERATION_LLM_MODEL", "two words"),
        ("MODERATION_LLM_MODEL", "x" * 201),
        ("MODERATION_LLM_TIMEOUT_SECONDS", "0"),
        ("MODERATION_LLM_CONCURRENCY", "3"),
        ("MODERATION_LLM_MAX_CHARS", "99"),
        ("MODERATION_LLM_FAILURE_THRESHOLD", "0"),
        ("MODERATION_LLM_COOLDOWN_SECONDS", "86401"),
        ("MODERATION_LLM_MIN_SCORE", "1.5"),
        ("MODERATION_LLM_MIN_SCORE", "-0.1"),
        ("MODERATION_LLM_MIN_SCORE", "abc"),
        ("MODERATION_LLM_SKIP_STAFF", "maybe"),
        ("MODERATION_LLM_GUIDANCE_PATH", "bad\x00path"),
    ],
)
def test_invalid_llm_configuration_fails_without_echoing_values(name: str, value: str) -> None:
    env = {
        **BASE_ENV,
        "MODERATION_STAFF_CHANNEL_ID": "555",
        "MODERATION_LLM_URL": "http://localhost:11434/v1",
        "MODERATION_LLM_MODEL": "m",
        name: value,
    }
    with pytest.raises(ConfigurationError) as error:
        Settings.from_env(env)
    assert name in str(error.value)
    assert "secret" not in str(error.value)


# Rule numbers and prompt


def test_rule_numbers_come_from_the_headings() -> None:
    assert RULES.numbers == RULE_NUMBERS


def test_prompt_contains_rules_guidance_and_capped_content() -> None:
    messages = build_messages(RULES.text, GUIDANCE.text, "z" * 50, 10)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "# 2. No piracy" in messages[0]["content"]
    assert "Choosing the rule" in messages[0]["content"]
    assert "untrusted member" in messages[0]["content"]
    assert messages[1]["content"].count("z") == 10
    without = build_messages(RULES.text, None, "x", 10)
    assert "Guidance:" not in without[0]["content"]


# Verdict parsing


@pytest.mark.parametrize(
    "text,expected",
    [
        (verdict_json(2, 0.9, "piracy"), Verdict(2, 0.9, "piracy")),
        (verdict_json(None, 0), Verdict(None, 0.0, "because")),
        (f"```json\n{verdict_json(6, 1)}\n```", Verdict(6, 1.0, "because")),
        (
            '{"rule": 1, "score": 0.5, "reason": "  hey\\n @everyone  <@1> "}',
            Verdict(1, 0.5, "hey @​everyone <@​1>"),
        ),
    ],
)
def test_valid_verdicts_are_parsed_and_sanitized(text: str, expected: Verdict) -> None:
    assert parse_verdict(text, RULE_NUMBERS) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "safe",
        "The message is fine.",
        '{"rule": 2, "score": 0.9}',
        '{"rule": 2, "score": 0.9, "reason": "x", "action": "ban"}',
        '{"rule": 42, "score": 0.9, "reason": "x"}',
        '{"rule": "2", "score": 0.9, "reason": "x"}',
        '{"rule": 2, "score": 1.5, "reason": "x"}',
        '{"rule": 2, "score": -1, "reason": "x"}',
        '{"rule": 2, "score": true, "reason": "x"}',
        '{"rule": 2, "score": "0.9", "reason": "x"}',
        '{"rule": 2, "score": 0.9, "reason": 5}',
        '[{"rule": 2, "score": 0.9, "reason": "x"}]',
        'Sure! {"rule": 2, "score": 0.9, "reason": "x"}',
    ],
)
def test_off_schema_answers_give_no_verdict(text: str) -> None:
    assert parse_verdict(text, RULE_NUMBERS) is None


def test_reason_is_capped_and_completion_text_is_extracted_strictly() -> None:
    assert len(sanitize_reason("word " * 100)) == 200
    assert completion_text(completion("hi")) == "hi"
    assert completion_text({"choices": []}) is None
    assert completion_text({"choices": [{"message": {"content": None}}]}) is None
    assert completion_text("nope") is None


# Client behavior


def test_requests_carry_model_prompt_json_mode_and_bearer_key() -> None:
    endpoint = FakeEndpoint([verdict_json(2, 0.9, "cracked copy requested")])

    async def scenario(url: str) -> None:
        instance = classifier(url, api_key="key-secret")
        try:
            verdict = await instance.classify("where can I get a cracked exe")
        finally:
            await instance.close()
        assert verdict == Verdict(2, 0.9, "cracked copy requested")
        request = endpoint.requests[0]
        assert request["model"] == "test-model"
        assert request["response_format"] == {"type": "json_object"}
        assert request["temperature"] == 0 and request["stream"] is False
        messages = request["messages"]
        assert isinstance(messages, list) and "cracked exe" in messages[1]["content"]
        assert endpoint.headers[0]["Authorization"] == "Bearer key-secret"
        assert instance.counters == ClassifierCounters(requests=1, verdicts=1)

    run_with_server(endpoint, scenario)


def test_off_schema_and_oversized_answers_count_as_no_verdict_without_logging_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = FakeEndpoint(["the message quotes secret-content and is fine", "huge"])

    async def scenario(url: str) -> None:
        instance = classifier(url)
        try:
            assert await instance.classify("secret-content") is None
            assert await instance.classify("secret-content") is None
        finally:
            await instance.close()
        assert instance.counters == ClassifierCounters(requests=1, invalid=1, failures=1)

    with caplog.at_level(logging.DEBUG):
        run_with_server(endpoint, scenario)
    assert "secret-content" not in caplog.text


def test_failures_open_the_circuit_and_it_closes_after_the_cooldown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = FakeEndpoint([500, "garbage", 503, verdict_json(None, 0)])

    async def scenario(url: str) -> None:
        instance = classifier(url, failure_threshold=3, cooldown_seconds=1)
        try:
            for _ in range(3):
                assert await instance.classify("hello") is None
            assert not instance.available
            assert await instance.classify("hello") is None  # Skipped while open.
            assert len(endpoint.requests) == 3
            instance._open_until = asyncio.get_running_loop().time() - 1
            assert instance.available
            assert await instance.classify("hello") == Verdict(None, 0.0, "because")
            assert len(endpoint.requests) == 4
        finally:
            await instance.close()
        assert instance.counters == ClassifierCounters(
            requests=1, verdicts=1, failures=3, skipped_open=1
        )

    with caplog.at_level(logging.WARNING):
        run_with_server(endpoint, scenario)
    assert "Classifier unavailable for 1 s" in caplog.text
    assert "ClassifierError" in caplog.text


def test_timeouts_count_as_failures_and_requests_can_be_cancelled() -> None:
    endpoint = FakeEndpoint(["slow", "slow"])

    async def scenario(url: str) -> None:
        instance = classifier(url, timeout_seconds=1, failure_threshold=5)
        try:
            assert await instance.classify("hello") is None
            assert instance.counters.failures == 1
            task = asyncio.create_task(instance.classify("hello"))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await instance.close()

    run_with_server(endpoint, scenario)


def test_unreachable_endpoint_is_a_failure_not_a_crash() -> None:
    async def scenario() -> None:
        instance = classifier("http://127.0.0.1:9", failure_threshold=1, cooldown_seconds=60)
        try:
            assert await instance.classify("hello") is None
            assert not instance.available
        finally:
            await instance.close()

    asyncio.run(scenario())


def test_missing_rules_are_a_failure() -> None:
    endpoint = FakeEndpoint([verdict_json(2, 0.9)])

    async def scenario(url: str) -> None:
        instance = Classifier(
            LlmSettings(url=url, model="m"), rules_text=lambda: None, rule_numbers=lambda: ()
        )
        try:
            assert await instance.classify("x") is None
        finally:
            await instance.close()
        assert endpoint.requests == []
        assert instance.counters.failures == 1

    run_with_server(endpoint, scenario)


# Reviewer integration


def intake(number: int, content: str, *, staff: bool = False) -> IntakeMessage:
    return IntakeMessage(
        number, 10, 42, NOW, content, f"https://discord.com/channels/123/10/{number}", (), staff
    )


def reviewer_with(
    store: ActivityStore, instance: Classifier, **overrides: object
) -> tuple[StaffReviewer, Mock]:
    channel = Mock(spec=discord.TextChannel)
    channel.id = 555
    channel.guild = SimpleNamespace(id=123, me=object())
    channel.permissions_for = Mock(return_value=discord.Permissions(send_messages=True))
    channel.send = AsyncMock(return_value=Mock(id=9001))
    client = Mock()
    client.get_channel = Mock(return_value=channel)
    settings = ModerationSettings(
        staff_channel_id=555,
        repeat_threshold=50,
        llm=LlmSettings(url=instance.settings.url, model="m", **overrides),  # type: ignore[arg-type]
    )
    review = StaffReviewer(
        client,
        settings,
        store,
        guild_id=123,
        rules_revision=lambda: 1,
        clock=lambda: NOW,
        classifier=instance,
    )
    return review, channel


def test_model_verdicts_become_staff_flags_only_above_the_minimum_score(tmp_path: Path) -> None:
    endpoint = FakeEndpoint(
        [
            verdict_json(2, 0.9, "asks for a crack"),
            verdict_json(1, 0.3, "mild"),
            verdict_json(None, 0),
        ]
    )
    store = ActivityStore(tmp_path / "db.sqlite3")

    async def scenario(url: str) -> None:
        instance = classifier(url)
        review, channel = reviewer_with(store, instance)
        try:
            await review.review(intake(1, "any cracked exe around? secret-text"))
            await review.review(intake(2, "meh"))
            await review.review(intake(3, "hello"))
        finally:
            await instance.close()
            await store.close()
        channel.send.assert_awaited_once()
        post = channel.send.call_args.args[0]
        assert "Rule 2: model: asks for a crack (confidence 0.90, llm)" in post
        assert "secret-text" not in post
        assert len(endpoint.requests) == 3

    run_with_server(endpoint, scenario)


def test_deterministic_flags_skip_the_model_and_staff_and_empty_messages_are_not_sent(
    tmp_path: Path,
) -> None:
    endpoint = FakeEndpoint([verdict_json(None, 0)] * 5)
    store = ActivityStore(tmp_path / "db.sqlite3")

    async def scenario(url: str) -> None:
        instance = classifier(url)
        review, channel = reviewer_with(store, instance)
        try:
            await review.review(intake(1, "join discord.gg/abc"))  # Deterministic flag.
            await review.review(intake(2, "staff message", staff=True))
            await review.review(intake(3, "   "))
            await review.review(intake(4, "ordinary chatter"))
        finally:
            await instance.close()
            await store.close()
        assert len(endpoint.requests) == 1
        assert "ordinary chatter" in str(endpoint.requests[0]["messages"])
        assert channel.send.await_count == 1

    run_with_server(endpoint, scenario)


def test_staff_messages_are_classified_when_skip_staff_is_off(tmp_path: Path) -> None:
    endpoint = FakeEndpoint([verdict_json(None, 0)])
    store = ActivityStore(tmp_path / "db.sqlite3")

    async def scenario(url: str) -> None:
        instance = classifier(url)
        review, _ = reviewer_with(store, instance, skip_staff=False)
        try:
            await review.review(intake(1, "staff message", staff=True))
        finally:
            await instance.close()
            await store.close()
        assert len(endpoint.requests) == 1

    run_with_server(endpoint, scenario)


# Examples and evaluation


def test_repository_examples_are_well_formed_and_cover_every_rule_and_injection() -> None:
    examples = load_examples(EXAMPLES_PATH)
    assert len(examples) >= 25
    rules_covered = {example.rule for example in examples if example.rule is not None}
    assert rules_covered == {1, 2, 3, 5, 6, 7, 8, 9}
    assert sum(example.rule is None for example in examples) >= 8
    assert sum(example.id.startswith("injection-") for example in examples) >= 3
    assert all(example.rule in (None, *RULE_NUMBERS) for example in examples)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"examples": "x"}',
        '{"examples": [{"id": "a", "rule": 2}]}',
        '{"examples": [{"id": "a", "rule": "2", "text": "x"}]}',
        '{"examples": [{"id": "a", "rule": 2, "text": "x"}, {"id": "a", "rule": 2, "text": "y"}]}',
        '{"examples": [{"id": "", "rule": 2, "text": "x"}]}',
        "not json",
    ],
)
def test_malformed_examples_are_rejected(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "examples.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ExamplesError):
        load_examples(path)
    with pytest.raises(ExamplesError):
        load_examples(tmp_path / "missing.json")


def test_evaluation_reports_precision_recall_and_mismatches() -> None:
    examples = (
        Example("tp", 2, "crack please"),
        Example("fn", 2, "subtle crack request"),
        Example("fp", None, "fine"),
        Example("ok", None, "also fine"),
        Example("low", 1, "weak signal"),
        Example("down", 3, "cheats"),
    )
    endpoint = FakeEndpoint(
        [
            verdict_json(2, 0.9),
            verdict_json(None, 0),
            verdict_json(2, 0.8),
            verdict_json(None, 0),
            verdict_json(1, 0.2),
            500,
        ]
    )

    async def scenario(url: str) -> None:
        instance = classifier(url, failure_threshold=10)
        try:
            report = await evaluate_examples(instance, examples, min_score=0.5)
        finally:
            await instance.close()
        assert (report.total, report.correct, report.unavailable) == (6, 2, 1)
        assert report.per_rule[2].precision == 0.5 and report.per_rule[2].recall == 0.5
        assert report.per_rule[1].recall == 0 and report.per_rule[1].precision is None
        assert report.per_rule[3].recall == 0
        assert [m[0] for m in report.mismatches] == ["fn", "fp", "low", "down"]
        text = report.render()
        assert "Rule 2: precision 0.50, recall 0.50 (tp 1, fp 1, fn 1)" in text
        assert "Mismatch fn: expected 2, got None (score 0.00)" in text
        assert "Mismatch down: expected 3, got None (no verdict)" in text
        assert "crack please" not in text

    run_with_server(endpoint, scenario)


def test_evaluation_command_runs_against_the_repository_examples(
    capsys: pytest.CaptureFixture[str],
) -> None:
    examples = load_examples(EXAMPLES_PATH)
    endpoint = FakeEndpoint(
        [verdict_json(example.rule, 0.9 if example.rule else 0) for example in examples]
    )

    async def scenario(url: str) -> None:
        env = {"MODERATION_LLM_URL": url, "MODERATION_LLM_MODEL": "m"}
        code = await asyncio.to_thread(
            evaluate_main,
            [
                "--examples",
                str(EXAMPLES_PATH),
                "--rules",
                str(ROOT / "rules" / "community-rules.md"),
            ],
            env,
        )
        assert code == 0

    run_with_server(endpoint, scenario)
    output = capsys.readouterr().out
    assert f"Examples: {len(examples)}, correct: {len(examples)}, no verdict: 0." in output
    assert "Mismatch" not in output


def test_evaluation_command_rejects_missing_configuration() -> None:
    with pytest.raises(SystemExit) as error:
        evaluate_main([], {})
    assert error.value.code == 2
