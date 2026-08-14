"""Prompts directory resolver.

The MADR.cli prompt tree is sharded by model combination:

    prompts/
        vic_routing/...
        vic_cama_flood/...
        ...

`resolve_prompts_dir` returns the right subfolder for a given combination.

Default is ``vic_routing`` so configs without a ``model_combination`` field
keep working bit-identical with pre-refactor behaviour.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

DEFAULT_COMBINATION = "vic_routing"

_PROMPTS_ROOT = Path(__file__).parent.parent / "prompts"


def resolve_prompts_dir(combination: Optional[str] = None) -> Path:
    """Return the prompts directory for the requested model combination.

    Args:
        combination: e.g. ``"vic_routing"`` or ``"vic_cama_flood"``. ``None``
            or empty string falls back to ``DEFAULT_COMBINATION``.

    Raises:
        FileNotFoundError: if the requested subfolder does not exist.
    """
    name = (combination or DEFAULT_COMBINATION).strip() or DEFAULT_COMBINATION
    path = _PROMPTS_ROOT / name
    if not path.is_dir():
        raise FileNotFoundError(
            f"No prompts directory for model combination {name!r}: "
            f"expected {path}. Available: "
            f"{sorted(p.name for p in _PROMPTS_ROOT.iterdir() if p.is_dir())}"
        )
    return path


def available_combinations() -> list[str]:
    """List combinations that have a prompts subfolder on disk."""
    if not _PROMPTS_ROOT.is_dir():
        return []
    return sorted(p.name for p in _PROMPTS_ROOT.iterdir() if p.is_dir())
