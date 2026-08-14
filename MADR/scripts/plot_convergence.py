#!/usr/bin/env python3
"""Post-run convergence plot: per-round NSE for every station, one PNG per run.

Reads only Tier-A files (round_*/results/worker_<sid>.json), so it works on
both fresh and archived runs. Style intentionally minimal per project
convention: no title, just axes and a compact legend.

Plotted elements:
  * one line per station through the ADOPTED candidate's NSE each round
  * multi-candidate rounds: every candidate as a small hollow marker
    (the adopted one sits on the line; rejected ones float off it)

Usage:
    python -m scripts.plot_convergence --run-dir /path/to/run
    python -m scripts.plot_convergence --run-dir /path/to/run --out /tmp/x.png

Called automatically at the end of run_langgraph (failure-tolerant: missing
matplotlib or malformed rounds never block the run).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_ROUND_RE = re.compile(r"round_(\d+)$")


def _collect(run_dir: Path) -> Dict[str, Dict[int, dict]]:
    """Return {station: {round_id: {"adopted": nse, "candidates": [(nse, adopted_bool), ...]}}}."""
    data: Dict[str, Dict[int, dict]] = {}
    for rdir in sorted(run_dir.glob("round_*")):
        m = _ROUND_RE.search(rdir.name)
        if not m:
            continue
        rid = int(m.group(1))
        for wf in sorted(rdir.glob("results/worker_*.json")):
            if wf.name.endswith("_diagnostic.json"):
                continue
            sid = wf.stem.replace("worker_", "")
            try:
                obj = json.loads(wf.read_text(encoding="utf-8"))
            except Exception:
                continue
            result = obj.get("result") or {}
            metrics = result.get("metrics") or {}
            adopted_nse = metrics.get("NSE")
            if adopted_nse is None:
                continue
            entry = {"adopted": float(adopted_nse), "candidates": []}
            win_label = obj.get("winning_strategy", "")
            mc = obj.get("mc_detail") or []
            if isinstance(mc, list) and len(mc) > 1:
                for c in mc:
                    try:
                        nse = float(c.get("nse"))
                    except (TypeError, ValueError):
                        continue
                    is_win = bool(c.get("label", "") == win_label) if win_label \
                        else bool(c.get("is_winner", False))
                    entry["candidates"].append((nse, is_win))
            data.setdefault(sid, {})[rid] = entry
    return data


def plot_run(run_dir: Path, out_path: Optional[Path] = None) -> Optional[Path]:
    """Render the convergence figure. Returns the PNG path (None on failure)."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless-safe (Linux server, no display)
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("matplotlib not installed — convergence plot skipped")
        return None

    run_dir = Path(run_dir)
    data = _collect(run_dir)
    if not data:
        logger.warning("No per-round worker results under %s — plot skipped", run_dir)
        return None
    out_path = Path(out_path) if out_path else run_dir / "convergence.png"

    # CJK-safe legend: if any station id is non-ASCII, prepend CJK-capable
    # fonts (macOS / Linux / Windows candidates) so names don't render as boxes.
    if any(not sid.isascii() for sid in data):
        matplotlib.rcParams["font.sans-serif"] = [
            "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC",
            "Source Han Sans SC", "Microsoft YaHei", "SimHei",
        ] + matplotlib.rcParams["font.sans-serif"]
        matplotlib.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(8, 4.5))
    colors = plt.cm.tab10.colors
    y_min = 1.0

    for i, sid in enumerate(sorted(data.keys())):
        color = colors[i % len(colors)]
        rounds = sorted(data[sid].keys())
        adopted = [data[sid][r]["adopted"] for r in rounds]
        y_min = min(y_min, min(adopted))
        ax.plot(rounds, adopted, "-", color=color, linewidth=1.2,
                label=sid, zorder=3)
        # multi-candidate rounds: all candidates as small hollow markers
        mc_x, mc_y = [], []
        for r in rounds:
            for nse, is_win in data[sid][r]["candidates"]:
                if not is_win:  # adopted point already sits on the line
                    mc_x.append(r)
                    mc_y.append(nse)
        if mc_x:
            y_min = min(y_min, min(mc_y))
            ax.scatter(mc_x, mc_y, s=12, facecolors="none",
                       edgecolors=color, linewidths=0.7, alpha=0.55, zorder=2)

    # one legend entry explaining the hollow markers (station-agnostic)
    ax.scatter([], [], s=12, facecolors="none", edgecolors="gray",
               linewidths=0.7, label="rejected candidates")

    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_xlabel("Round")
    ax.set_ylabel("NSE")
    ax.set_ylim(bottom=max(-1.0, y_min - 0.05), top=1.0)
    ax.grid(True, linewidth=0.3, alpha=0.4)
    ax.legend(fontsize=8, frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    logger.info("Convergence plot written to %s", out_path)
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--out", default="",
                    help="Output PNG path (default: <run-dir>/convergence.png)")
    args = ap.parse_args()
    out = plot_run(Path(args.run_dir), Path(args.out) if args.out else None)
    if out is None:
        print("plot failed (see warnings)")
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
