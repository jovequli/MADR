"""Opt-in ablation deployment over the unchanged MADR deployment tools.

Use --action validate before prepare/run. Each launch selects exactly one arm;
the standard launcher tools/run_madr.py neither imports this file nor installs any intervention.
"""
from __future__ import annotations

import copy
from pathlib import Path
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = REPOSITORY / 'MADR'
sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))

from ablations import runtime
from ablations.arm_registry import (NO_EXPLORATION, FRESH_EXPLORATION_ONLY,
                                      SENSITIVITY_IMPLEMENTATIONS, arm_label)
from ablations.search_interventions import candidate_budget
import run_madr as deployment


def configure_deployment():
    """Adapt this process only; preserve shared preparation, transport and recovery."""
    if getattr(deployment, '_ablation_entry_installed', False):
        return
    deployment.LAUNCH_KEYS = deployment.LAUNCH_KEYS | {'ablation'}
    original_resolve = deployment.resolve_launch
    original_load = deployment.load_spec
    original_sources = deployment.source_files
    original_completion = deployment.validate_completion
    original_summarize = deployment.summarize

    def resolve(source):
        launch, settings = original_resolve(source)
        selected = runtime.protocol(launch.get('ablation'))
        if selected['arm'] == 'C1_no_exploration' and launch.get('exploration') != {'mode': 'fresh'}:
            raise ValueError('No-exploration requires exploration={"mode":"fresh"}; no reuse bundle is opened')
        if selected['arm'] == runtime.SEQUENTIAL and launch.get('exploration') != {'mode': 'fresh'}:
            raise ValueError('Strict sequential calibration requires fresh exploration under each frozen upstream background; no reuse bundle is opened')
        if selected['arm'] in FRESH_EXPLORATION_ONLY and launch.get('exploration') != {'mode': 'fresh'}:
            raise ValueError('This exploration intervention requires fresh mode without a reuse source: ' + arm_label(selected['arm'], selected['version']))
        return launch, settings

    def load(source):
        spec = original_load(source)  # Validate the untouched reference profile first.
        cfg = runtime.configure(spec['config'], spec['launch']['ablation'])
        spec['config'] = cfg
        if cfg['ablation_protocol']['arm'] in NO_EXPLORATION:
            spec['launch']['exploration'] = {'mode': 'disabled'}
        if cfg['ablation_protocol']['arm'] == runtime.SEQUENTIAL:
            from ablations.sequential_intervention import budget as sequential_budget
            budget = sequential_budget(cfg)
            count = len(cfg['workers'])
            budget['logical_llm_calls'] = {'leader': len(budget['rounds']),
                                           'worker': budget['main_loop'] - 2 * count}
            budget['routing_fixed_flux_preparations'] = count
            budget['exploration_information_evaluations'] = budget['exploration']
            budget['mc_round_ids'] = [row['round_id'] for row in budget['rounds'] if row['per_station'] > 1]
            budget['coverage_note'] = 'Each station completes the reference local schedule before the next station starts; round IDs here index station-local steps in execution order.'
            budget['topology'] = spec['budget']['topology']
            budget['ablation_protocol'] = cfg['ablation_protocol']
            budget['arm_label'] = cfg['ablation_arm_label']
            spec['budget'] = budget
            return spec
        budget = candidate_budget(cfg)
        rounds = len(budget['rounds'])
        count = len(cfg['workers'])
        budget['logical_llm_calls'] = {
            'leader': 0 if cfg['ablation_protocol']['arm'] == 'A1_no_leader' else rounds,
            'worker': budget['main_loop'] - 2 * count if cfg['method'] == 'MADR' else 0,
        }
        budget['routing_fixed_flux_preparations'] = count if any(
            row['phase'] == 'ROUTING_ONLY' for row in budget['rounds']) else 0
        explore = cfg['initial_exploration']
        budget['exploration_information_evaluations'] = count * (
            explore['n_near'] + explore['n_random']) if explore['enabled'] else 0
        budget['topology'] = spec['budget']['topology']
        budget['mc_round_ids'] = [r['round_id'] for r in budget['rounds'] if r['per_station'] > 1]
        budget['ablation_protocol'] = cfg['ablation_protocol']
        budget['arm_label'] = cfg['ablation_arm_label']
        if cfg['ablation_protocol']['arm'] in SENSITIVITY_IMPLEMENTATIONS:
            budget['coverage_note'] = ('Sensitivity schedule; candidate evaluation budgets and exploration quantities '
                                       'are explicitly set for this sensitivity configuration.')
            spec['budget'] = budget
            return spec
        budget['coverage_note'] = (
            'Original main rounds, phases and MC slots retained; exploration omitted without replacement evaluations.'
            if cfg['ablation_protocol']['arm'] == 'C1_no_exploration' else
            'Reference schedule transformed explicitly; actual rounds may differ at matched evaluation budget.')
        spec['budget'] = budget
        return spec

    def sources(method='MADR'):
        result = original_sources(method)
        for path in sorted((REPOSITORY / 'ablations').rglob('*')):
            if (path.is_file() and not path.is_symlink() and '__pycache__' not in path.parts
                    and path.suffix in ('.py', '.md', '.json') and not path.name.startswith('.')):
                result[str(path.relative_to(REPOSITORY))] = deployment.sha256(path)
        return result

    def completion(work, manifest, exit_code):
        from ablations import sequential_deployment
        if sequential_deployment.selected(manifest):
            return sequential_deployment.validate_completion(work, manifest, exit_code)
        return original_completion(work, manifest, exit_code)

    def summarize(source):
        from ablations import sequential_deployment
        spec = load(source)
        if spec['config']['ablation_protocol']['arm'] == runtime.SEQUENTIAL:
            manifest = deployment.verify_prepared(spec)
            return sequential_deployment.summarize(spec, manifest, deployment)
        return original_summarize(source)

    class ProcessCommands:
        """Redirect only the framework child to its frozen opt-in bootstrap."""
        def __init__(self, module):
            self.module = module

        def __getattr__(self, name):
            return getattr(self.module, name)

        def Popen(self, command, *args, **kwargs):
            command = list(command)
            if command[1:4] == ['-B', '-m', 'scripts.run_langgraph']:
                snapshot = Path(kwargs['cwd'])
                command = [command[0], '-B', str(snapshot / 'ablations/execute.py'), *command[4:]]
            return self.module.Popen(command, *args, **kwargs)

    class FileCommands:
        """Copy the opt-in ablation sources from the repository-level ablations directory."""
        def __init__(self, module):
            self.module = module

        def __getattr__(self, name):
            return getattr(self.module, name)

        def copyfile(self, source, target, *args, **kwargs):
            source = Path(source)
            if source.is_relative_to(ROOT / 'ablations'):
                source = REPOSITORY / source.relative_to(ROOT)
            return self.module.copyfile(source, target, *args, **kwargs)

    # Token accounting reads the frozen protocol; physical usage remains measured.
    import summarize_token_usage as usage
    original_budget = usage.evaluation_budget

    def evaluation_budget(schedule, method='MADR', config=None):
        result = original_budget(schedule, method=method, config=config)
        selected = (config or {}).get('ablation_protocol')
        if selected:
            result['ablation_protocol'] = copy.deepcopy(selected)
            if selected['arm'] == 'A1_no_leader':
                result['leader_logical_calls'] = 0
                for row in result['rounds']:
                    row['expected_leader_logical_calls'] = 0
            elif selected['arm'] == runtime.SEQUENTIAL and 'sequential_station_context' not in (config or {}):
                final_local = config['max_rounds']
                for row in result['rounds']:
                    local = row['local_round_id']
                    main = row['candidate_evaluations']
                    row.update(center_reference_slots=0, evaluation_attempts_upper_bound=main,
                               expected_worker_logical_calls=main if 1 < local < final_local else 0,
                               physical_only_main_evaluations=main if local in (1, final_local) else 0)
                result.update(center_reference_slots=0,
                              total_evaluation_attempts_upper_bound=result['main_candidate_evaluations'],
                              worker_logical_calls=sum(row['expected_worker_logical_calls'] for row in result['rounds']),
                              scope='Station-local main evaluations summed in execution order; exploration and fixed-flux preparation excluded.')
        return result

    deployment.resolve_launch = resolve
    deployment.load_spec = load
    deployment.source_files = sources
    deployment.validate_completion = completion
    deployment.summarize = summarize
    deployment.subprocess = ProcessCommands(deployment.subprocess)
    deployment.shutil = FileCommands(deployment.shutil)
    usage.evaluation_budget = evaluation_budget
    deployment._ablation_entry_installed = True


def main():
    configure_deployment()
    return deployment.main()


if __name__ == '__main__':
    raise SystemExit(main())
