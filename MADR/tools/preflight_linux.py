"""Pinned runtime and native executable inspection only; does not execute models."""
from __future__ import annotations
import os,sys,platform,subprocess,importlib,importlib.metadata
from pathlib import Path
from linux_common import ROOT,sha256
def dependencies(provider=None):
    if sys.version_info[:2] != (3, 12):
        raise ValueError('Use the dedicated Python 3.12 Linux environment for this pinned package')
    versions = {}
    for raw in ((ROOT / 'tools/requirements-linux.txt').read_text() + '\n' +
                (ROOT / 'tools/requirements-api.txt').read_text()).splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line: continue
        name, expected = line.split('==')
        actual = importlib.metadata.version(name)
        if actual != expected:
            raise ValueError(f'Dependency {name}: expected {expected}, found {actual}')
        versions[name] = actual
    for module in ('numpy', 'sqlite3', 'langgraph.graph', 'langgraph.checkpoint.sqlite'):
        importlib.import_module(module)
    for sdk,distribution in (('openai','openai'),('anthropic','anthropic'),('google.genai','google-genai')):
        importlib.import_module(sdk)
        versions[distribution] = importlib.metadata.version(distribution)
    return {'python': platform.python_version(), 'executable': sys.executable, 'versions': versions}


def inspect_native(path):
    p = Path(path)
    if not os.access(p, os.X_OK): raise ValueError('Native file is not executable: ' + str(p))
    with p.open('rb') as f: header = f.read(20)
    if len(header) < 20 or header[:4] != b'\x7fELF':
        raise ValueError('Expected a Linux ELF, not a macOS executable: ' + str(p))
    machine = int.from_bytes(header[18:20], 'little' if header[5] == 1 else 'big')
    expected = {'x86_64': 62, 'amd64': 62, 'aarch64': 183, 'arm64': 183}.get(platform.machine().lower())
    if expected is None or machine != expected: raise ValueError('Native ELF architecture mismatch')
    result = subprocess.run(['ldd', str(p)], capture_output=True, text=True, timeout=20)
    message = result.stdout + result.stderr
    static = 'statically linked' in message or 'not a dynamic executable' in message
    if 'not found' in message or (result.returncode and not static):
        raise ValueError('Native shared-library check failed; inspect ldd for ' + str(p))
    return {'path': str(p), 'sha256': sha256(p), 'elf_machine': machine, 'ldd_passed': True}

