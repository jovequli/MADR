"""Run reproducibility records that never read API secrets."""
from pathlib import Path
import datetime as dt
import hashlib
import importlib.metadata
import json
import platform
import random
import shutil


def _scrub(value):
    if isinstance(value, dict):
        return {k: ('<redacted>' if k.lower() in {'api_key', 'token', 'password', 'secret', 'authorization'} else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def save_run_snapshot(root, output, config, cli_args):
    root, output = Path(root), Path(output)
    files = {}
    for folder in ('scripts', 'prompts', 'schemas'):
        for p in sorted((root / folder).rglob('*')):
            if p.is_file() and '__pycache__' not in p.parts and not p.name.startswith('._') and p.suffix != '.pyc':
                files[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    from .prompt_source import PromptSource, validate_static_source_equivalence
    source = PromptSource(config.get('static_input_protocol'))
    static_sources = validate_static_source_equivalence()
    static_sources['selected_protocol'] = dict(source.protocol)
    static_sources['role_system_sha256'] = {'Leader': hashlib.sha256(source.leader_system().encode()).hexdigest()}
    if config.get('method', 'MADR') == 'MADR':
        static_sources['role_system_sha256']['Worker'] = hashlib.sha256(source.worker_system().encode()).hexdigest()
    static_sources['scope'] = 'Startup: full bundled section/file inventory and selected role system bytes; not a claim that every section is used by every request.'
    safe_config = _scrub(config)
    config_hash = hashlib.sha256(json.dumps(safe_config, sort_keys=True).encode()).hexdigest()
    directory = output / 'provenance'
    manifest_path = directory / 'manifest.json'
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if not cli_args.get('resume'):
            raise ValueError('Output already contains a run; use a new output directory')
        if previous['files'] != files or previous['config_sha256'] != config_hash:
            raise ValueError('Resume refused: source/prompts/config differ from the original run')
        return
    if cli_args.get('resume'):
        raise ValueError('Resume requires a matching provenance snapshot')
    directory.mkdir(parents=True, exist_ok=False)
    for rel in files:
        dest = directory / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / rel, dest)
    versions = {}
    for name in ('numpy', 'langgraph', 'langgraph-checkpoint', 'langgraph-checkpoint-sqlite', 'anthropic', 'openai', 'google-genai'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    (directory / 'config.json').write_text(json.dumps(safe_config, ensure_ascii=False, indent=2))
    manifest = {'created_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'files': files,
                'config_sha256': config_hash, 'protocol_id': config.get('protocol_id'),
                'static_sources': static_sources,
                'cli': _scrub(cli_args), 'python': platform.python_version(), 'packages': versions,
                'seed': config.get('random_seed'), 'llm_seed_note': 'Python/NumPy seed does not fix remote LLM sampling or provider updates',
                'resume_note': 'Resume checks code/config identity; exact random-stream replay is not guaranteed'}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def seed_local_randomness(config):
    import numpy as np
    seed = config.get('random_seed')
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError('Explicit integer random_seed in [0, 2**32) is required')
    random.seed(seed)
    np.random.seed(seed)


class AuditedClient:
    """Outer parsed-response record, with shared IDs for internal API attempts.

    CLI transports receive one adapter invocation boundary; that does not imply
    one remote request and does not count version/help probes as model calls.
    """
    def __init__(self, client, path, agent_id, enabled=None, source_owner=None):
        self._client, self._path, self._agent_id = client, Path(path), agent_id
        options = getattr(getattr(client, 'config', None), 'extra_params', {}) or {}
        self.enabled = options.get('process_trace_enabled', True) if enabled is None else enabled
        self.trace_metadata = {}
        self._source_owner = source_owner
        self.last_call_id = None
        self.last_attempt_ids = []

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _append(self, record):
        from .process_trace import append_event
        return append_event(self._path, record, enabled=self.enabled)

    def chat(self, messages=None, system='', **kwargs):
        from .process_trace import llm_trace_context, new_id, emit_attempt, response_evidence
        call_id = new_id()
        self.last_call_id = call_id
        self.last_attempt_ids = []
        config = getattr(self._client, 'config', None)
        requested = getattr(config, 'model', None)
        temp = kwargs.get('temperature', getattr(config, 'temperature', None))
        static_metadata = None
        if self._source_owner is not None:
            source = self._source_owner.prompt_source
            static_metadata = source.provenance_metadata()
            static_metadata['agent_static_prompt_metadata'] = dict(getattr(self._source_owner, 'static_prompt_metadata', {}))
            static_metadata['actual_system_sha256'] = hashlib.sha256(system.encode('utf-8')).hexdigest()
            static_metadata['actual_messages_sha256'] = hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            static_metadata['request_scope'] = 'Actual system/messages hashes plus agent-instance loaded sections and protocol-specific process cumulative section reads; cumulative reads are not role-exclusive or per-request attribution.'
        self._append({'event': 'request', 'call_id': call_id, 'agent_id': self._agent_id,
                      'utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                      'model_requested': requested, 'temperature': temp,
                      'system': system, 'messages': messages, 'options': _scrub(kwargs),
                      'static_sources': static_metadata,
                      'context': self.trace_metadata})
        adapter_boundary = not getattr(self._client, '_emits_attempt_events', False)
        with llm_trace_context(self._path, call_id, self._agent_id, self.enabled,
                               self.trace_metadata) as context:
            if adapter_boundary:
                attempt_id = new_id()
                self.last_attempt_ids = [attempt_id]
                emit_attempt(context, 'attempt_request', attempt_id=attempt_id,
                             outer_attempt=1, transport_attempt=1,
                             record_scope='client adapter invocation; internal CLI/remote requests not enumerated',
                             model_requested=requested,
                             request={'system': system, 'messages': messages})
            try:
                response = self._client.chat(messages=messages, system=system, **kwargs)
            except Exception as error:
                if adapter_boundary:
                    emit_attempt(context, 'attempt_error', attempt_id=attempt_id,
                                 outer_attempt=1, transport_attempt=1,
                                 stage='client_adapter_invocation', error_type=type(error).__name__)
                else:
                    self.last_attempt_ids = list(getattr(self._client, 'last_attempt_ids', []))
                self._append({'event': 'exception', 'call_id': call_id, 'type': type(error).__name__,
                              'attempt_ids': self.last_attempt_ids,
                              'failure_class': getattr(error, 'failure_class', None),
                              'usage_summary': getattr(self._client, 'last_usage_summary', None)})
                raise
            evidence = response_evidence(response)
            if adapter_boundary:
                emit_attempt(context, 'attempt_response', attempt_id=attempt_id,
                             outer_attempt=1, transport_attempt=1,
                             **{**evidence, 'record_scope': 'client adapter returned; raw CLI wrapper is saved by CLI transport'},
                             recorded_before_parse=False)
            else:
                self.last_attempt_ids = list(getattr(self._client, 'last_attempt_ids', []))
            self._append({'event': 'response', 'call_id': call_id,
                          'model_requested': requested, 'model_reported': evidence['model_reported'],
                          'provider': getattr(response, 'provider', None),
                          'content': getattr(response, 'content', None),
                          'parsed': getattr(response, 'parsed', None),
                          'prompt_tokens': getattr(response, 'prompt_tokens', None),
                          'completion_tokens': getattr(response, 'completion_tokens', None),
                          'usage_provider_reported': evidence['usage_provider_reported'],
                          'transport_call_id': evidence['transport_call_id'],
                          'attempt_ids': self.last_attempt_ids,
                          'call_usage_summary': getattr(self._client, 'last_usage_summary', None)})
        return response
