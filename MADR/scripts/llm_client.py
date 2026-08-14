#!/usr/bin/env python3
"""Unified LLM client for agent team calibration.

Supports multiple backends through OpenAI-compatible API format:
- Anthropic Claude (via native SDK)
- OpenAI / Codex
- Google Gemini (via google-genai SDK)
- Kimi (Moonshot) — OpenAI-compatible
- Qwen (DashScope) — OpenAI-compatible
- MiniMax — OpenAI-compatible

All backends expose the same interface: chat() -> structured JSON.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verbatim LLM call logging (write-before-parse)
# ---------------------------------------------------------------------------
# Every request/response that passes through LLMClient.chat() is appended as
# one JSON line to the file named by the MADR_LLM_LOG environment variable
# (set by run_langgraph at startup to <output_root>/llm_calls.jsonl).
#
# Design rules:
#   * The raw response text is persisted BEFORE any JSON parsing is attempted,
#     so parse failures can never lose evidence.
#   * Logging is strictly additive and never raises: any logging failure is
#     swallowed (with a logger warning) so it cannot affect the run.
#   * Append-only JSONL: crash-safe (at worst one truncated final line),
#     greppable, and no SQLite/WAL hazards.

_LLM_LOG_ENV = "MADR_LLM_LOG"
_llm_log_lock = threading.Lock()


def _append_llm_log(record: Dict[str, Any]) -> None:
    """Append one record to the verbatim call log. Never raises."""
    path = os.environ.get(_LLM_LOG_ENV, "").strip()
    if not path:
        return
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _llm_log_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
    except Exception:
        logger.warning("Failed to append LLM call log (non-fatal)", exc_info=True)


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": "",  # uses native SDK
    },
    "claude_cli": {
        # Invokes the local `claude` CLI binary instead of HTTP API.
        # Auth handled by Claude Code's own credential store (no API key needed).
        "api_key_env": "",
        "base_url": "",
        "binary": "claude",  # override via config.base_url if non-standard path
    },
    "codex_cli": {
        # Invokes the local `codex exec` CLI instead of the OpenAI HTTP API.
        # Auth handled by Codex's own credential store (no API key needed).
        "api_key_env": "",
        "base_url": "",
        "binary": "codex",  # override via config.base_url if non-standard path
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
    },
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "",  # uses native SDK
    },
    "kimi": {
        "api_key_env": "KIMI_API_KEY",
        "base_url": "https://api.moonshot.cn/v1",
    },
    "qwen": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "minimax": {
        "api_key_env": "MINIMAX_API_KEY",
        "base_url": "https://api.minimaxi.com/anthropic",
        "sdk": "anthropic",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
    },
}

# Model -> provider mapping (common models)
MODEL_PROVIDER_MAP: Dict[str, str] = {
    # Anthropic
    "claude-opus-4-6": "anthropic",
    "claude-sonnet-4-6": "anthropic",
    "claude-haiku-4-5-20251001": "anthropic",
    # OpenAI / Codex
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "gpt-5.2": "openai",
    "gpt-5.3-codex": "openai",
    "gpt-5.2-codex": "openai",
    "gpt-5.1-codex-max": "openai",
    "gpt-5.1-codex-mini": "openai",
    # Gemini
    "gemini-3-pro-preview": "gemini",
    "gemini-3-flash-preview": "gemini",
    "gemini-2.5-pro": "gemini",
    "gemini-2.5-flash": "gemini",
    "gemini-2.5-flash-lite": "gemini",
    # Kimi
    "kimi-k2.5": "kimi",
    "moonshot-v1-auto": "kimi",
    "kimi-latest": "kimi",
    # Qwen
    "qwen3-max": "qwen",
    "qwen3-coder-plus": "qwen",
    "qwen-max": "qwen",
    "qwen-plus": "qwen",
    # MiniMax
    "MiniMax-M2.5": "minimax",
    # DeepSeek
    "deepseek-v4-pro": "deepseek",
    "deepseek-v4-flash": "deepseek",
    "deepseek-chat": "deepseek",
    "deepseek-reasoner": "deepseek",
}


def infer_provider(model: str) -> str:
    """Infer provider from model name."""
    if model in MODEL_PROVIDER_MAP:
        return MODEL_PROVIDER_MAP[model]
    low = model.lower()
    if "claude" in low:
        return "anthropic"
    if "gpt" in low or "o1" in low or "o3" in low or "codex" in low:
        return "openai"
    if "gemini" in low:
        return "gemini"
    if "moonshot" in low or "kimi" in low:
        return "kimi"
    if "qwen" in low:
        return "qwen"
    if "minimax" in low:
        return "minimax"
    if "deepseek" in low:
        return "deepseek"
    raise ValueError(f"Cannot infer provider for model '{model}'. "
                     f"Specify provider explicitly or add to MODEL_PROVIDER_MAP.")


# ---------------------------------------------------------------------------
# Response dataclass
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """Standardized response from any LLM backend."""
    content: str                        # raw text content
    parsed: Optional[Dict[str, Any]] = None  # parsed JSON (if json mode)
    model: str = ""
    provider: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_sec: float = 0.0
    finish_reason: str = ""
    raw_response: Any = None            # original SDK response object


# ---------------------------------------------------------------------------
# LLMClient
# ---------------------------------------------------------------------------

@dataclass
class LLMClientConfig:
    """Configuration for a single LLM client instance."""
    model: str
    provider: str = ""                  # auto-inferred if empty
    api_key_env: str = ""               # env var name for API key
    api_key: str = ""                   # direct key (not recommended)
    base_url: str = ""                  # override base URL
    temperature: float = 0.3
    max_tokens: int = 4096
    timeout_sec: float = 120.0
    max_retries: int = 3
    retry_delay_sec: float = 2.0
    json_mode: bool = True              # request structured JSON output
    extra_params: Dict[str, Any] = field(default_factory=dict)


class LLMClient:
    """Unified LLM client that dispatches to the appropriate backend."""

    def __init__(self, config: LLMClientConfig):
        self.config = config
        if not config.provider:
            config.provider = infer_provider(config.model)
        self.provider = config.provider

        # Resolve API key (CLI providers skip this — they use their own auth)
        self._api_key = config.api_key
        if self.provider not in {"claude_cli", "codex_cli"}:
            if not self._api_key:
                env_var = config.api_key_env or PROVIDER_DEFAULTS.get(
                    self.provider, {}
                ).get("api_key_env", "")
                if env_var:
                    self._api_key = os.environ.get(env_var, "")
            if not self._api_key:
                raise ValueError(
                    f"No API key for provider '{self.provider}'. "
                    f"Set env var or pass api_key directly."
                )

        # Resolve base URL
        self._base_url = config.base_url or PROVIDER_DEFAULTS.get(
            self.provider, {}
        ).get("base_url", "")

        # Lazy-init SDK clients
        self._openai_client = None
        self._anthropic_client = None
        self._gemini_model = None

    # ----- public API -----

    def chat(
        self,
        messages: List[Dict[str, str]],
        system: str = "",
        json_schema: Optional[Dict] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """Send a chat request and return structured response.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."}
            system: System prompt (handled differently per provider)
            json_schema: Optional JSON schema for structured output validation
            temperature: Override default temperature
            max_tokens: Override default max_tokens
            meta: Optional caller identity for the verbatim call log, e.g.
                {"role": "worker", "subbasin": "bengbu", "round": 5,
                 "phase": "VIC_ONLY"}. Purely observational — has no effect
                on the request itself.

        Returns:
            LLMResponse with content, parsed JSON, and usage stats
        """
        temp = temperature if temperature is not None else self.config.temperature
        tokens = max_tokens if max_tokens is not None else self.config.max_tokens

        # --- verbatim call log context (write-before-parse) ---
        call_id = uuid.uuid4().hex[:12]
        _request_snapshot = {"system": system, "messages": messages}

        def _log_response(resp_obj: "LLMResponse", event: str,
                          attempt_no: int, max_toks: int) -> None:
            _append_llm_log({
                "event": event,
                "call_id": call_id,
                "attempt": attempt_no,
                "ts": _utc_now_iso(),
                "provider": resp_obj.provider or self.provider,
                "model": resp_obj.model or self.config.model,
                "temperature": temp,
                "max_tokens": max_toks,
                "meta": meta or {},
                "request": _request_snapshot,
                "response_text": resp_obj.content,
                "finish_reason": resp_obj.finish_reason,
                "usage": {
                    "prompt": resp_obj.prompt_tokens,
                    "completion": resp_obj.completion_tokens,
                    "total": resp_obj.total_tokens,
                },
                "duration_sec": resp_obj.duration_sec,
            })

        last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                t0 = time.monotonic()
                # Determine SDK backend: some providers use anthropic-compatible API
                sdk_hint = PROVIDER_DEFAULTS.get(self.provider, {}).get("sdk", "")
                if self.provider == "claude_cli":
                    resp = self._call_claude_cli(messages, system, temp, tokens)
                elif self.provider == "codex_cli":
                    resp = self._call_codex_cli(
                        messages, system, temp, tokens, json_schema=json_schema,
                    )
                elif self.provider == "anthropic" or sdk_hint == "anthropic":
                    resp = self._call_anthropic(messages, system, temp, tokens)
                elif self.provider == "gemini":
                    resp = self._call_gemini(messages, system, temp, tokens)
                else:
                    # OpenAI-compatible (openai, kimi, qwen)
                    resp = self._call_openai_compat(messages, system, temp, tokens)
                resp.duration_sec = round(time.monotonic() - t0, 3)

                # Persist the raw response BEFORE any parsing (write-before-parse)
                _log_response(resp, "response", attempt, tokens)

                # Try to parse JSON from content
                if self.config.json_mode and resp.content:
                    resp.parsed = self._extract_json(resp.content)

                # Auto-retry with 2x tokens when truncation caused JSON parse failure
                if (resp.finish_reason == "max_tokens"
                        and self.config.json_mode
                        and resp.parsed is None
                        and tokens < 16384):
                    retry_tokens = min(tokens * 2, 16384)
                    logger.warning(
                        "Truncated JSON detected (max_tokens=%d). "
                        "Retrying with max_tokens=%d ...",
                        tokens, retry_tokens,
                    )
                    t0 = time.monotonic()
                    if self.provider == "gemini":
                        resp = self._call_gemini(messages, system, temp, retry_tokens)
                    elif self.provider == "claude_cli":
                        resp = self._call_claude_cli(messages, system, temp, retry_tokens)
                    elif self.provider == "codex_cli":
                        resp = self._call_codex_cli(
                            messages, system, temp, retry_tokens,
                            json_schema=json_schema,
                        )
                    elif self.provider == "anthropic" or sdk_hint == "anthropic":
                        resp = self._call_anthropic(messages, system, temp, retry_tokens)
                    else:
                        resp = self._call_openai_compat(messages, system, temp, retry_tokens)
                    resp.duration_sec = round(time.monotonic() - t0, 3)
                    _log_response(resp, "response_truncation_retry", attempt, retry_tokens)
                    if self.config.json_mode and resp.content:
                        resp.parsed = self._extract_json(resp.content)

                # Validate against schema if provided
                if json_schema and resp.parsed is not None:
                    self._validate_schema(resp.parsed, json_schema)

                return resp

            except Exception as e:
                last_error = e
                # Failed attempts are logged too, so systematic provider
                # failures (e.g. a Worker falling back to the executor for
                # most of a run) leave a complete audit trail.
                _append_llm_log({
                    "event": "error",
                    "call_id": call_id,
                    "attempt": attempt,
                    "ts": _utc_now_iso(),
                    "provider": self.provider,
                    "model": self.config.model,
                    "temperature": temp,
                    "max_tokens": tokens,
                    "meta": meta or {},
                    "request": _request_snapshot,
                    "error": f"{type(e).__name__}: {e}",
                })
                logger.warning(
                    "LLM call attempt %d/%d failed (%s): %s",
                    attempt, self.config.max_retries, self.provider, e,
                )
                if attempt < self.config.max_retries:
                    time.sleep(self.config.retry_delay_sec * attempt)

        raise RuntimeError(
            f"LLM call failed after {self.config.max_retries} retries: {last_error}"
        ) from last_error

    def test_connection(self) -> Dict[str, Any]:
        """Quick connectivity test. Returns model info and latency."""
        t0 = time.monotonic()
        resp = self.chat(
            messages=[{"role": "user", "content": "Reply with exactly: {\"status\": \"ok\"}"}],
            system="You are a test assistant. Always reply in valid JSON.",
            temperature=0.0,
            max_tokens=64,
        )
        latency = round(time.monotonic() - t0, 3)
        return {
            "provider": self.provider,
            "model": self.config.model,
            "status": "ok" if resp.parsed and resp.parsed.get("status") == "ok" else "unexpected_response",
            "latency_sec": latency,
            "raw_content": resp.content[:200],
            "tokens": {
                "prompt": resp.prompt_tokens,
                "completion": resp.completion_tokens,
                "total": resp.total_tokens,
            },
        }

    # ----- Anthropic native SDK -----

    def _get_anthropic_client(self):
        if self._anthropic_client is None:
            try:
                import anthropic
            except ImportError:
                raise ImportError("pip install anthropic")
            kwargs = {
                "api_key": self._api_key,
                "timeout": self.config.timeout_sec,
            }
            # Support custom base_url for Anthropic-compatible providers (e.g., MiniMax)
            if self._base_url:
                kwargs["base_url"] = self._base_url
            self._anthropic_client = anthropic.Anthropic(**kwargs)
        return self._anthropic_client

    def _call_anthropic(
        self, messages: List[Dict], system: str, temperature: float, max_tokens: int,
    ) -> LLMResponse:
        client = self._get_anthropic_client()
        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        resp = client.messages.create(**kwargs)
        content = ""
        for block in resp.content:
            if hasattr(block, "text"):
                content += block.text
        return LLMResponse(
            content=content,
            model=resp.model,
            provider=self.provider,
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
            finish_reason=resp.stop_reason or "",
            raw_response=resp,
        )

    # ----- Claude CLI (local `claude` binary, no HTTP API) -----

    def _call_claude_cli(
        self, messages: List[Dict], system: str, temperature: float, max_tokens: int,
    ) -> LLMResponse:
        """Invoke the local `claude` CLI binary instead of the HTTP API.

        Auth is handled by Claude Code's own credential store (OAuth /
        subscription / keychain), so no API key is required.

        Note: the CLI does not expose `temperature` or `max_tokens` as flags.
        These config knobs are ignored when calling via CLI; the CLI uses
        Claude Code's own defaults for sampling.
        """
        import subprocess
        import shutil
        import tempfile

        # Resolve binary path (config.base_url can override the default `claude`)
        binary = self._base_url or PROVIDER_DEFAULTS.get(
            self.provider, {}
        ).get("binary", "claude")
        if not shutil.which(binary) and not os.path.isabs(binary):
            raise RuntimeError(
                f"Cannot find '{binary}' on PATH. Install Claude Code or set "
                f"`base_url` in config to the full path of the binary."
            )

        # Flatten messages into a single user prompt for -p mode.
        # If multiple messages exist (rare in our stateless flow), we
        # concatenate them with role markers so the conversation context is
        # preserved.
        if len(messages) == 1 and messages[0].get("role", "user") == "user":
            user_prompt = messages[0]["content"]
        else:
            parts = []
            for m in messages:
                role = m.get("role", "user").upper()
                parts.append(f"[{role}]\n{m.get('content','')}")
            user_prompt = "\n\n".join(parts)

        # Build the command. System prompt goes through a temp file because
        # our system prompts are large (~10K chars) and embedding via argv
        # works but is cleaner via --system-prompt-file.
        cmd = [
            binary,
            "-p",                       # non-interactive print mode
            "--model", self.config.model,
            "--output-format", "json",
            "--tools", "",              # pure LLM backend: disable all tools
        ]
        sys_path = None
        if system:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as tf:
                    tf.write(system)
                    sys_path = tf.name
                cmd += ["--system-prompt-file", sys_path]
            except OSError as e:
                logger.warning(
                    "Could not write system prompt to temp file (%s); "
                    "falling back to inline arg.", e
                )
                cmd += ["--system-prompt", system]

        try:
            result = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_sec,
            )
        except subprocess.TimeoutExpired as e:
            if sys_path:
                try:
                    os.unlink(sys_path)
                except OSError:
                    pass
            raise RuntimeError(
                f"claude CLI timed out after {self.config.timeout_sec}s"
            ) from e
        finally:
            if sys_path:
                try:
                    os.unlink(sys_path)
                except OSError:
                    pass

        if result.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {result.returncode}. "
                f"stderr: {result.stderr[:500]}"
            )

        # Parse JSON wrapper
        try:
            wrapper = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude CLI returned non-JSON output (parse error: {e}). "
                f"First 300 chars: {result.stdout[:300]}"
            ) from e

        if wrapper.get("is_error"):
            err = wrapper.get("api_error_status") or wrapper.get("subtype", "unknown")
            raise RuntimeError(f"claude CLI reported error: {err}")

        content = wrapper.get("result", "") or ""
        usage = wrapper.get("usage", {}) or {}
        # input_tokens here counts only the *non-cached* portion; the cached
        # portion is recorded separately. For consistency with the other
        # providers we sum cache_creation + cache_read + input_tokens.
        prompt_tokens = (
            usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
        )
        completion_tokens = usage.get("output_tokens", 0)

        return LLMResponse(
            content=content,
            model=self.config.model,
            provider=self.provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            finish_reason=wrapper.get("stop_reason", ""),
            raw_response=wrapper,
        )

    # ----- Codex CLI (local `codex exec` binary, no HTTP API) -----

    def _call_codex_cli(
        self,
        messages: List[Dict],
        system: str,
        temperature: float,
        max_tokens: int,
        json_schema: Optional[Dict] = None,
    ) -> LLMResponse:
        """Invoke local ``codex exec`` and return its final agent message.

        Auth is handled by Codex's credential store. The CLI does not expose
        ``temperature`` or ``max_tokens`` controls, so those values are request
        metadata only and are not effective sampling/output-limit settings for
        this provider.

        User/history content is always delivered through stdin so large Worker
        prompts never consume argv space. ``system`` is supplied as Codex
        ``developer_instructions`` and an optional response schema is supplied
        through ``--output-schema`` after normalizing a strict Codex-only copy.
        Stdout is a JSONL event stream; its exact text is retained in
        ``LLMResponse.raw_response``.
        """
        import shutil
        import subprocess
        import tempfile

        binary = self._base_url or PROVIDER_DEFAULTS.get(
            self.provider, {}
        ).get("binary", "codex")
        if not shutil.which(binary) and not os.path.isabs(binary):
            raise RuntimeError(
                f"Cannot find '{binary}' on PATH. Install Codex CLI or set "
                f"`base_url` in config to the full path of the binary."
            )

        if len(messages) == 1 and messages[0].get("role", "user") == "user":
            user_prompt = messages[0].get("content", "")
        else:
            parts = []
            for message in messages:
                role = message.get("role", "user").upper()
                parts.append(f"[{role}]\n{message.get('content', '')}")
            user_prompt = "\n\n".join(parts)

        cmd = [
            binary,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox", "read-only",
            "--model", self.config.model,
            "--color", "never",
            "-c", 'approval_policy="never"',
        ]
        if system:
            # JSON string syntax is valid TOML basic-string syntax for the
            # characters emitted here, and subprocess receives argv directly.
            encoded_system = json.dumps(system, ensure_ascii=False)
            cmd += ["-c", f"developer_instructions={encoded_system}"]

        schema_path = None
        if json_schema:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8"
                ) as schema_file:
                    json.dump(
                        self._codex_strict_output_schema(json_schema),
                        schema_file,
                        ensure_ascii=False,
                    )
                    schema_path = schema_file.name
                cmd += ["--output-schema", schema_path]
            except OSError as exc:
                raise RuntimeError(
                    f"Could not write Codex output schema temp file: {exc}"
                ) from exc

        # A lone '-' makes stdin the full prompt. Never move user_prompt into
        # argv: production Worker prompts can contain tens of thousands of
        # tokens and approach the platform ARG_MAX limit.
        cmd.append("-")

        try:
            result = subprocess.run(
                cmd,
                input=user_prompt,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"codex CLI timed out after {self.config.timeout_sec}s"
            ) from exc
        finally:
            if schema_path:
                try:
                    os.unlink(schema_path)
                except OSError:
                    pass

        if result.returncode != 0:
            # Codex reports structured API failures on stdout while routine
            # startup warnings go to stderr. Preserve stdout in the exception
            # so chat()'s error record contains the actionable event stream.
            raise RuntimeError(
                f"codex CLI exited {result.returncode}. "
                f"stdout: {result.stdout or '<empty>'} "
                f"stderr tail: {result.stderr[-2000:]}"
            )

        raw_event_stream = result.stdout
        events: List[Dict[str, Any]] = []
        for line_no, line in enumerate(raw_event_stream.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"codex CLI returned malformed JSONL at line {line_no}: "
                    f"{exc}. First 300 chars: {line[:300]}"
                ) from exc
            if not isinstance(event, dict):
                raise RuntimeError(
                    f"codex CLI JSONL line {line_no} is not an object"
                )
            events.append(event)

        failure_event = next(
            (
                event for event in events
                if event.get("type") in {"error", "turn.failed"}
            ),
            None,
        )
        if failure_event is not None:
            detail = failure_event.get("error") or failure_event.get("message")
            raise RuntimeError(
                f"codex CLI reported failure: {detail or failure_event}. "
                f"Event stream: {raw_event_stream}"
            )

        agent_messages = []
        usage: Dict[str, Any] = {}
        turn_completed = False
        for event in events:
            event_type = event.get("type")
            item = event.get("item")
            if (
                event_type == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                agent_messages.append(item["text"])
            elif event_type == "turn.completed":
                turn_completed = True
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]

        if not agent_messages:
            raise RuntimeError(
                "codex CLI JSONL contained no completed agent_message"
            )
        if not turn_completed:
            raise RuntimeError(
                "codex CLI JSONL contained no turn.completed event"
            )

        # Verified against codex-cli 0.144.2: input_tokens already includes
        # cached_input_tokens, and output_tokens already includes
        # reasoning_output_tokens. The cached/reasoning fields are subsets and
        # must not be added again when computing LLMResponse totals.
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        completion_tokens = int(usage.get("output_tokens", 0) or 0)

        return LLMResponse(
            content=agent_messages[-1],
            model=self.config.model,
            provider=self.provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            finish_reason="stop",
            raw_response=raw_event_stream,
        )

    @staticmethod
    def _codex_strict_output_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
        """Return a strict schema copy accepted by Codex structured output.

        Codex requires every object property to appear in ``required`` and
        disallows undeclared properties. This narrows only the temporary schema
        sent to the CLI; chat() still validates the parsed response against the
        caller's original schema after verbatim logging.
        """
        def _strictify(node: Any) -> Any:
            if isinstance(node, list):
                return [_strictify(item) for item in node]
            if not isinstance(node, dict):
                return node

            strict = {key: _strictify(value) for key, value in node.items()}
            properties = strict.get("properties")
            if strict.get("type") == "object" and isinstance(properties, dict):
                strict["required"] = list(properties.keys())
                strict["additionalProperties"] = False
            return strict

        return _strictify(schema)

    # ----- OpenAI-compatible -----

    def _get_openai_client(self):
        if self._openai_client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError("pip install openai")
            self._openai_client = OpenAI(
                api_key=self._api_key,
                base_url=self._base_url or None,
                timeout=self.config.timeout_sec,
            )
        return self._openai_client

    def _call_openai_compat(
        self, messages: List[Dict], system: str, temperature: float, max_tokens: int,
    ) -> LLMResponse:
        client = self._get_openai_client()
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self.config.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        resp = client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = resp.usage
        return LLMResponse(
            content=choice.message.content or "",
            model=resp.model,
            provider=self.provider,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            finish_reason=choice.finish_reason or "",
            raw_response=resp,
        )

    # ----- Google Gemini -----

    def _get_gemini_client(self):
        """Get or create google.genai Client (new SDK)."""
        if self._gemini_model is None:
            try:
                from google import genai
            except ImportError:
                raise ImportError("pip install google-genai")
            self._gemini_model = genai.Client(api_key=self._api_key)
        return self._gemini_model

    def _call_gemini(
        self, messages: List[Dict], system: str, temperature: float, max_tokens: int,
    ) -> LLMResponse:
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError("pip install google-genai")

        client = self._get_gemini_client()

        # Build contents in Gemini format
        contents = []
        if system:
            contents.append({"role": "user", "parts": [{"text": f"[System instruction]\n{system}"}]})
            contents.append({"role": "model", "parts": [{"text": "Understood. I will follow these instructions."}]})
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        # Build generation config
        gen_config_kwargs = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if self.config.json_mode:
            gen_config_kwargs["response_mime_type"] = "application/json"
        gen_config = types.GenerateContentConfig(**gen_config_kwargs)

        resp = client.models.generate_content(
            model=self.config.model,
            contents=contents,
            config=gen_config,
        )

        # Extract text
        try:
            text = resp.text if resp.text else ""
        except (ValueError, AttributeError):
            text = ""
            if resp.candidates:
                for part in resp.candidates[0].content.parts:
                    text += part.text
            if not text:
                logger.warning("Gemini response had no extractable text.")

        # Usage metadata
        prompt_tokens = 0
        completion_tokens = 0
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            prompt_tokens = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            completion_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0

        # Extract actual finish reason (was hardcoded as "stop" — caused silent truncation)
        finish_reason = "stop"
        if resp.candidates:
            raw_fr = getattr(resp.candidates[0], "finish_reason", None)
            if raw_fr is not None:
                # Enum like FinishReason.STOP → "stop", FinishReason.MAX_TOKENS → "max_tokens"
                finish_reason = str(raw_fr).split(".")[-1].lower()
        if finish_reason == "max_tokens":
            logger.warning(
                "Gemini response TRUNCATED (finish_reason=MAX_TOKENS, "
                "max_output_tokens=%d, completion_tokens=%d). Output may be incomplete.",
                max_tokens, completion_tokens,
            )

        return LLMResponse(
            content=text,
            model=self.config.model,
            provider="gemini",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            finish_reason=finish_reason,
            raw_response=resp,
        )

    # ----- Utilities -----

    @staticmethod
    def _fix_json_text(text: str) -> str:
        """Fix common LLM JSON issues before parsing."""
        import re
        # 1. Gemini sometimes uses \' (invalid JSON escape) instead of '
        text = text.replace("\\'", "'")
        # 2. Remove trailing commas before } or ] (common LLM mistake)
        text = re.sub(r',\s*([}\]])', r'\1', text)
        # 3. Fix truncated decimals: "0.01." → "0.01" (Gemini Flash)
        text = re.sub(r'(\d+\.\d+)\.(\s*[,}\]\n])', r'\1\2', text)
        # 4. Fix trailing dot on integers: ": 8." → ": 8.0"
        text = re.sub(r':\s*(\d+)\.\s*([,}\]])', r': \1.0\2', text)
        return text

    @staticmethod
    def _ensure_dict(parsed: Any) -> Optional[Dict[str, Any]]:
        """Ensure parsed JSON is a dict. Handle common non-dict returns."""
        if isinstance(parsed, dict):
            return parsed
        # Gemini sometimes returns a JSON string that itself contains JSON
        if isinstance(parsed, str):
            try:
                inner = json.loads(parsed)
                if isinstance(inner, dict):
                    return inner
            except (json.JSONDecodeError, TypeError):
                pass
        # If it's a list, look for the first dict element
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    return item
        logger.debug("Parsed JSON is %s, not dict. Falling through.", type(parsed).__name__)
        return None

    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        """Try to extract JSON from LLM response text."""
        text = text.strip()
        fixed_text = LLMClient._fix_json_text(text)

        # Direct JSON parse (try fixed first, then original)
        for candidate in (fixed_text, text):
            try:
                result = LLMClient._ensure_dict(json.loads(candidate))
                if result is not None:
                    return result
            except json.JSONDecodeError:
                pass

        # Try extracting from markdown code block
        if "```" in text:
            for marker in ("```json", "```"):
                start = text.find(marker)
                if start >= 0:
                    start += len(marker)
                    end = text.find("```", start)
                    if end > start:
                        block = LLMClient._fix_json_text(text[start:end].strip())
                        try:
                            result = LLMClient._ensure_dict(json.loads(block))
                            if result is not None:
                                return result
                        except json.JSONDecodeError:
                            pass

        # Try finding the outermost { ... } block (string-aware)
        brace_start = text.find("{")
        if brace_start >= 0:
            # Find matching closing brace, properly handling strings
            depth = 0
            in_string = False
            escape_next = False
            for i in range(brace_start, len(text)):
                ch = text[i]
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = LLMClient._fix_json_text(text[brace_start:i + 1])
                        try:
                            result = LLMClient._ensure_dict(json.loads(candidate))
                            if result is not None:
                                return result
                        except json.JSONDecodeError as e:
                            logger.debug("JSON brace extraction failed: %s", e)
                            break

        # Last resort: if response was truncated, try to repair by closing open structures
        if brace_start is not None and brace_start >= 0:
            candidate = LLMClient._fix_json_text(text[brace_start:])
            # Try closing open brackets/braces
            for suffix in ('"}]}', '"}]', '"}', '}]}', '}]', '}'):
                try:
                    result = LLMClient._ensure_dict(json.loads(candidate + suffix))
                    if result is not None:
                        return result
                except json.JSONDecodeError:
                    pass

        logger.debug("JSON extraction failed for text[:200]: %s", text[:200])
        return None

    @staticmethod
    def _validate_schema(data: Dict, schema: Dict) -> None:
        """Recursive JSON schema validation.

        Supports: type (object/array/string/number/integer/boolean/null),
        properties recursion, required at every object level,
        additionalProperties:false, items for arrays, and enum.
        Unknown/unsupported schema keywords are ignored.
        """
        LLMClient._validate_node(data, schema, "")

    @staticmethod
    def _validate_node(value, schema: Dict, path: str) -> None:
        loc = path or "<root>"

        expected_type = schema.get("type")
        if expected_type is not None:
            allowed = expected_type if isinstance(expected_type, list) else [expected_type]
            if not any(LLMClient._type_matches(value, t) for t in allowed):
                raise ValueError(
                    f"JSON schema validation failed at {loc}: "
                    f"expected {expected_type}, got {type(value).__name__}"
                )

        enum_values = schema.get("enum")
        if enum_values is not None and value not in enum_values:
            raise ValueError(
                f"JSON schema validation failed at {loc}: "
                f"value {value!r} not in enum {enum_values}"
            )

        if isinstance(value, dict):
            required = schema.get("required", [])
            missing = [k for k in required if k not in value]
            if missing:
                raise ValueError(
                    f"JSON schema validation failed at {loc}: "
                    f"missing required fields {missing}"
                )
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                extra = [k for k in value if k not in properties]
                if extra:
                    raise ValueError(
                        f"JSON schema validation failed at {loc}: unexpected keys {extra}"
                    )
            for k, v in value.items():
                if k in properties and isinstance(properties[k], dict):
                    child_path = f"{path}.{k}" if path else k
                    LLMClient._validate_node(v, properties[k], child_path)
        elif isinstance(value, list):
            items_schema = schema.get("items")
            if isinstance(items_schema, dict):
                for i, item in enumerate(value):
                    LLMClient._validate_node(item, items_schema, f"{path}[{i}]")

    @staticmethod
    def _type_matches(value, type_name: str) -> bool:
        if type_name == "object":
            return isinstance(value, dict)
        if type_name == "array":
            return isinstance(value, list)
        if type_name == "string":
            return isinstance(value, str)
        if type_name == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if type_name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if type_name == "boolean":
            return isinstance(value, bool)
        if type_name == "null":
            return value is None
        return True  # unknown type keyword: ignore


# ---------------------------------------------------------------------------
# Factory helper
# ---------------------------------------------------------------------------

def create_client(
    model: str,
    provider: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    json_mode: bool = True,
    **kwargs,
) -> LLMClient:
    """Convenience factory to create an LLMClient."""
    cfg = LLMClientConfig(
        model=model,
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        **{k: v for k, v in kwargs.items() if k in LLMClientConfig.__dataclass_fields__},
    )
    return LLMClient(cfg)
