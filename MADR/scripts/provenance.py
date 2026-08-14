#!/usr/bin/env python3
"""Run-start provenance snapshot.

Called once by run_langgraph.main() right after the output directory is
created. Writes an immutable record of *what produced this run* so that any
result can later be traced to exact configuration, prompt templates, code
version, and environment — without relying on files that live outside the
run directory (which may change after the run).

Everything here is strictly additive and failure-tolerant: a provenance
failure logs a warning but never blocks the calibration run.

Output layout (inside <output_root>/provenance/):
    config_snapshot.json   fully-resolved team config CONTENT (not a path),
                           plus source path, CLI argv, and timestamp
    prompts/               verbatim copy of the prompt template tree
    code_version.txt       git commit hash + dirty status (or "unknown")
    environment.json       python/platform/hostname + key package versions
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_KEY_PACKAGES = (
    "anthropic",
    "openai",
    "google-genai",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "numpy",
    "pandas",
    "netCDF4",
)


def _git_version(repo_dir: Path) -> str:
    """Return '<hash> (clean|dirty: N files)' or an explanatory fallback."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        if head.returncode != 0:
            return f"unknown (not a git repository: {repo_dir})"
        commit = head.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=10,
        )
        dirty_lines = [ln for ln in status.stdout.splitlines() if ln.strip()]
        state = "clean" if not dirty_lines else f"dirty: {len(dirty_lines)} files"
        return f"{commit} ({state})"
    except Exception as e:  # git missing, timeout, ...
        return f"unknown ({type(e).__name__}: {e})"


def _package_versions() -> Dict[str, str]:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover
        return {}
    out: Dict[str, str] = {}
    for name in _KEY_PACKAGES:
        try:
            out[name] = metadata.version(name)
        except Exception:
            out[name] = "not installed"
    return out


def write_provenance(
    output_root: Path,
    resolved_config: Dict[str, Any],
    config_source_path: str = "",
    project_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Write the provenance snapshot. Returns the directory (or None on failure).

    Args:
        output_root: the run output directory (already created)
        resolved_config: the team config as actually loaded/merged in memory
        config_source_path: path the config was loaded from (recorded only)
        project_dir: package root containing prompts/ (defaults to this file's
            parent's parent, i.e. the MADR package directory)
    """
    try:
        output_root = Path(output_root)
        prov = output_root / "provenance"
        prov.mkdir(parents=True, exist_ok=True)
        pkg_dir = Path(project_dir) if project_dir else Path(__file__).parent.parent

        # 1. Config snapshot — the CONTENT actually used, not a pointer.
        snapshot = {
            "_written_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "_config_source_path": str(Path(config_source_path).resolve())
            if config_source_path else "",
            "_argv": sys.argv,
            "team_config": resolved_config,
        }
        with (prov / "config_snapshot.json").open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2, default=str)

        # 2. Prompt templates — verbatim copy (small; a few tens of KB).
        prompts_src = pkg_dir / "prompts"
        if prompts_src.is_dir():
            shutil.copytree(prompts_src, prov / "prompts", dirs_exist_ok=True)
        else:
            logger.warning("Provenance: prompts dir not found at %s", prompts_src)

        # 3. Code version.
        (prov / "code_version.txt").write_text(
            _git_version(pkg_dir) + "\n", encoding="utf-8"
        )

        # 4. Environment.
        env = {
            "python": sys.version,
            "platform": platform.platform(),
            "hostname": socket.gethostname(),
            "packages": _package_versions(),
        }
        with (prov / "environment.json").open("w", encoding="utf-8") as f:
            json.dump(env, f, ensure_ascii=False, indent=2)

        logger.info("Provenance snapshot written to %s", prov)
        return prov
    except Exception:
        logger.warning("Provenance snapshot failed (non-fatal)", exc_info=True)
        return None
