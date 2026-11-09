"""Cross-station diagnostic summaries and conditional coordination hypotheses."""
from dataclasses import dataclass, asdict
from .diagnostic_evidence import validate_mode, finite

@dataclass
class CoordinationResult:
    insights: list
    compact_summary: str
    global_status: str
    mean_nse: object
    outlet_nse: object
    n_stagnant: int
    def to_dict(self):
        return asdict(self)

class LeaderCoordinator:
    def __init__(self, upstream_map, outlet_id=None, diagnostic_mode="historical_policy", static_input_protocol=None, prompt_visibility_protocol=None):
        self.upstream_map = upstream_map
        sinks = set(upstream_map) - {sid for upstreams in upstream_map.values() for sid in upstreams}
        if outlet_id is None:
            if len(sinks) != 1:
                raise ValueError("Configure outlet_id explicitly when topology has no unique outlet")
            outlet_id = next(iter(sinks))
        elif outlet_id not in sinks:
            raise ValueError("Configured outlet_id must be a known terminal station")
        self.outlet_id = outlet_id
        self.diagnostic_mode = validate_mode(diagnostic_mode)
        self.static_input_protocol = static_input_protocol
        from .prompt_visibility import validate_prompt_visibility_protocol
        self.prompt_visibility_protocol = validate_prompt_visibility_protocol(prompt_visibility_protocol)

    def compute_cross_basin_insights(self, worker_summaries):
        if self.diagnostic_mode == 'historical_policy':
            from .historical_policy import policy_coordination
            return policy_coordination(self.upstream_map, self.outlet_id, worker_summaries)

    def format_diagnostics_for_leader(self, diagnostic_reports, coordination, j_history,
                                     nse_history_by_basin=None, active_params=None):
        static_input_protocol = self.static_input_protocol
        if self.diagnostic_mode == 'historical_policy':
            from .historical_policy import policy_leader_block
            return policy_leader_block(diagnostic_reports, coordination, j_history, nse_history_by_basin, active_params, static_input_protocol, self.prompt_visibility_protocol)

    def build_nse_history_by_basin(self, round_history):
        result = {}
        for entry in round_history:
            for wr in entry.get('worker_results', []):
                sid = wr.get('subbasin_id')
                nse = finite(wr.get('metrics', {}).get('NSE'))
                if sid and nse is not None and wr.get('ok') is not False:
                    result.setdefault(sid, []).append(nse)
        return result
