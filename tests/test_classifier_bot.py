# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from afterhours_bot.__main__ import main
from afterhours_bot.bot import CommunityBot
from afterhours_bot.config import LlmSettings, ModerationSettings, Settings

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "rules" / "community-rules.md"
GUIDANCE = ROOT / "rules" / "classifier-guidance.md"


@pytest.fixture(autouse=True)
def isolated_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


def settings(guidance: Path | None = GUIDANCE) -> Settings:
    return Settings(
        token="example-secret",  # noqa: S106
        guild_id=123,
        moderation=ModerationSettings(
            staff_channel_id=555,
            rules_path=RULES,
            llm=LlmSettings(
                url="http://127.0.0.1:9/v1", model="test-model", guidance_path=guidance
            ),
        ),
    )


def test_classifier_is_wired_reports_status_and_closes_with_the_bot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async with CommunityBot(settings()) as bot:
            assert bot.classifier is not None
            assert bot.reviewer is not None and bot.reviewer.classifier is bot.classifier
            await bot.setup_hook()
            assert bot.rules is not None and bot.rules.numbers == (1, 2, 3, 4, 5, 6, 7, 8, 9)
            assert bot.guidance is not None
            status = await bot.moderation_status()
            assert "Classifier: available, model test-model with guidance" in status
            assert "0 answers, 0 verdicts" in status
            with patch.object(bot.classifier, "close", new_callable=AsyncMock) as close:
                await bot.close()
                close.assert_awaited_once()

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Classifier guidance loaded" in caplog.text


def test_missing_guidance_is_a_warning_not_a_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        async with CommunityBot(settings(tmp_path / "missing-secret.md")) as bot:
            await bot.setup_hook()
            assert bot.guidance is None
            assert "without guidance" in await bot.moderation_status()
        async with CommunityBot(settings(None)) as bot:
            await bot.setup_hook()
            assert bot.guidance is None

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert "Classifier guidance not loaded" in caplog.text
    assert "missing-secret" not in caplog.text


def test_classifier_is_off_without_llm_settings() -> None:
    async def scenario() -> None:
        config = Settings(
            token="example-secret",  # noqa: S106
            guild_id=123,
            moderation=ModerationSettings(staff_channel_id=555, rules_path=RULES),
        )
        async with CommunityBot(config) as bot:
            assert bot.classifier is None
            assert "Classifier: off" in await bot.moderation_status()

    asyncio.run(scenario())


def test_configuration_check_reports_the_classifier_offline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "example-secret")
    monkeypatch.setenv("DISCORD_GUILD_ID", "123")
    monkeypatch.setenv("MODERATION_STAFF_CHANNEL_ID", "555")
    monkeypatch.setenv("MODERATION_RULES_PATH", str(RULES))
    monkeypatch.setenv("MODERATION_LLM_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("MODERATION_LLM_MODEL", "test-model")
    monkeypatch.setenv("MODERATION_LLM_GUIDANCE_PATH", str(GUIDANCE))
    monkeypatch.setenv("MODERATION_LLM_API_KEY", "key-secret")
    with patch("afterhours_bot.__main__.run_bot", new_callable=AsyncMock) as run:
        with caplog.at_level(logging.INFO):
            assert main(["--check-config"]) == 0
        run.assert_not_awaited()
    assert "rules 1, 2, 3, 4, 5, 6, 7, 8, 9" in caplog.text
    assert "Classifier configured: model test-model, guidance" in caplog.text
    assert "key-secret" not in caplog.text
