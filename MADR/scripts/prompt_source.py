"""Static prompts and expert knowledge from one Markdown source.

Content fingerprints are recorded per run. Runtime snapshots protect an existing
run without binding a new basin or knowledge file to a previous release's hashes.
"""
from __future__ import annotations
import hashlib
import json
import re
from threading import RLock
from pathlib import Path

PROTOCOL = "minimal_static_sources_v1"
_CONSUMED = {}
_CONSUMED_LOCK = RLock()
CORE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_PATH = CORE_ROOT / "prompts/vic_routing/static_bundle.md"
_SECTION = re.compile(r"<!-- BEGIN STATIC: ([a-z][a-z0-9_.]*) -->\n(.*?)\n<!-- END STATIC: \1 -->", re.S)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _content_sha(sections):
    return _sha(json.dumps(sections, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8"))


def parse_bundle(text):
    sections = {}
    for match in _SECTION.finditer(text):
        name, body = match.groups()
        if name in sections:
            raise ValueError(f"Duplicate static section: {name}")
        if not body.strip():
            raise ValueError(f"Empty static section: {name}")
        sections[name] = body
    if not sections or text.count("<!-- BEGIN STATIC:") != len(sections) or text.count("<!-- END STATIC:") != len(sections):
        raise ValueError("Malformed static bundle markers")
    return sections


def _normalize(protocol):
    from .execution_protocol import validate_static_input_protocol
    return validate_static_input_protocol(protocol)


def _validated_source():
    """Read the configured knowledge source and compute its current identity."""
    data = BUNDLE_PATH.read_bytes()
    sections = parse_bundle(data.decode("utf-8"))
    manifest = {"protocol": PROTOCOL, "bundle_sha256": _sha(data),
                "content_sha256": _content_sha(sections),
                "sections": {name: _sha(body.encode("utf-8")) for name, body in sections.items()}}
    return sections, manifest


def validate_static_source_equivalence():
    """Return the validated single-source inventory for runtime provenance."""
    sections, manifest = _validated_source()
    return {**manifest, "source_mode": "single_markdown", "validated": True,
            "section_count": len(sections),
            "consumed_files": ["prompts/vic_routing/static_bundle.md"]}


class PromptSource:
    def __init__(self, protocol=None):
        self.protocol = _normalize(protocol)
        self.sections, self.manifest = _validated_source()
        self._used = set()

    def get_text(self, section):
        if section not in self.sections:
            raise ValueError(f"Required static section is missing: {section}")
        self._used.add(section)
        key = tuple(self.protocol[name] for name in ("protocol", "storage", "worker_content"))
        with _CONSUMED_LOCK:
            _CONSUMED.setdefault(key, set()).add(section)
        return self.sections[section]

    def render_static(self, section, **values):
        return self.get_text(section).format(**values)

    def worker_system(self):
        return self.get_text("worker_system_minimal")

    def leader_system(self):
        return "\n\n".join((self.get_text("leader_system_minimal"), self.get_text("leader_knowledge_minimal")))

    def provenance_metadata(self, *, system=None, user_template=None):
        key = tuple(self.protocol[name] for name in ("protocol", "storage", "worker_content"))
        with _CONSUMED_LOCK:
            cumulative = sorted(_CONSUMED.get(key, set()))
        metadata = {**self.protocol, "content_sha256": self.manifest["content_sha256"],
                "bundle_sha256": self.manifest["bundle_sha256"],
                "section_sha256": {name: self.manifest["sections"][name] for name in sorted(self._used)},
                "cumulative_consumed_section_sha256": {name: self.manifest["sections"][name] for name in cumulative},
                "consumption_scope": "section_sha256: this PromptSource instance; cumulative: this method module/process and exact protocol tuple, not a role-exclusive or per-request inventory",
                "consumed_files": ["prompts/vic_routing/static_bundle.md"]}
        if system is not None:
            metadata["system_sha256"] = _sha(system.encode("utf-8"))
        if user_template is not None:
            metadata["user_template_sha256"] = _sha(user_template.encode("utf-8"))
        return metadata


def get_text(section, protocol=None):
    return PromptSource(protocol).get_text(section)


def render_static(section, protocol=None, **values):
    return PromptSource(protocol).render_static(section, **values)
