"""The three guidance setups, and the system prompt each one builds."""

import re
from enum import StrEnum
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKILL_PATH = REPO / "skills" / "use-browser" / "SKILL.md"

BASE_PROMPT = "You complete the task that the user gives you. Use the browser tools that you have."


class Setup(StrEnum):
    """`skill` is the skill text plus the server instructions string."""

    SKILL = "skill"
    INSTRUCTIONS = "instructions"
    NONE = "none"


class SkillMissing(Exception):
    """The skill file does not exist yet, so the setup that needs it cannot run."""


def read_skill(path: Path = SKILL_PATH) -> str:
    """The skill text without its front matter."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise SkillMissing(f"The skill file {path} does not exist. The setup {Setup.SKILL.value!r} needs it.") from None
    return re.sub(r"\A---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL).strip()


def build_system_prompt(setup: Setup, instructions: str | None, skill_path: Path = SKILL_PATH) -> str:
    """The base prompt, then the server instructions string, then the skill text, as the setup says."""
    parts = [BASE_PROMPT]
    if setup in (Setup.INSTRUCTIONS, Setup.SKILL) and instructions:
        parts.append(instructions)
    if setup is Setup.SKILL:
        parts.append(read_skill(skill_path))
    return "\n\n".join(parts)
