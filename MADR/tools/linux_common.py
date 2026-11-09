"""Provider profiles, input identities and isolated network policies."""
from __future__ import annotations
import json, hashlib, sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import proxy_bypass_environment
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
API_PROFILES = {'ds_default': {'provider': 'deepseek', 'model': 'deepseek-flash', 'api_key_env': 'DEEPSEEK_API_KEY', 'base_url': 'https://api.deepseek.com', 'thinking': 'provider_default', 'extra_params': {'retry_max_tokens': 32768, 'temperature_policy': 'omit'}}, 'sonnet_default': {'provider': 'anthropic', 'model': 'claude-sonnet-5-5', 'api_key_env': 'ANTHROPIC_API_KEY', 'base_url': 'https://api.anthropic.com', 'thinking': 'provider_default', 'extra_params': {'retry_max_tokens': 32768, 'temperature_policy': 'omit'}}, 'sonnet_cached': {'provider': 'anthropic', 'model': 'claude-sonnet-5-5', 'api_key_env': 'ANTHROPIC_API_KEY', 'base_url': 'https://api.anthropic.com', 'thinking': 'provider_default', 'extra_params': {'prompt_cache_system': True, 'retry_max_tokens': 32768, 'temperature_policy': 'omit'}}, 'gemini_default': {'provider': 'gemini', 'model': 'gemini-3.8-flash', 'api_key_env': 'GEMINI_API_KEY', 'base_url': 'https://generativelanguage.googleapis.com', 'thinking': 'provider_default', 'extra_params': {'retry_max_tokens': 32768, 'temperature_policy': 'omit'}}}
VARIANTS=("E_refined",)
PHYSICAL_KEYS=("soil_base","global_base","rout_template_dir","obs_file","vic_exe","rout_exe")
PYTHON_HASH_SEED="0"
RUN_ROUNDS=90
def expected_provider_profile(profile_id):
    import copy
    # A bare arm means its default template profile only; prepared rows always
    # carry an explicit profile ID so provider identity is never inferred.
    if profile_id in VARIANTS or profile_id == 'deepseek': profile_id='ds_default'
    if profile_id not in API_PROFILES: raise ValueError('Unknown native API profile')
    profile=copy.deepcopy(API_PROFILES[profile_id])
    profile.update(timeout_sec=600.0,max_attempts=3,retry_delay_sec=2.0,max_tokens=16384,
                   temperature_policy='omit')
    return profile


def configure_provider(config, profile_id):
    import copy
    if profile_id in VARIANTS or profile_id == 'deepseek': profile_id='ds_default'
    profile=expected_provider_profile(profile_id)
    config['api_profile_id']=profile_id
    for agent in [config['leader'],*config['workers']]:
        for field in ('provider','model','api_key_env','base_url','timeout_sec','retry_delay_sec','max_tokens','extra_params'):
            agent[field]=copy.deepcopy(profile[field])
        agent['max_retries']=profile['max_attempts']
        agent.pop('api_key',None)
    return config


def provider_network_policy(provider):
    if provider not in ('deepseek', 'anthropic', 'gemini'):
        raise ValueError('Unknown provider for frozen network policy')
    direct = provider == 'deepseek'
    return {'mode':'direct' if direct else 'inherit', 'child_proxy_bypass':direct}


def proxy_environment_status(policy, environment):
    """Inspect proxy routing without exposing URLs, credentials, or host values."""
    if policy not in [provider_network_policy(provider) for provider in ('deepseek','anthropic','gemini')]:
        raise ValueError('Missing or inconsistent frozen provider network policy')
    present = any(bool(value.strip()) for key, value in environment.items()
                  if key.lower() in ('http_proxy','https_proxy','all_proxy'))
    # Match urllib/httpx environment precedence, including lowercase empty
    # variables removing uppercase values. Do not mutate the process environment.
    proxies = {}
    for key, value in environment.items():
        if value and key.lower().endswith('_proxy'):
            proxies[key.lower()[:-6]] = value
    for key, value in environment.items():
        if key.endswith('_proxy'):
            if value: proxies[key[:-6]] = value
            else: proxies.pop(key[:-6], None)
    proxy = proxies.get('https') or proxies.get('all') or ''
    try:
        parsed = urlsplit(proxy)
        valid_proxy = (parsed.scheme in ('http','https') and bool(parsed.hostname)
                       and not any(char.isspace() for char in proxy))
        if parsed.port is not None: valid_proxy = valid_proxy and 1 <= parsed.port <= 65535
    except (ValueError, TypeError):
        valid_proxy = False
    no_proxy = proxies.get('no', '')
    bypass_all = '*' in [part.strip() for part in no_proxy.split(',')]
    endpoint_bypass = any(proxy_bypass_environment(host, {'no':no_proxy})
                          for host in ('api.anthropic.com','generativelanguage.googleapis.com'))
    configured = valid_proxy and not endpoint_bypass and not bypass_all
    return {**policy, 'proxy_environment_present_at_launch':present,
            'https_proxy_configured':configured, 'global_proxy_bypass_present':bypass_all,
            'provider_endpoint_bypass_present':endpoint_bypass,
            'proxy_values_recorded':False, 'parent_environment_modified':False}


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    def no_duplicates(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('Duplicate JSON key: ' + key)
            out[key] = value
        return out
    return json.loads(Path(path).read_text(), object_pairs_hook=no_duplicates)


def write_new(path, value):
    path = Path(path)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def overlaps(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)

