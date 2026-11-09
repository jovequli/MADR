"""Exact-input exploration reuse validation, without physical or API calls."""
from __future__ import annotations
import copy,sys
from pathlib import Path
from linux_common import read_json,write_new,sha256,RUN_ROUNDS
def expected_information(config):
    settings = config.get('initial_exploration', {})
    if settings.get('enabled') is not True:
        raise ValueError('Shared exploration requires enabled exploration in every run')
    counts = [settings.get('n_near', 10), settings.get('n_random', 40)]
    if any(type(v) is not int or v < 0 for v in counts) or sum(counts) < 1:
        raise ValueError('Invalid shared exploration sample count')
    return len(config['workers']) * sum(counts)


def validate_reuse_receipt(receipt, expected):
    """Reject a nominal cache reuse that evaluated any physical candidate."""
    for key in ('evaluation_requests', 'physical_execution_requests', 'actual_vic_calls',
                'actual_routing_calls'):
        if type(receipt.get(key)) is not int or receipt[key] != 0:
            raise ValueError('Shared exploration validation unexpectedly executed models: ' + key)
    for key in ('inherited_evaluations', 'cache_hits', 'information_budget_evaluations'):
        if type(receipt.get(key)) is not int or receipt[key] != expected:
            raise ValueError('Shared exploration information accounting differs: ' + key)
    if receipt.get('mode') != 'reuse':
        raise ValueError('Shared validation must use mode=reuse')


def _worker(config_path, output_path, source=None, fresh=False):
    """Executed in one framework subprocess; never enter a calibration round."""
    if fresh:
        raise ValueError('This entry only validates reuse; fresh exploration runs through the normal framework graph')
    config = copy.deepcopy(read_json(config_path))
    output = Path(output_path).resolve()
    if output.exists():
        raise ValueError('Exploration validation/generation output already exists: ' + str(output))
    config['output_root'] = str(output)
    settings = config['initial_exploration']
    settings.pop('seed_from_run', None)
    if fresh:
        settings['mode'] = 'fresh'
        settings.pop('source', None)
        settings.pop('replay_parameter_set_sha256', None)
    else:
        if not source:
            raise ValueError('Reuse validation requires a source bundle')
        settings.update(mode='reuse', source=str(Path(source).resolve(strict=True)))

    # Import from the framework cwd selected by the parent. Even fresh exploration
    # is a pure numerical stage: dummy clients are installed as a second safeguard.
    sys.path.insert(0, str(Path.cwd()))
    from scripts import graph_nodes
    from scripts.execution_protocol import validate_periods
    from scripts.run_provenance import seed_local_randomness
    validate_periods(config)
    seed_local_randomness(config)
    state = {'team_config': config, 'team_config_path': str(config_path),
             'output_root': str(output), 'mode': config.get('model_binding', {}).get('mode', 'real'),
             'dry_run': True, 'max_rounds': config.get('max_rounds', RUN_ROUNDS)}
    state.update(graph_nodes.init_calibration(state))
    state.update(graph_nodes.run_exploration(state))
    receipt = read_json(output / 'exploration_receipt.json')
    if not fresh:
        validate_reuse_receipt(receipt, expected_information(config))
    calls = output / 'llm_calls.jsonl'
    if calls.exists() and calls.stat().st_size:
        raise ValueError('Numerical exploration unexpectedly contains an LLM call')
    if any(output.glob('round_*')):
        raise ValueError('Exploration-only worker unexpectedly entered calibration rounds')
    write_new(output / 'shared_worker_receipt.json', {
        'kind': 'numerical_exploration_only', 'fresh': fresh,
        'llm_calls': 0, 'calibration_rounds': 0,
        'dummy_llm_clients': True, 'physical_execution_mode': state['mode'],
        'exploration_receipt_sha256': sha256(output / 'exploration_receipt.json')})

