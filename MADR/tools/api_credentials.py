"""Load only the three approved API secrets without shell evaluation or persistence."""
from pathlib import Path
import os
import re
import shlex

KEY_NAMES=('DEEPSEEK_API_KEY','ANTHROPIC_API_KEY','GEMINI_API_KEY')

def credential_environment(key_file=None, *, forbidden_roots=(), environment=None):
    env=dict(os.environ if environment is None else environment)
    if key_file is None:
        return env
    path=Path(key_file).expanduser().resolve(strict=True)
    if not path.is_file() or path.stat().st_size>1024*1024:
        raise ValueError('API key file must be a regular text file under 1 MiB')
    for root in forbidden_roots:
        if path.is_relative_to(Path(root).resolve()):
            raise ValueError('API key file must stay outside code and work directories')
    try:
        lines=path.read_text(encoding='utf-8-sig').splitlines()
    except (OSError,UnicodeError):
        raise ValueError('Cannot read API key file as text') from None
    seen=set()
    for number,line in enumerate(lines,1):
        line=line.strip()
        if not line or line.startswith('#'):continue
        if line.startswith('export '):line=line[7:].lstrip()
        match=re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)',line)
        if not match or match.group(1) not in KEY_NAMES:
            raise ValueError(f'API key file line {number}: only the three approved key assignments are accepted')
        name,raw=match.groups()
        if name in seen:raise ValueError(f'API key file line {number}: duplicate variable')
        try:values=shlex.split(raw,comments=True,posix=True)
        except ValueError:raise ValueError(f'API key file line {number}: invalid quoting') from None
        if len(values)!=1 or not values[0] or any(c.isspace() for c in values[0]):
            raise ValueError(f'API key file line {number}: require one nonempty literal value')
        # Keys are literals, never interpolated, expanded, sourced, or executed.
        env[name]=values[0];seen.add(name)
    return env
