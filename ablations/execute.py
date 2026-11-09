"""Run one frozen ablation configuration through the shared graph entry."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--team-config', required=True)
    args, _ = parser.parse_known_args()
    config = json.loads(Path(args.team_config).read_text())
    from ablations.runtime import install, protocol
    selected = protocol(config.get('ablation_protocol'))
    if selected['arm'] == 'A3_strict_sequential' and 'sequential_station_context' not in config:
        from ablations.sequential_intervention import execute
        summary = execute(config, Path(config['output_root']))
        return 0 if summary.get('audit_complete') is True else 1
    install(config)
    from scripts.run_langgraph import main as run_graph
    return run_graph()


if __name__ == '__main__':
    raise SystemExit(main())
