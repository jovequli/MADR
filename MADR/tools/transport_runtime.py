"""Per-child direct networking and interrupt cleanup."""
from __future__ import annotations
import os,signal,time
from linux_common import proxy_environment_status,PYTHON_HASH_SEED
def stop_group(process):
    try: os.killpg(process.pid,signal.SIGTERM)
    except ProcessLookupError: return
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        process.poll()
        try: os.killpg(process.pid,0)
        except ProcessLookupError: return
        time.sleep(.1)
    try: os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait()


def child_environment(policy, environment=None):
    """Apply one run's frozen routing; keep URLs and credentials in memory only."""
    env = dict(os.environ if environment is None else environment)
    status = proxy_environment_status(policy, env)
    if policy['mode'] == 'direct':
        for key in list(env):
            if key.lower() in ('http_proxy','https_proxy','all_proxy','no_proxy'):
                env.pop(key, None)
        env.update(NO_PROXY='*', no_proxy='*')
    env.update(PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1', PYTHONHASHSEED=PYTHON_HASH_SEED)
    return env, {**status, 'python_hash_seed':PYTHON_HASH_SEED}

