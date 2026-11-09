"""Validated, opt-in phase stopping with fixed-round defaults."""
from .execution_protocol import global_score, is_finite

def validate_pipeline(pipeline):
    if not pipeline:
        raise ValueError("Phase pipeline cannot be empty")
    names=[p['name'] for p in pipeline]
    allowed=['VIC_ONLY','ROUTING_ONLY','JOINT']
    if len(set(names)) != len(names) or any(n not in allowed for n in names):
        raise ValueError("Unknown or repeated phase")
    if names != sorted(names, key=allowed.index):
        raise ValueError("Phase order must follow VIC_ONLY, ROUTING_ONLY, JOINT")
    for p in pipeline:
        if p['max_rounds'] < 1 or p['mc_rounds'] < 0 or p['mc_rounds'] > p['max_rounds']:
            raise ValueError("Invalid phase or MC round count")
        if p['mc_rounds'] and not 1 <= p['mc_candidates'] <= 3:
            raise ValueError("MC rounds require one to three candidates")
        patience=p.get('patience_rounds')
        if patience is not None and (type(patience) is not int or patience < 1):
            raise ValueError("patience_rounds must be null or a positive integer")
        delta=p.get('patience_min_delta', 0.0)
        if not is_finite(delta) or float(delta) < 0:
            raise ValueError("patience_min_delta must be finite and nonnegative")
    return pipeline

def phase_progress(state):
    """Mean NSE plateau, with MC and failed/incomplete rounds excluded."""
    best=state.get('best_mean_nse_in_phase')
    stagnant=state.get('rounds_without_improvement_in_phase',0)
    p=state['phase_pipeline'][state['current_phase_idx']]
    rows=state.get('round_worker_results',[])
    values=global_score(rows,state['worker_cfgs'],state.get('objective_weights',{}))
    valid=all(is_finite(x) for x in values)
    if valid:
        mean=float(values[2])
        if best is None or mean > best + float(p.get('patience_min_delta',0.0)):
            best,stagnant=mean,0
        elif state['rounds_in_current_phase'] > p.get('mc_rounds',0):
            stagnant+=1
    return {'best_mean_nse_in_phase':best,
            'rounds_without_improvement_in_phase':stagnant}, valid
