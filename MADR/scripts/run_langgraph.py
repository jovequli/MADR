#!/usr/bin/env python3
"""Run MADR calibration from a JSON team configuration.

Supports output selection, checkpoint resume and neutral dry-run fallback. The dry-run option disables LLM calls only; physical execution still follows model_binding.mode."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Ensure the project root is in sys.path
PACKAGE_ROOT = Path(__file__).parent.parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langgraph.checkpoint.sqlite import SqliteSaver

from .graph_builder import compile_graph
from .graph_state import CalibrationState



def load_team_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)
    from .execution_protocol import validate_static_input_protocol, validate_worker_evidence_mode
    config["static_input_protocol"] = validate_static_input_protocol(config.get("static_input_protocol"))
    config["worker_evidence_mode"] = validate_worker_evidence_mode(config.get("worker_evidence_mode", "existing"))
    from .prompt_visibility import validate_visibility_config
    config["prompt_visibility_protocol"] = validate_visibility_config(config, method=config.get("method", "MADR"))
    return config


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="LangGraph-based MADR calibration orchestrator."
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
        help="Short descriptive name for this run (e.g. 'madr_sonnet_rep1'). "
             "Used in the output folder name: <timestamp>_<run-name>.",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="Resume from latest checkpoint (via SqliteSaver).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Skip all LLM calls, use neutral center/hold fallback.",
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

    from .execution_protocol import validate_periods, runtime_context
    from .run_provenance import save_run_snapshot, seed_local_randomness
    validate_periods(cfg)
    runtime_context(cfg)
    from .phase_config import load_phase_pipeline, get_total_max_rounds
    pipeline = load_phase_pipeline(cfg)
    configured_max = cfg.get("max_rounds", 0)
    if type(configured_max) is not int or configured_max < 0 or args.max_rounds < 0:
        raise ValueError("max_rounds must be a nonnegative integer (0 uses pipeline sum)")
    max_rounds = args.max_rounds or configured_max or get_total_max_rounds(pipeline)
    if not args.resume:
        seed_local_randomness(cfg)

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
        output_root = f"/tmp/madr_run_{tag}"
    Path(output_root).mkdir(parents=True, exist_ok=True)

    save_run_snapshot(PACKAGE_ROOT, output_root, cfg, vars(args))

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
        "recursion_limit": 100 + max_rounds * (20 + 10 * len(cfg["workers"])),
        "configurable": {
            "thread_id": args.thread_id,
        }
    }

    if args.resume:
        # Resume from checkpoint — invoke with None input
        print(f"Resuming from checkpoint (thread_id={args.thread_id})...")
        state = app.get_state(config)
        if not state or not state.values:
            conn.close()
            raise ValueError("Resume refused: no checkpoint for this thread; use a new directory for a fresh run")
        saved = state.values
        # A native/LLM batch is not a transaction. Do not replay a partial batch
        # or manufacture a fresh run in the existing audit directory.
        if state.next and tuple(state.next) != ("round_start",):
            conn.close()
            raise ValueError("Resume requires a clean round_start boundary; mid-round replay is intentionally refused")
        if saved.get("max_rounds") != max_rounds or saved.get("dry_run") != args.dry_run:
            conn.close()
            raise ValueError("Resume refused: effective max_rounds or dry_run differs")
        # Validate actual inputs before even a late/final checkpoint can resume.
        from .graph_nodes import _get_shared, _SHARED
        _SHARED.clear()
        _get_shared(saved, prepare_cache=bool(state.next))
        print(f"  Found checkpoint at round {saved.get('round_id', '?')}")
        result = app.invoke(None, config) if state.next else saved

    if not args.resume:
        # Fresh start — build initial state
        initial_state: CalibrationState = {
            "team_config": cfg,
            "team_config_path": args.team_config,
            "output_root": output_root,
            "mode": mode,
            "dry_run": args.dry_run,
            "max_rounds": max_rounds,
        }

        if args.dry_run:
            print("[DRY-RUN] All LLM calls will use neutral center/hold fallback")

        result = app.invoke(initial_state, config)

    # A saved incomplete audit must not be advertised as successful completion.
    summary = result.get("final_summary", {}) if isinstance(result, dict) else {}
    audit_ok = summary.get("audit_complete") is True
    if result and isinstance(result, dict):
        summary = result.get("final_summary", {})
        if summary:
            print(f"\n{'='*60}")
            print("  CALIBRATION COMPLETE" if audit_ok else "  CALIBRATION INCOMPLETE: AUDIT FAILED")
            print(f"{'='*60}")
            print(f"  Rounds: {summary.get('rounds_executed', '?')}")
            print(f"  Best round: {summary.get('best_round', '?')}")
            print(f"  Representative mean NSE: {summary.get('best_mean_nse')}")
            print(f"  Online best J_total (separate): {summary.get('online_best_global_j_total', {}).get('score')}")
            print(f"  Output: {summary.get('output_root', '?')}")

    # Clean up
    conn.close()
    return 0 if audit_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
