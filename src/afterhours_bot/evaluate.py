# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measure a configured classifier against labelled synthetic examples. Manual, offline tests."""

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from afterhours_bot.classifier import Classifier
from afterhours_bot.config import ConfigurationError, LlmSettings, parse_llm_settings
from afterhours_bot.rules import ModerationRulesError, RuleSet, load_rules

logger = logging.getLogger(__name__)
MAX_EXAMPLES = 10_000
MAX_EXAMPLE_CHARS = 20_000


class ExamplesError(ValueError):
    """The examples file is missing or malformed."""


@dataclass(frozen=True)
class Example:
    id: str
    rule: int | None
    text: str


@dataclass(frozen=True)
class RuleScore:
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def precision(self) -> float | None:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else None

    @property
    def recall(self) -> float | None:
        actual = self.true_positives + self.false_negatives
        return self.true_positives / actual if actual else None


@dataclass
class Report:
    total: int = 0
    correct: int = 0
    unavailable: int = 0
    per_rule: dict[int, RuleScore] = field(default_factory=dict)
    mismatches: list[tuple[str, int | None, int | None]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Examples: {self.total}, correct: {self.correct}, no verdict: {self.unavailable}."
        ]
        for rule in sorted(self.per_rule):
            score = self.per_rule[rule]
            precision = "n/a" if score.precision is None else f"{score.precision:.2f}"
            recall = "n/a" if score.recall is None else f"{score.recall:.2f}"
            lines.append(
                f"Rule {rule}: precision {precision}, recall {recall} "
                f"(tp {score.true_positives}, fp {score.false_positives}, "
                f"fn {score.false_negatives})"
            )
        for example_id, expected, predicted in self.mismatches:
            lines.append(f"Mismatch {example_id}: expected {expected}, got {predicted}")
        return "\n".join(lines)


def load_examples(path: Path) -> tuple[Example, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ExamplesError("The examples file does not exist.") from None
    except OSError, ValueError:
        raise ExamplesError("The examples file could not be read as JSON.") from None
    items = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) > MAX_EXAMPLES:
        raise ExamplesError("The examples file needs an 'examples' list.")
    examples: list[Example] = []
    seen: set[str] = set()
    for item in items:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or item["id"] in seen
            or not (item.get("rule") is None or type(item.get("rule")) is int)
            or not isinstance(item.get("text"), str)
            or not item["text"].strip()
            or len(item["text"]) > MAX_EXAMPLE_CHARS
        ):
            raise ExamplesError("Each example needs a unique id, a rule number or null, and text.")
        seen.add(item["id"])
        examples.append(Example(item["id"], item["rule"], item["text"]))
    return tuple(examples)


async def evaluate_examples(
    classifier: Classifier, examples: Sequence[Example], *, min_score: float
) -> Report:
    report = Report(total=len(examples))
    for example in examples:
        verdict = await classifier.classify(example.text)
        if verdict is None:
            report.unavailable += 1
            predicted: int | None = None
        else:
            predicted = verdict.rule if verdict.score >= min_score else None
        if predicted == example.rule:
            report.correct += 1
        else:
            report.mismatches.append((example.id, example.rule, predicted))
        for rule in {value for value in (example.rule, predicted) if value is not None}:
            score = report.per_rule.get(rule, RuleScore())
            if predicted == example.rule == rule:
                score = RuleScore(
                    score.true_positives + 1, score.false_positives, score.false_negatives
                )
            elif predicted == rule:
                score = RuleScore(
                    score.true_positives, score.false_positives + 1, score.false_negatives
                )
            elif example.rule == rule:
                score = RuleScore(
                    score.true_positives, score.false_positives, score.false_negatives + 1
                )
            report.per_rule[rule] = score
    return report


async def run(
    settings: LlmSettings, rules: RuleSet, guidance: RuleSet | None, examples: Sequence[Example]
) -> Report:
    classifier = Classifier(
        settings,
        rules_text=lambda: rules.text,
        rule_numbers=lambda: rules.numbers,
        guidance_text=lambda: guidance.text if guidance is not None else None,
    )
    try:
        return await evaluate_examples(classifier, examples, min_score=settings.min_score)
    finally:
        await classifier.close()


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the configured moderation classifier against labelled examples."
    )
    parser.add_argument(
        "--examples",
        type=Path,
        default=Path("rules/classifier-examples.json"),
        help="Labelled examples JSON (default: rules/classifier-examples.json).",
    )
    parser.add_argument(
        "--rules",
        type=Path,
        default=Path("rules/community-rules.md"),
        help="Rule text to judge against (default: rules/community-rules.md).",
    )
    args = parser.parse_args(argv)
    environment = os.environ if env is None else env
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = parse_llm_settings(environment)
        if settings is None:
            raise ConfigurationError("MODERATION_LLM_URL and MODERATION_LLM_MODEL are required.")
        rules = load_rules(args.rules)
        guidance = (
            load_rules(settings.guidance_path)
            if settings.guidance_path is not None and settings.guidance_path.exists()
            else None
        )
        examples = load_examples(args.examples)
    except (ConfigurationError, ModerationRulesError, ExamplesError) as error:
        parser.error(str(error))
    report = asyncio.run(run(settings, rules, guidance, examples))
    print(report.render())
    return 0 if report.unavailable == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
