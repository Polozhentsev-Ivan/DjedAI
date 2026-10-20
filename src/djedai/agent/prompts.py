from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """Loads a versioned prompt from the root prompts/ directory."""
    prompt_file = PROMPTS_DIR / f"{name}.md"
    if not prompt_file.exists():
        return "Промпт не найден."

    return prompt_file.read_text(encoding="utf-8")
