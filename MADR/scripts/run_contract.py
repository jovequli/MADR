"""Configuration validation and model-evaluation accounting."""
import json
from pathlib import Path

def validate_topology(config):
    """Validate configured identities and a connected, single-outlet DAG."""
    workers = config.get('workers')
    if not isinstance(workers, list) or not workers:
        raise ValueError('Configure at least one worker')
    def identifier(value):
        return (isinstance(value, str) and bool(value) and value not in ('.', '..')
                and not any(c.isspace() or c in '/\\' for c in value))
    for key in ('subbasin_id', 'station_code', 'agent_id'):
        values = [w.get(key) for w in workers if isinstance(w, dict)]
        if len(values) != len(workers) or not all(identifier(v) for v in values):
            raise ValueError('Workers require safe nonempty ' + key + ' identifiers')
        if len(values) != len(set(values)):
            raise ValueError('Duplicate worker ' + key)
    stations = {w['subbasin_id'] for w in workers}
    outlet = config.get('outlet_subbasin_id')
    if outlet not in stations:
        raise ValueError('outlet_subbasin_id must identify a configured worker')
    downstream = {}; parents = {sid: set() for sid in stations}
    for worker in workers:
        sid = worker['subbasin_id']; edges = worker.get('neighbors_downstream', [])
        if (not isinstance(edges, list) or not all(isinstance(x, str) for x in edges)
                or len(edges) != len(set(edges))):
            raise ValueError('Downstream neighbors must be a list of unique identities: ' + sid)
        if any(x not in stations or x == sid for x in edges):
            raise ValueError('Unknown or self-referencing downstream neighbor: ' + sid)
        if type(worker.get('is_outlet')) is not bool or worker['is_outlet'] != (sid == outlet):
            raise ValueError('Worker is_outlet must agree with outlet_subbasin_id: ' + sid)
        downstream[sid] = edges
        for target in edges: parents[target].add(sid)
    if downstream[outlet]:
        raise ValueError('The configured outlet must have no downstream neighbor')
    remaining = {sid: len(up) for sid, up in parents.items()}; order = []
    queue = sorted(sid for sid, n in remaining.items() if n == 0)
    while queue:
        sid = queue.pop(0); order.append(sid)
        for target in downstream[sid]:
            remaining[target] -= 1
            if remaining[target] == 0: queue.append(target)
    if len(order) != len(stations):
        raise ValueError('Configured topology contains a cycle')
    reaches_outlet = {outlet}
    for sid in reversed(order):
        if any(target in reaches_outlet for target in downstream[sid]): reaches_outlet.add(sid)
    if reaches_outlet != stations:
        raise ValueError('Every station must drain to the configured outlet')
    return {'station_ids': sorted(stations),
            'station_codes': sorted(w['station_code'] for w in workers),
            'outlet_id': outlet, 'upstream_map': {sid: sorted(up) for sid, up in parents.items()},
            'downstream_station_ids': sorted(sid for sid, up in parents.items() if up),
            'topological_order': order}


def is_test_run(config, mode, dry_run=False):
    return bool(config.get('test_only') or dry_run or mode!='real')

def validate_serial(config):
    if type(config.get('max_parallel_workers')) is not int or config['max_parallel_workers'] != 1:
        raise ValueError('The matched comparison profile requires explicit max_parallel_workers=1')
    if config.get('representative_selection', 'max_mean_nse_complete_round') != 'max_mean_nse_complete_round':
        raise ValueError('The matched representative must maximize complete-round mean NSE')
    if config.get('representative_tie_break', 'earliest_round') != 'earliest_round':
        raise ValueError('The matched representative tie-break is earliest_round')


def center_reference_budget(config, rounds_executed, final_confirmation=True):
    stations={w['subbasin_id'] for w in config['workers']}
    downstream=sorted({sid for w in config['workers'] for sid in w.get('neighbors_downstream',[]) if sid in stations})
    last=rounds_executed-1 if final_confirmation else rounds_executed
    rows=[{'round_id':rid,'subbasin_ids':downstream,'candidate_id':0,
           'reference_slots':len(downstream)} for rid in range(2,last+1)]
    return sum(row['reference_slots'] for row in rows),rows

def expected_candidate_budget(config, rounds_executed, final_confirmation=True, exploration_requests=0):
    """Configured MC slots remain mandatory even if SADR enters escape."""
    station_count=len(config['workers']);round_id=0;rows=[]
    for phase in config['phase_config']['pipeline']:
        for local_round in range(1,phase['max_rounds']+1):
            round_id+=1
            if round_id>rounds_executed:break
            mc=(local_round<=phase.get('mc_rounds',0) and phase.get('mc_candidates',0)>0)
            if round_id==1 or (round_id==rounds_executed and final_confirmation):mc=False
            per_station=phase['mc_candidates'] if mc else 1
            rows.append({'round_id':round_id,'phase':phase['name'],'per_station':per_station,
                         'candidate_evaluations':station_count*per_station})
        if round_id>=rounds_executed:break
    if len(rows)!=rounds_executed:raise ValueError('Executed rounds exceed configured pipeline')
    main=sum(r['candidate_evaluations'] for r in rows)
    references,reference_rows=center_reference_budget(config,rounds_executed,final_confirmation)
    return {'main_loop':main,'center_reference_slots':references,'center_rechecks':references,
            'exploration':exploration_requests, 'total':main+references+exploration_requests,
            'total_evaluations_upper_bound':main+references+exploration_requests,
            'count_semantics':'Planned upper bound: center slots may reuse authenticated prior evaluations without model calls.',
            'rounds':rows,'center_rounds':reference_rows}

def audit_budget(run_root, config, rounds_executed, accounting, final_confirmation=True):
    root=Path(run_root);receipt=root/'exploration_receipt.json'
    exploration_cfg=config.get('initial_exploration',{})
    exploration=0
    if exploration_cfg.get('enabled'):
        if not receipt.is_file():raise ValueError('Enabled exploration is missing its receipt')
        received=json.loads(receipt.read_text())
        mode=exploration_cfg.get('mode','fresh')
        if mode not in ('fresh','replay','reuse') or received.get('mode')!=mode:
            raise ValueError('Exploration receipt mode differs from configured mode')
        if mode in ('fresh','replay'):
            exploration=(exploration_cfg['n_near']+exploration_cfg['n_random'])*len(config['workers'])
        if received.get('evaluation_requests')!=exploration:
            raise ValueError('Exploration receipt differs from configured evaluation budget')
    # Follow actual phase boundaries so optional patience remains auditable.
    phases={p['name']:p for p in config['phase_config']['pipeline']}
    ordered=list(phases);rows=[];previous=None;local_round=0;phase_idx=-1
    for n in range(1,rounds_executed+1):
        meta=json.loads((root/f'round_{n:03d}/round_meta.json').read_text())
        if meta.get('round_id')!=n:raise ValueError('Round metadata ID differs from directory')
        name=meta['phase'];phase=phases[name];new_idx=ordered.index(name)
        if name!=previous:
            if new_idx!=phase_idx+1:raise ValueError('Invalid phase order or repeated phase')
            if previous is not None and phases[previous].get('patience_rounds') is None and local_round!=phases[previous]['max_rounds']:
                raise ValueError('Fixed-budget phase ended before its configured cap')
            phase_idx=new_idx
        local_round=local_round+1 if name==previous else 1;previous=name
        if local_round>phase['max_rounds']:raise ValueError('Phase exceeded its configured cap')
        mc=local_round<=phase.get('mc_rounds',0) and phase.get('mc_candidates',0)>0
        if n==1 or (n==rounds_executed and final_confirmation):mc=False
        count=phase['mc_candidates'] if mc else 1
        rows.append({'round_id':n,'phase':name,'per_station':count,'candidate_evaluations':count*len(config['workers'])})
    main=sum(r['candidate_evaluations'] for r in rows)
    caps=sum(p['max_rounds'] for p in phases.values())
    if rounds_executed==caps and (phase_idx!=len(phases)-1 or local_round!=phases[previous]['max_rounds']):
        raise ValueError('Full-budget run did not finish its configured phase schedule')
    references,reference_rows=center_reference_budget(config,rounds_executed,final_confirmation)
    expected={'main_loop':main,'center_rechecks':references,'exploration':exploration,
              'total':main+references+exploration,'rounds':rows,'center_rounds':reference_rows}
    index=json.loads((root/'native_audit/candidate_index.json').read_text())
    actual_rounds={n:{} for n in range(1,rounds_executed+1)}
    actual_references={};identity_errors=[]
    for encoded,attempt in index.items():
        sid,run_id,candidate_id,phase_tag=json.loads(encoded)
        if phase_tag.startswith('init_explore_'):continue
        if phase_tag.startswith('center_reference_round_'):
            number=int(phase_tag.rsplit('_',1)[1])
            key=(number,sid)
            actual_references[key]=actual_references.get(key,0)+1
            if candidate_id!=0 or run_id!=number*100:
                identity_errors.append({'kind':'invalid_center_reference_identity','key':[sid,run_id,candidate_id,phase_tag]})
            continue
        if not phase_tag.startswith('round_'):continue
        number=int(phase_tag.split('_')[1])
        if candidate_id < 1 or run_id != number*100+candidate_id:
            identity_errors.append({'kind':'invalid_main_candidate_identity','key':[sid,run_id,candidate_id,phase_tag]})
        if number in actual_rounds:
            actual_rounds[number][sid]=actual_rounds[number].get(sid,0)+1
    mismatches=list(identity_errors)
    sids={w['subbasin_id'] for w in config['workers']}
    for row in expected['rounds']:
        actual=actual_rounds[row['round_id']]
        if set(actual)!=sids or any(v!=row['per_station'] for v in actual.values()):
            mismatches.append({'round_id':row['round_id'],'expected_per_station':row['per_station'],'actual':actual})
    reference_slots={(row['round_id'],sid) for row in reference_rows for sid in row['subbasin_ids']}
    reference_mismatches=[];new_references=0;reused_references=0
    identities={tuple(json.loads(encoded)):attempt for encoded,attempt in index.items()}
    for rid,sid in sorted(reference_slots):
        path=root/f'round_{rid:03d}/center_references/{sid}.json'
        try:
            record=json.loads(path.read_text())
            additional=record.get('additional_model_evaluations')
            logical_round=record.get('logical_round_id',record.get('round_id'))
            if type(additional) is not int or additional not in (0,1) or logical_round!=rid:
                raise ValueError('invalid logical round or additional_model_evaluations')
            identity=record['execution_identity']
            key=(sid,identity['run_id'],identity['candidate_id'],identity['phase_tag'])
            if key not in identities:
                raise ValueError('reference source execution identity is absent from native index')
            reuse=record.get('reference_reuse')
            if additional:
                if reuse or key!=(sid,rid*100,0,f'center_reference_round_{rid}'):
                    raise ValueError('new reference has incorrect native identity or reuse flag')
                new_references+=1
            else:
                if (not isinstance(reuse,dict) or reuse.get('protocol')!='same-input-center-reference-v1'
                        or not reuse.get('cache_key') or reuse.get('logical_round_id')!=rid
                        or reuse.get('source_execution_identity')!=identity
                        or reuse.get('source_attempt_id')!=identities[key]):
                    raise ValueError('reused reference lacks matching source receipt')
                source=root/'native_audit/candidates'/identities[key]/'execution_record.json'
                native=json.loads(source.read_text())
                if (native.get('ok') is not True or native.get('metrics')!=record.get('metrics')
                        or native.get('effective_parameters')!=record.get('params')):
                    raise ValueError('reused reference differs from successful source evaluation')
                reused_references+=1
            if actual_references.get((rid,sid),0)!=additional:
                raise ValueError('native reference calls disagree with logical reference record')
        except (OSError,KeyError,ValueError,TypeError) as error:
            reference_mismatches.append({'round_id':rid,'subbasin_id':sid,'error':str(error)})
    for rid,sid in sorted(set(actual_references)-reference_slots):
        reference_mismatches.append({'round_id':rid,'subbasin_id':sid,'error':'unexpected native center reference'})
    expected.update(center_reference_slots=references, center_rechecks=new_references,
                    center_reference_reuses=reused_references,
                    total=main+new_references+exploration,
                    total_evaluations_upper_bound=main+references+exploration)
    total=accounting.get('candidate_evaluations')
    return {'expected':expected,'actual_candidate_evaluations':total,'round_mismatches':mismatches,
            'center_reference_mismatches':reference_mismatches,
            'actual_center_rechecks':sum(actual_references.values()),
            'center_reference_reuses':reused_references,'center_reference_slots':references,
            'actual_vic_launches':accounting.get('actual_vic_launches'),
            'actual_routing_launches':accounting.get('actual_routing_launches',accounting.get('routing_launches')),
            'fixed_flux_preparations':accounting.get('cache_preparation_attempts'),
            'aligned':total==expected['total'] and not mismatches and not reference_mismatches,
            'count_semantics':'Executor evaluation attempts and actual process starts are separate; reused center references launch no model and are excluded from candidate_evaluations. Fixed-flux preparation is separate.'}
