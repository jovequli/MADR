#!/usr/bin/env python3
"""Token budget manager for Leader LLM context.

Solves MADR's token explosion by enforcing a hard budget on the
Leader prompt. Sections are prioritized: low-priority sections get
compressed or dropped first when the budget is exceeded.

Usage:
    ctx = ContextManager(budget=50000)
    ctx.add_section("system", system_prompt, priority=1)
    ctx.add_section("results", diagnostics, priority=1,
                    min_tokens=500, compressor=compress_diagnostics)
    ctx.add_section("history", history_block, priority=3,
                    compressor=compress_history)
    ctx.add_section("centers", centers_json, priority=4)
    prompt = ctx.build()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Rough token estimation: ~4 chars per token for English/code,
# ~2 chars per token for Chinese text.
CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def compress_by_truncation(text: str, target_tokens: int) -> str:
    """Simple compressor: truncate to target token count."""
    target_chars = int(target_tokens * CHARS_PER_TOKEN)
    if len(text) <= target_chars:
        return text
    return text[:target_chars] + "\n... [truncated to fit token budget]"


def compress_history(text: str, target_tokens: int) -> str:
    """Compress history by keeping only the most recent rounds.

    Assumes history is formatted with "Round N:" headers.
    """
    lines = text.split("\n")
    target_chars = int(target_tokens * CHARS_PER_TOKEN)

    # Keep lines from the end until we exceed budget
    kept = []
    total = 0
    for line in reversed(lines):
        total += len(line) + 1
        if total > target_chars and kept:
            break
        kept.append(line)

    if len(kept) < len(lines):
        result = "[earlier rounds omitted]\n" + "\n".join(reversed(kept))
    else:
        result = "\n".join(reversed(kept))
    return result


def compress_diagnostics(text: str, target_tokens: int) -> str:
    """Compress diagnostics by keeping header + per-basin summaries."""
    lines = text.split("\n")
    target_chars = int(target_tokens * CHARS_PER_TOKEN)

    # Keep all lines up to target
    kept = []
    total = 0
    for line in lines:
        total += len(line) + 1
        if total > target_chars:
            kept.append("... [remaining diagnostics omitted]")
            break
        kept.append(line)
    return "\n".join(kept)


@dataclass
class Section:
    """A named section in the Leader prompt."""
    name: str
    content: str
    priority: int                                   # 1=must include, higher=lower priority
    min_tokens: int = 0                             # minimum tokens to keep (for compressible sections)
    compressor: Optional[Callable[[str, int], str]] = None  # compression function
    tokens: int = 0                                 # estimated tokens (auto-computed)

    def __post_init__(self):
        self.tokens = estimate_tokens(self.content)


class ContextManager:
    """Manages token budget for Leader LLM prompt construction.

    Sections are added with priorities:
    - Priority 1: always included (system prompt, current results)
    - Priority 2: compressed if over budget
    - Priority 3+: compressed then dropped if still over budget

    Build order: sections are concatenated in insertion order.
    Budget enforcement: higher-priority-number sections are compressed/dropped first.
    """

    def __init__(self, budget: int = 50000):
        self.budget = budget
        self.sections: List[Section] = []

    def add_section(
        self,
        name: str,
        content: str,
        priority: int = 2,
        min_tokens: int = 0,
        compressor: Optional[Callable[[str, int], str]] = None,
    ):
        """Add a section to the prompt.

        Args:
            name: Section identifier.
            content: The text content.
            priority: 1=must include, higher=lower priority.
            min_tokens: Minimum tokens to keep after compression (0=can drop).
            compressor: Function(text, target_tokens) -> compressed_text.
        """
        self.sections.append(Section(
            name=name,
            content=content,
            priority=priority,
            min_tokens=min_tokens,
            compressor=compressor,
        ))

    def build(self) -> str:
        """Build the final prompt within token budget.

        Strategy:
        1. Compute total tokens.
        2. If within budget, return as-is.
        3. Otherwise, process sections from highest priority number (least important):
           a. If section has compressor, compress to min_tokens.
           b. If section has no compressor and min_tokens=0, drop it.
        4. If still over budget after all compressions, truncate lowest-priority sections.
        """
        total = sum(s.tokens for s in self.sections)

        if total <= self.budget:
            return self._concat()

        # Need to reduce: sort by priority (highest number = drop first)
        overage = total - self.budget
        logger.info("Context over budget by ~%d tokens. Compressing.", overage)

        # Process from lowest priority (highest number) to highest priority
        sections_by_priority = sorted(
            range(len(self.sections)),
            key=lambda i: self.sections[i].priority,
            reverse=True,
        )

        for idx in sections_by_priority:
            if overage <= 0:
                break
            section = self.sections[idx]

            if section.priority <= 1:
                # Never compress/drop priority 1 sections
                continue

            if section.compressor:
                # Compress to min_tokens (or smaller)
                old_tokens = section.tokens
                target = max(section.min_tokens, section.tokens - overage)
                if target < section.tokens:
                    section.content = section.compressor(section.content, target)
                    section.tokens = estimate_tokens(section.content)
                    saved = old_tokens - section.tokens
                    overage -= saved
                    logger.info("  Compressed '%s': %d -> %d tokens (saved %d)",
                                section.name, old_tokens, section.tokens, saved)
            elif section.min_tokens == 0:
                # Drop entirely
                old_tokens = section.tokens
                section.content = ""
                section.tokens = 0
                overage -= old_tokens
                logger.info("  Dropped '%s': saved %d tokens", section.name, old_tokens)

        if overage > 0:
            logger.warning("Still over budget by ~%d tokens after compression.", overage)

        return self._concat()

    def _concat(self) -> str:
        """Concatenate all non-empty sections."""
        parts = [s.content for s in self.sections if s.content]
        return "\n\n".join(parts)

    def summary(self) -> Dict[str, int]:
        """Return token usage per section."""
        return {s.name: s.tokens for s in self.sections}

    @property
    def total_tokens(self) -> int:
        return sum(s.tokens for s in self.sections)
