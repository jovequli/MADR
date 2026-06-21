#!/usr/bin/env python3
"""CLI entry point for LangGraph-based SADR calibration.

Replaces orchestrator.main() with LangGraph graph execution.
Supports:
- --team-config: path to team configuration JSON
- --output-root: override output directory
- --resume: resume from last checkpoint
- --dry-run: skip LLM calls, use rule-based fallback
- --max-rounds: override max rounds
- --log-level: logging level

Usage:
    python -m scripts.run_langgraph \
        --team-config configs/bengbu_1979_1980.json \
        --output-root /path/to/outputs

    # Resume from checkpoint:
    python -m scripts.run_langgraph \
        --team-config configs/bengbu_1979_1980.json \
        --resume

    # Dry run (no LLM calls):
    python -m scripts.run_langgraph \
        --team-config configs/bengbu_1979_1980.json \
        --dry-run --max-rounds 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Ensure the project root is in sys.path
SKILL_ROOT = Path(__file__).parent.parent
PROJECT_ROOT = SKILL_ROOT.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langgraph.checkpoint.sqlite import SqliteSaver

from .graph_builder import compile_graph
from .graph_state import CalibrationState

logger = logging.getLogger(__name__)


def load_team_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LangGraph-based SADR calibration orchestrator."
    )
    p.add_argument(
        "--team-config", required=True,
        help="Path to team configuration JSON.",
    )
    p.add_argument(
        "--output-root", default="",
        help="Override output directory.",
    )
    p.add_argument(
        "--result-repo", default="",
        help="Result repository directory. Output will be placed in a timestamped subfolder.",
    )
    p.add_argument(
        "--run-name", default="",
        help="Short descriptive name for this run (e.g. 'gemini3flash_101r'). "
             "Used in the output folder name: <timestamp>_<run-name>.",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="Resume from latest checkpoint (via SqliteSaver).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip all LLM calls, use rule-based fallback.",
    )
    p.add_argument(
        "--max-rounds", type=int, default=0,
        help="Override max rounds (0 = use config/pipeline sum).",
    )
    p.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    p.add_argument(
        "--thread-id", default="default",
        help="Thread ID for checkpoint tracking (default: 'default').",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Load config
    cfg = load_team_config(args.team_config)

    # Determine output root (resolve to absolute path)
    if args.result_repo:
        # Auto-generate timestamped subfolder under result_repo
        repo = Path(args.result_repo).resolve()
        tag = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_name = args.run_name or cfg.get("run_name", "run")
        output_root = str(repo / f"{tag}_{run_name}")
    elif args.output_root:
        output_root = str(Path(args.output_root).resolve())
    elif cfg.get("output_root"):
        output_root = str(Path(cfg["output_root"]).resolve())
    else:
        tag = dt.datetime.now().strftime("%Y%m%d_%H%M")
        output_root = f"/tmp/agent_team_cali_lg_{tag}"
    Path(output_root).mkdir(parents=True, exist_ok=True)

    # Mode
    mode = cfg.get("model_binding", {}).get("mode", "simulate")

    # Set up SqliteSaver for checkpointing
    db_path = str(Path(output_root) / "langgraph_checkpoints.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    checkpointer = SqliteSaver(conn)

    # Build and compile graph
    app = compile_graph(checkpointer=checkpointer)
    print(f"LangGraph calibration graph compiled ({len(app.nodes)} nodes)")

    # Config for checkpointing
    config = {
        "configurable": {
            "thread_id": args.thread_id,
        }
    }

    if args.resume:
        # Resume from checkpoint — invoke with None input
        print(f"Resuming from checkpoint (thread_id={args.thread_id})...")
        try:
            state = app.get_state(config)
            if state and state.values:
                print(f"  Found checkpoint at round {state.values.get('round_id', '?')}")
                result = app.invoke(None, config)
            else:
                print("  No checkpoint found. Starting fresh.")
                args.resume = False
        except Exception as e:
            logger.warning("Failed to resume: %s. Starting fresh.", e)
            args.resume = False

    if not args.resume:
        # Fresh start — build initial state
        initial_state: CalibrationState = {
            "team_config": cfg,
            "team_config_path": args.team_config,
            "output_root": output_root,
            "mode": mode,
            "dry_run": args.dry_run,
            "max_rounds": args.max_rounds if args.max_rounds > 0 else 0,
        }

        if args.dry_run:
            print("[DRY-RUN] All LLM calls will use rule-based fallback")

        result = app.invoke(initial_state, config)

    # Print final summary
    if result and isinstance(result, dict):
        summary = result.get("final_summary", {})
        if summary:
            print(f"\n{'='*60}")
            print(f"  CALIBRATION COMPLETE")
            print(f"{'='*60}")
            print(f"  Rounds: {summary.get('rounds_executed', '?')}")
            print(f"  Best round: {summary.get('best_round', '?')}")
            print(f"  Best J_total: {summary.get('best_global_score', '?'):.5f}")
            print(f"  Output: {summary.get('output_root', '?')}")

    # Clean up
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
