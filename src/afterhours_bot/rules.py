# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The community rule text and its content digest, used for revision tracking."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

MAX_RULES_BYTES = 65_536
RULE_HEADING = re.compile(r"^#+\s*(\d{1,2})\.[ \t]*(.*)$", re.MULTILINE)


class ModerationRulesError(RuntimeError):
    """The rule file is missing, unreadable, empty or too large."""


@dataclass(frozen=True)
class RuleSet:
    text: str
    sha256: str

    @property
    def short_digest(self) -> str:
        return self.sha256[:12]

    @property
    def numbers(self) -> tuple[int, ...]:
        """Rule numbers from headings such as `# 2. No piracy`."""
        return tuple(sorted(self.titles))

    @property
    def titles(self) -> dict[int, str]:
        """Rule number to heading text, for example 2 -> 'No piracy'."""
        titles: dict[int, str] = {}
        for number, title in RULE_HEADING.findall(self.text):
            value = int(number)
            if 0 < value < 100 and value not in titles:
                titles[value] = " ".join(title.split()).rstrip(".") or f"rule {value}"
        return titles


def load_rules(path: Path, *, max_bytes: int = MAX_RULES_BYTES) -> RuleSet:
    """Read and normalize the rule file. Blocking I/O; call it off the event loop."""
    try:
        if path.stat().st_size > max_bytes:
            raise ModerationRulesError(f"The rule file exceeds {max_bytes} bytes.")
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ModerationRulesError("The rule file does not exist.") from None
    except OSError:
        raise ModerationRulesError("The rule file could not be read.") from None
    if len(raw) > max_bytes:
        raise ModerationRulesError(f"The rule file exceeds {max_bytes} bytes.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ModerationRulesError("The rule file must be UTF-8 text.") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ModerationRulesError("The rule file is empty.")
    text += "\n"
    return RuleSet(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
