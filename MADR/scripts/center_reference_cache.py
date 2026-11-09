"""Reuse authenticated, successful evaluations within one run.

Only center-reference queries use this cache. Candidate and LLM schedules are
unchanged. Entries point to native evidence; they do not impersonate new runs.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
import threading

from .native_audit import atomic_json
from .routing_cache import ContentFingerprinter, flux_inputs, _content_key

PROTOCOL = "same-input-center-reference-v1"


def validate_config(value=None):
    value = {"enabled": True} if value is None else value
    if not isinstance(value, dict) or set(value) != {"enabled"} or type(value["enabled"]) is not bool:
        raise ValueError("center_reference_cache must contain one boolean enabled field")
    return dict(value)


def evaluation_inputs(executor, sid, params, upstream_soil, upstream_routing, fp):
    """Identify executed inputs, scoring implementation, and fixed-flux state."""
    cfg = executor._vic_configs[sid]
    routing = Path(cfg.rout_template_dir).resolve(strict=True)
    templates = {str(p.relative_to(routing)): fp.file(p) for p in sorted(routing.rglob("*"))
                 if p.is_file() and not p.name.startswith("._")}
    external = {}
    for p in routing.glob("*rout_global.txt"):
        for line in p.read_text().splitlines():
            token = line.strip()
            if (not token or token.startswith("#") or ("vic_in" in token and "fluxes_" in token)
                    or token.endswith("/rout_out/") or token.endswith("./rout_out/")):
                continue
            path = Path(token)
            rewritten = any(token.endswith(suffix) and any(routing.glob("*" + suffix))
                            for suffix in ("_direc.txt", "_veloc.txt", "_diff.txt", "_xmask.txt",
                                           "_frac.txt", "_staloc.txt"))
            rewritten = rewritten or (token.endswith("UH.all") and any(routing.glob("*.all")))
            if path.is_absolute() and not rewritten:
                external[str(path)] = fp.file(path)
    fixed_flux = None
    if executor._cache is not None and sid in executor._cache.active:
        status = executor._cache.verify(sid, cfg, params, upstream_soil)
        fixed_flux = {k: status[k] for k in ("key", "manifest_sha256")}
    elif cfg.vic_flux_source:
        raise ValueError("Unverified fixed flux cannot supply an evaluation reference")
    root = Path(__file__).parent
    numerical_code = {name: fp.file(root / name)["sha256"] for name in (
        "vic_executor.py", "vic_runner.py", "routing_utils.py", "routing_cache.py",
        "execution_protocol.py", "center_reference_cache.py")}
    return {
        "protocol": PROTOCOL, "mode": executor.mode, "subbasin_id": sid,
        "phase": executor.reference_phase,
        "effective_parameters": dict(params),
        "vic": flux_inputs(cfg, params, upstream_soil, fp),
        "observation": fp.file(cfg.obs_file), "routing_executable": fp.file(cfg.rout_exe),
        "routing_templates": templates, "routing_external_inputs": external,
        "upstream_routing_in_application_order": [
            {"direction": fp.file(row.direc_path), "velocity": row.velocity,
             "diffusivity": row.diffusivity} for row in upstream_routing or []],
        "fixed_flux": fixed_flux, "numerical_code": numerical_code,
    }


class CenterReferenceCache:
    def __init__(self, output_root):
        self.root = Path(output_root).resolve()
        self.directory = self.root / "center_reference_cache"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "index.json"
        self.fp = ContentFingerprinter()
        self.lock = threading.RLock()
        self.entries = {}
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            if saved.get("protocol") != PROTOCOL or saved.get("run_root") != str(self.root):
                raise ValueError("Reference cache belongs to a different run or protocol")
            self.entries = saved["entries"]

    def _save(self):
        atomic_json(self.path, {"protocol": PROTOCOL, "run_root": str(self.root), "entries": self.entries})

    def _inside(self, relative):
        path = (self.root / relative).resolve(strict=True)
        if self.root not in path.parents:
            raise ValueError("Reference evidence is outside this run")
        return path

    def signature(self, executor, sid, params, upstream_soil, upstream_routing):
        return _content_key(evaluation_inputs(executor, sid, params, upstream_soil, upstream_routing, self.fp))

    def store(self, signature, record):
        score = record.get("metrics", {}).get("NSE")
        tag = record.get("phase_tag", "")
        # Exploration has a different identity convention and is not a center.
        if (record.get("ok") is not True or isinstance(score, bool)
                or not isinstance(score, (int, float)) or not math.isfinite(score)
                or not (tag.startswith("round_") or tag.startswith("center_reference_round_"))):
            return
        flows = [row for row in record.get("artifacts", [])
                 if row.get("saved", "").endswith(".day.gz") and row.get("compression") == "gzip"]
        if not flows:
            return
        source = self.root / "native_audit" / "candidates" / record["attempt_id"] / "execution_record.json"
        if not source.is_file():
            return
        entry = {"source_record": str(source.relative_to(self.root)),
                 "source_record_sha256": self.fp.file(source)["sha256"],
                 "flow_files": [{"path": "native_audit/" + row["saved"],
                                 "sha256": self.fp.file(self.root / "native_audit" / row["saved"])["sha256"],
                                 "daily_sha256": row["sha256"]} for row in flows]}
        with self.lock:
            self.entries[signature] = entry
            self._save()

    def lookup(self, signature, sid, params):
        with self.lock:
            entry = self.entries.get(signature)
            if entry is None:
                return None
            try:
                source = self._inside(entry["source_record"])
                if self.fp.file(source)["sha256"] != entry["source_record_sha256"]:
                    raise ValueError("Reference execution record changed")
                record = json.loads(source.read_text())
                if (record.get("evaluation_input_key") != signature or record.get("subbasin_id") != sid
                        or record.get("ok") is not True or record.get("effective_parameters") != params
                        or not math.isfinite(record["metrics"]["NSE"])):
                    raise ValueError("Reference execution identity differs")
                expected = {"native_audit/" + row["saved"]: row["sha256"]
                            for row in record.get("artifacts", [])
                            if row.get("saved", "").endswith(".day.gz") and row.get("compression") == "gzip"}
                if not expected or expected != {row["path"]: row["daily_sha256"] for row in entry["flow_files"]}:
                    raise ValueError("Reference flow index differs from native evidence")
                for row in entry["flow_files"]:
                    path = self._inside(row["path"])
                    if self.fp.file(path)["sha256"] != row["sha256"]:
                        raise ValueError("Reference daily flow changed")
                    digest = hashlib.sha256()
                    with gzip.open(path, "rb") as stream:
                        for block in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(block)
                    if digest.hexdigest() != row["daily_sha256"]:
                        raise ValueError("Reference daily flow does not match native evidence")
                return {"cache_key": signature, "record": record, "flow_files": entry["flow_files"]}
            except (OSError, EOFError, ValueError, KeyError, TypeError):
                self.entries.pop(signature, None)
                self._save()
                return None

    def materialize(self, hit, destination):
        """Copy only daily-flow evidence; never reuse a mutable native workspace."""
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for row in hit["flow_files"]:
            source = self._inside(row["path"])
            target = destination / source.name.removesuffix(".gz")
            with gzip.open(source, "rb") as original, target.open("wb") as output:
                shutil.copyfileobj(original, output)
