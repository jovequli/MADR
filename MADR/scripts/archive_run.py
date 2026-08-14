#!/usr/bin/env python3
"""Tiered post-run archiver: shrink a finished run directory ~10x while
keeping everything that cannot be regenerated.

Retention policy (rationale: keep by *non-regenerability*, not by habit —
the physics is deterministic and can always be re-run from the stored
parameters; the LLM conversation cannot):

  Tier A — non-regenerable, keep forever (llm_calls.jsonl, provenance/,
           ledgers, round results/messages/leader_decision/round_meta,
           run_manifest, final_summary, exploration summaries, ABORTED_*).
           Never touched by this script.
  Tier B — expensive but regenerable, keep compressed:
           routing_result daily series (.day / .day_mm) are gzipped;
           derived aggregations (.month/.month_mm/.year/.year_mm/
           .end_of_month) are deleted — all recomputable from .day.
  Tier C — working files / caches, deleted on success:
           <sid>_calibration_ai/ (live working dir of the last round),
           cached_vic_runoff/, __pycache__/, .DS_Store.
  Checkpoints — langgraph_checkpoints.db(+wal/shm) is a resume mechanism,
           not an archive. Once the verbatim llm_calls.jsonl exists, the
           DB is VACUUMed into a compressed archive copy and removed.
           If llm_calls.jsonl is absent (pre-hook runs), the DB is KEPT —
           it is then the only replay source.

Safety rules:
  * Dry-run by default: prints the plan; nothing changes without --apply.
  * Refuses to run without run_manifest.json (not a run dir).
  * Refuses to clean an aborted run (ABORTED_*.md) unless --force:
    failed runs keep all working files for post-mortem and resume.
  * Never follows symlinks; only touches paths inside --run-dir.

Usage:
    python -m scripts.archive_run --run-dir /path/to/run          # dry-run
    python -m scripts.archive_run --run-dir /path/to/run --apply  # execute
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# routing_result extensions (text after the LAST dot in the file name;
# note station files look like "BB   .day" — name ends with the extension)
ROUTING_DELETE_EXTS = {"month", "month_mm", "year", "year_mm", "end_of_month"}
ROUTING_GZIP_EXTS = {"day", "day_mm"}

CACHE_DIR_NAMES = {"cached_vic_runoff", "__pycache__"}
NOISE_FILE_NAMES = {".DS_Store"}


def _size_of(path: Path) -> int:
    if path.is_file() and not path.is_symlink():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and not p.is_symlink():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _fmt(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} B"
        nbytes /= 1024.0
    return f"{nbytes:.1f} GB"


def _inside(run_dir: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(run_dir.resolve())
        return True
    except ValueError:
        return False


class Plan:
    """Collects planned actions; executes them only with apply=True."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.delete_dirs: List[Path] = []
        self.delete_files: List[Path] = []
        self.gzip_files: List[Path] = []
        self.notes: List[str] = []
        self.checkpoint_action: str = "keep (no decision yet)"

    def summarize(self) -> str:
        lines = []
        del_bytes = sum(_size_of(p) for p in self.delete_dirs) + \
            sum(_size_of(p) for p in self.delete_files)
        gz_bytes = sum(_size_of(p) for p in self.gzip_files)
        lines.append(f"  Tier C deletions: {len(self.delete_dirs)} dirs, "
                     f"{len(self.delete_files)} files  ({_fmt(del_bytes)})")
        lines.append(f"  Tier B gzip:      {len(self.gzip_files)} files "
                     f"({_fmt(gz_bytes)} before compression)")
        lines.append(f"  Checkpoints:      {self.checkpoint_action}")
        for n in self.notes:
            lines.append(f"  Note: {n}")
        return "\n".join(lines)


def build_plan(run_dir: Path, keep_checkpoints: bool,
               keep_aggregates: bool) -> Plan:
    plan = Plan(run_dir)

    # --- Tier C: working dirs and caches ---
    for p in sorted(run_dir.iterdir()):
        if p.is_symlink():
            plan.notes.append(f"symlink skipped: {p.name}")
            continue
        if p.is_dir() and (p.name.endswith("_calibration_ai")
                           or p.name in CACHE_DIR_NAMES):
            plan.delete_dirs.append(p)
    for p in run_dir.rglob("__pycache__"):
        if p.is_dir() and _inside(run_dir, p) and p not in plan.delete_dirs:
            plan.delete_dirs.append(p)
    for p in run_dir.rglob("*"):
        if p.name in NOISE_FILE_NAMES and p.is_file():
            plan.delete_files.append(p)

    # --- Tier B: routing_result pruning + compression ---
    for rr in sorted(run_dir.glob("round_*/routing_result")):
        for f in rr.rglob("*"):
            if not f.is_file() or f.is_symlink():
                continue
            if f.suffix == ".gz":
                continue
            ext = f.name.rsplit(".", 1)[-1].strip() if "." in f.name else ""
            if ext in ROUTING_DELETE_EXTS and not keep_aggregates:
                plan.delete_files.append(f)
            elif ext in ROUTING_GZIP_EXTS:
                plan.gzip_files.append(f)

    # --- Checkpoints ---
    db = run_dir / "langgraph_checkpoints.db"
    llm_log = run_dir / "llm_calls.jsonl"
    if not db.exists():
        plan.checkpoint_action = "none found"
    elif keep_checkpoints:
        plan.checkpoint_action = "keep (--keep-checkpoints)"
    elif llm_log.exists() and llm_log.stat().st_size > 0:
        plan.checkpoint_action = (
            "VACUUM INTO langgraph_checkpoints.vacuum.db.gz, "
            "then delete db/-wal/-shm (verbatim llm_calls.jsonl present)"
        )
    else:
        plan.checkpoint_action = (
            "KEEP — llm_calls.jsonl missing/empty; the checkpoint DB is the "
            "only replay source for this run"
        )
    return plan


def apply_plan(plan: Plan) -> None:
    run_dir = plan.run_dir
    for p in plan.delete_dirs:
        if _inside(run_dir, p) and p.exists():
            shutil.rmtree(p, ignore_errors=True)
    for p in plan.delete_files:
        if _inside(run_dir, p) and p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    for p in plan.gzip_files:
        if not (_inside(run_dir, p) and p.exists()):
            continue
        gz_path = p.with_name(p.name + ".gz")
        with open(p, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        p.unlink()

    if plan.checkpoint_action.startswith("VACUUM"):
        db = run_dir / "langgraph_checkpoints.db"
        vac = run_dir / "langgraph_checkpoints.vacuum.db"
        if vac.exists():
            vac.unlink()
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("VACUUM INTO ?", (str(vac),))
        finally:
            conn.close()
        gz_path = vac.with_name(vac.name + ".gz")
        with open(vac, "rb") as src, gzip.open(gz_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        vac.unlink()
        for side in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
            if side.exists():
                side.unlink()


def write_checksums(run_dir: Path) -> Path:
    sums_path = run_dir / "SHA256SUMS"
    entries: List[Tuple[str, str]] = []
    for p in sorted(run_dir.rglob("*")):
        if not p.is_file() or p.is_symlink() or p == sums_path:
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        entries.append((h.hexdigest(), str(p.relative_to(run_dir))))
    with sums_path.open("w", encoding="utf-8") as f:
        for digest, rel in entries:
            f.write(f"{digest}  {rel}\n")
    return sums_path


def write_retention_md(run_dir: Path, plan_summary: str,
                       size_before: int, size_after: int) -> None:
    text = f"""# RETENTION — tiered archive record

- Archived at: {dt.datetime.now(dt.timezone.utc).isoformat()}
- Size before: {_fmt(size_before)}
- Size after:  {_fmt(size_after)}

## Policy
Keep by non-regenerability. Tier A (non-regenerable: verbatim LLM calls,
provenance, ledgers, per-round results/messages/decisions) is never touched.
Tier B (regenerable but expensive: routing daily series) is gzipped; derived
aggregations (.month/.year/...) are deleted — recomputable from .day.
Tier C (working files: *_calibration_ai, caches, checkpoints after the
verbatim log exists) is removed on success. Aborted runs are not cleaned.

## Actions taken
{plan_summary}

## Verification
`SHA256SUMS` covers every remaining file; verify with:
    shasum -a 256 -c SHA256SUMS
"""
    (run_dir / "RETENTION.md").write_text(text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", required=True, help="Finished run directory")
    ap.add_argument("--apply", action="store_true",
                    help="Execute the plan (default: dry-run)")
    ap.add_argument("--keep-checkpoints", action="store_true",
                    help="Do not touch langgraph_checkpoints.db")
    ap.add_argument("--keep-aggregates", action="store_true",
                    help="Keep .month/.year/... routing aggregations")
    ap.add_argument("--force", action="store_true",
                    help="Archive even if the run has an ABORTED_*.md marker")
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        print(f"ERROR: not a directory: {run_dir}")
        return 2
    if not (run_dir / "run_manifest.json").exists():
        print(f"ERROR: {run_dir} has no run_manifest.json — refusing "
              f"(is this really a run directory?)")
        return 2

    aborted = sorted(run_dir.glob("ABORTED_*.md"))
    if aborted and not args.force:
        print(f"REFUSED: aborted run ({aborted[0].name}). Failed runs keep "
              f"all working files for resume/post-mortem. Use --force to "
              f"override.")
        return 3

    size_before = _size_of(run_dir)
    plan = build_plan(run_dir, args.keep_checkpoints, args.keep_aggregates)
    plan_summary = plan.summarize()  # capture sizes BEFORE anything is deleted

    print(f"Run dir: {run_dir}")
    print(f"Size:    {_fmt(size_before)}")
    print("Plan:")
    print(plan_summary)

    if not args.apply:
        print("\nDRY-RUN ONLY — nothing was changed. Re-run with --apply.")
        return 0

    apply_plan(plan)
    sums = write_checksums(run_dir)
    size_after = _size_of(run_dir)
    write_retention_md(run_dir, plan_summary, size_before, size_after)
    print(f"\nDone. {_fmt(size_before)} -> {_fmt(size_after)}")
    print(f"Checksums: {sums}")
    print(f"Record:    {run_dir / 'RETENTION.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
