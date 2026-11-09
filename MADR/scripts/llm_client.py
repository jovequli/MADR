#!/usr/bin/env python3
"""Provider-specific API clients with common structured-response handling.

Uses native Anthropic and Google SDKs and OpenAI-compatible interfaces where
configured. Responses and provider-reported usage retain their original audit identity."""

from __future__ import annotations

import json
import copy
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDER_DEFAULTS: Dict[str, Dict[str, str]] = {
    "anthropic": {
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com",  # explicit: ignore endpoint env overrides
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
    },
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com",  # Gemini Developer API
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
    "claude-sonnet-5-5": "anthropic",
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
    "gemini-3.8-flash": "gemini",
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
    "deepseek-flash": "deepseek",
    "deepseek-pro": "deepseek",
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


def normalize_response_optional_nulls(data, role, context=None):
    """Entry point for runtime-metadata normalization."""
    from .output_contract import normalize_runtime_metadata
    return normalize_runtime_metadata(data, role, context)


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
    reasoning_content: Optional[str] = None  # provider-returned channel; never parsed as final JSON
    normalization_audit: Optional[Dict[str, Any]] = None  # audited runtime metadata and optional-null normalization


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
    failure_mode: str = "fallback"   # stop: exact attempt cap, strict JSON/schema validation
    json_mode: bool = True              # request structured JSON output
    extra_params: Dict[str, Any] = field(default_factory=dict)
    static_input_protocol: Optional[Dict[str, str]] = None


class LLMCallExhausted(RuntimeError):
    """No usable response within the exact attempt cap; usage remains auditable."""
    def __init__(self, attempts, error_type, failure_class, usage_summary):
        super().__init__(f"LLM call failed after {attempts} attempts ({error_type})")
        self.failure_class = failure_class
        self.usage_summary = copy.deepcopy(usage_summary)


class LLMClient:
    """Unified LLM client that dispatches to the appropriate backend."""

    def __init__(self, config: LLMClientConfig):
        self.config = config
        if config.failure_mode not in ('fallback', 'stop', 'historical_fallback'):
            raise ValueError('Unknown LLM failure_mode')
        self._strict_contract = config.failure_mode in ('stop', 'historical_fallback')
        from .execution_protocol import validate_static_input_protocol
        config.static_input_protocol = validate_static_input_protocol(config.static_input_protocol)
        if type(config.max_retries) is not int or config.max_retries < 1:
            raise ValueError("max_retries is the positive total framework attempt cap")
        if not config.provider:
            config.provider = infer_provider(config.model)
        self.provider = config.provider
        self._validate_provider_options()

        # Resolve API key
        self._api_key = config.api_key
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

    def _validate_provider_options(self):
        """Provider controls must not silently disappear or override core request fields."""
        options = self.config.extra_params
        if not isinstance(options, dict):
            raise ValueError("extra_params must be a dictionary")
        if "prompt_cache_system" in options:
            if self.provider != "anthropic" or type(options["prompt_cache_system"]) is not bool:
                raise ValueError("prompt_cache_system must be a boolean for the anthropic provider only")
        limit = options.get("retry_max_tokens", 16384)
        if type(limit) is not int or limit < 1:
            raise ValueError("retry_max_tokens must be a positive integer")
        if "retry_max_tokens" in options and limit < self.config.max_tokens:
            raise ValueError("retry_max_tokens must be at least max_tokens")
        if self.provider in ("anthropic", "gemini"):
            allowed = {"retry_max_tokens", "temperature_policy", "process_trace_path", "process_trace_enabled"}
            allowed |= ({"thinking", "output_config", "prompt_cache_system"}
                        if self.provider == "anthropic" else {"thinking_config"})
            if set(options) - allowed:
                raise ValueError("Unsupported native-provider extra_params key")
            if "temperature_policy" in options and options["temperature_policy"] != "omit":
                raise ValueError("Native-provider temperature_policy must be omit")
            if self.provider == "anthropic":
                thinking = options.get("thinking")
                output = options.get("output_config")
                if thinking is not None:
                    if (self.config.model != "claude-sonnet-5-5" or not isinstance(thinking, dict)
                            or set(thinking) != {"type"} or thinking["type"] not in ("adaptive", "between_tools")):
                        raise ValueError("Sonnet 5.5 thinking must be adaptive or between_tools with no additional fields")
                    if output != {"effort": "high"}:
                        raise ValueError("Sonnet 5.5 requests use output_config.effort = high")
                if output is not None and output != {"effort": "high"}:
                    raise ValueError("Sonnet 5.5 requests require output_config={effort: high}")
            else:
                thinking = options.get("thinking_config")
                if thinking is not None and (self.config.model != "gemini-3.8-flash" or not isinstance(thinking, dict)
                        or set(thinking) != {"thinking_level"} or thinking["thinking_level"] not in ("low", "medium")):
                    raise ValueError("Gemini 3.8 Flash thinking_config must contain thinking_level low or medium")
            return
        if self.provider != "deepseek":
            return
        allowed = {"thinking", "reasoning_effort", "retry_max_tokens", "temperature_policy",
                   "process_trace_path", "process_trace_enabled"}
        if set(options) - allowed:
            raise ValueError("Unsupported DeepSeek extra_params key")
        if "temperature_policy" in options and options["temperature_policy"] != "omit":
            raise ValueError("DeepSeek temperature_policy, when supplied, must be omit")
        thinking = options.get("thinking")
        if thinking is not None and (not isinstance(thinking, dict)
                or set(thinking) != {"type"} or thinking["type"] not in ("enabled", "disabled")):
            raise ValueError("DeepSeek thinking must contain type enabled or disabled")
        effort = options.get("reasoning_effort")
        if effort is not None and effort not in ("none", "low", "high", "max"):
            raise ValueError("Unsupported DeepSeek reasoning_effort")
        if thinking and ((thinking["type"] == "enabled" and effort == "none")
                or (thinking["type"] == "disabled" and effort not in (None, "none"))):
            raise ValueError("Conflicting DeepSeek thinking and reasoning_effort")

    def _provider_request_options(self):
        keys = {"deepseek": ("thinking", "reasoning_effort"),
                "anthropic": ("thinking", "output_config"),
                "gemini": ("thinking_config",)}.get(self.provider, ())
        return {k: copy.deepcopy(self.config.extra_params[k]) for k in keys if k in self.config.extra_params}

    def _thinking_explicitly_enabled(self):
        options = self._provider_request_options()
        return (options.get("thinking", {}).get("type") == "enabled"
                or options.get("reasoning_effort") in ("low", "high", "max"))

    def _temperature_omitted(self):
        if self.provider in ("anthropic", "gemini"):
            return self.config.extra_params.get("temperature_policy") == "omit"
        return self.provider == "deepseek" and (
            self.config.extra_params.get("temperature_policy") == "omit"
            or self._thinking_explicitly_enabled())

    def _connection_probe_tokens(self):
        """Reserve the configured budget when a probe can produce thinking."""
        if self.provider in ("anthropic", "gemini"):
            return self.config.max_tokens
        if self.provider == "deepseek":
            options = self._provider_request_options()
            disabled = (options.get("thinking", {}).get("type") == "disabled"
                        or options.get("reasoning_effort") == "none")
            # Reserve a thinking-capable probe budget when controls are absent.
            # Request fields remain omitted so the provider controls its defaults.
            if self._thinking_explicitly_enabled() or (self.config.model == "deepseek-flash" and not disabled):
                return self.config.max_tokens
        return 64

    def _request_controls(self, temperature, max_tokens):
        controls = {"temperature": temperature, "max_tokens": max_tokens}
        if self.provider == "deepseek":
            controls.update(self._provider_request_options())
            controls["temperature_requested"] = temperature
            controls["thinking_setting_source"] = ("explicit" if self._provider_request_options()
                                                    else "provider_default_not_pinned")
            if self.config.extra_params.get("temperature_policy") == "omit":
                controls["temperature_policy"] = "omit"
                controls["temperature"] = None
                controls["temperature_status"] = "omitted: explicit experiment policy"
            elif self._thinking_explicitly_enabled():
                controls["temperature"] = None
                controls["temperature_status"] = "omitted: unsupported in thinking mode"
            controls["response_format"] = {"type": "json_object"} if self.config.json_mode else None
            controls["retry_max_tokens"] = self.config.extra_params.get("retry_max_tokens", 16384)
        elif self.provider in ("anthropic", "gemini"):
            controls["temperature_requested"] = temperature
            controls.update(self._provider_request_options())
            controls["thinking_setting_source"] = ("explicit" if self._provider_request_options()
                                                    else "provider_default_not_pinned")
            controls["tools_sent"] = False
            controls["retry_max_tokens"] = self.config.extra_params.get("retry_max_tokens", 16384)
            if self._temperature_omitted():
                controls["temperature"] = None
                controls["temperature_policy"] = "omit"
                controls["temperature_status"] = "omitted: explicit experiment policy"
            controls["system_transport"] = ("system" if self.provider == "anthropic"
                                            else "system_instruction")
            if self.provider == "anthropic":
                controls["prompt_cache_system"] = self.config.extra_params.get("prompt_cache_system", False)
            controls["json_transport"] = ("prompt_and_local_validation" if self.provider == "anthropic"
                                          else "application/json" if self.config.json_mode else None)
        return controls

    def _retry_token_limit(self, current_tokens):
        return max(current_tokens, self.config.extra_params.get("retry_max_tokens", 16384))

    # ----- public API -----

    _emits_attempt_events = True

    def chat(self, messages, system="", json_schema=None, temperature=None, max_tokens=None):
        """API calls with per-backend-invocation evidence.

        Strict API runs disable SDK retries and enforce one shared attempt cap.
        """
        from .process_trace import call_context, emit_attempt, new_id, response_evidence
        from .llm_failure_policy import reported_usage_totals
        temp = temperature if temperature is not None else self.config.temperature
        tokens = max_tokens if max_tokens is not None else self.config.max_tokens
        context = call_context(self)
        self.last_attempt_ids = []
        self.last_failure_class = None
        self.last_usage_summary = {'known_usage': {'prompt': 0, 'completion': 0, 'total': 0},
            'unknown_usage_attempts': 0, 'observed_attempts': 0, 'usage_complete': True,
            'scope': 'sum of provider-reported usage across all attempts; unknown is not zero'}
        failure_stages = []
        transport_sequence = 0
        latest_attempt_id = None
        response_received = False
        attempt_messages = copy.deepcopy(messages)

        def invoke(outer_attempt, max_toks, reason):
            nonlocal transport_sequence, latest_attempt_id, response_received
            transport_sequence += 1
            latest_attempt_id = new_id()
            self.last_attempt_ids.append(latest_attempt_id)
            self.last_usage_summary['observed_attempts'] += 1
            response_received = False
            identity = {"attempt_id": latest_attempt_id, "outer_attempt": outer_attempt,
                        "transport_attempt": transport_sequence, "invocation_reason": reason,
                        "provider": self.provider, "model_requested": self.config.model,
                        "record_scope": ("one API invocation; SDK retries disabled" if self._strict_contract
                                         else "framework backend invocation; SDK internal retries not enumerated")}
            emit_attempt(context, "attempt_request", **identity,
                         request={"system": system, "messages": attempt_messages},
                         controls=self._request_controls(temp, max_toks))
            started = time.monotonic()
            try:
                sdk_hint = PROVIDER_DEFAULTS.get(self.provider, {}).get("sdk", "")
                if self.provider == "anthropic" or sdk_hint == "anthropic":
                    response = self._call_anthropic(attempt_messages, system, temp, max_toks)
                elif self.provider == "gemini":
                    response = self._call_gemini(attempt_messages, system, temp, max_toks)
                else:
                    response = self._call_openai_compat(attempt_messages, system, temp, max_toks)
                response.duration_sec = round(time.monotonic() - started, 3)
            except Exception as error:
                self.last_usage_summary['unknown_usage_attempts'] += 1
                self.last_usage_summary['usage_complete'] = False
                failure_stages.append('transport')
                emit_attempt(context, "attempt_error", **identity,
                             stage="backend_invocation", error_type=type(error).__name__,
                             duration_sec=round(time.monotonic() - started, 3))
                raise
            response_received = True
            # This event must precede JSON extraction, repair and schema validation.
            evidence = response_evidence(response)
            usage = reported_usage_totals(evidence)
            for key in ('prompt', 'completion', 'total'):
                if usage[key] is not None:
                    self.last_usage_summary['known_usage'][key] += usage[key]
            if any(usage[key] is None for key in ('prompt', 'completion', 'total')):
                self.last_usage_summary['unknown_usage_attempts'] += 1
                self.last_usage_summary['usage_complete'] = False
            emit_attempt(context, "attempt_response", **{**identity, **evidence, "record_scope": identity["record_scope"]},
                         duration_sec=response.duration_sec, recorded_before_parse=True)
            return response

        last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                resp = invoke(attempt, tokens, "initial" if attempt == 1 else "outer_retry")
                if self.config.json_mode and resp.content:
                    resp.parsed = self._extract_json(resp.content, prefer_last=(self.provider == "anthropic"))
                if (not self._strict_contract and resp.finish_reason == "max_tokens" and self.config.json_mode
                        and resp.parsed is None and tokens < self._retry_token_limit(tokens)):
                    retry_tokens = max(tokens, min(tokens * 2, self._retry_token_limit(tokens)))
                    logger.warning("Truncated JSON detected (max_tokens=%d). Retrying with max_tokens=%d ...",
                                   tokens, retry_tokens)
                    emit_attempt(context, "retry_scheduled", previous_attempt_id=latest_attempt_id,
                                 outer_attempt=attempt, reason="unparsed_max_tokens",
                                 retry_max_tokens=retry_tokens)
                    resp = invoke(attempt, retry_tokens, "truncation_retry")
                    if self.config.json_mode and resp.content:
                        resp.parsed = self._extract_json(resp.content, prefer_last=(self.provider == "anthropic"))
                if self._strict_contract:
                    if self.config.json_mode and resp.parsed is None:
                        raise ValueError("Unparsed JSON response")
                    if resp.finish_reason in ("max_tokens", "length"):
                        raise ValueError("Truncated response is not an accepted logical result")
                    if self.provider == "deepseek" and resp.finish_reason != "stop":
                        # A parseable object may still be an aborted, filtered or
                        # resource-limited partial response. Only normal completion
                        # is an accepted DeepSeek result for this JSON-only protocol.
                        raise ValueError("DeepSeek response did not finish normally")
                    if self.provider == "anthropic" and resp.finish_reason != "end_turn":
                        raise ValueError("Anthropic response did not finish normally")
                    if self.provider == "gemini" and resp.finish_reason != "stop":
                        raise ValueError("Gemini response did not finish normally")
                    # Normalize only after raw response archival and normal
                    # completion checks. Truncated/malformed results cannot be
                    # made acceptable by this narrow optional-field adapter.
                    role = getattr(self, "response_role", None)
                    if role is not None and resp.parsed is not None:
                        resp.parsed, resp.normalization_audit = normalize_response_optional_nulls(resp.parsed, role, context.get("meta", {}))
                        emit_attempt(context, "response_normalization", attempt_id=latest_attempt_id,
                                     outer_attempt=attempt, transport_attempt=transport_sequence,
                                     normalization=resp.normalization_audit)
                    validator = getattr(self, "response_validator", None)
                    if validator is not None:
                        validator(resp.parsed)
                if json_schema and resp.parsed is not None:
                    self._validate_schema(resp.parsed, json_schema)
                emit_attempt(context, 'logical_call_accounting', outcome='accepted',
                             usage_summary=self.last_usage_summary,
                             attempt_ids=list(self.last_attempt_ids))
                return resp
            except Exception as error:
                last_error = error
                if response_received:
                    failure_stages.append('invalid_response')
                    emit_attempt(context, "attempt_error", attempt_id=latest_attempt_id,
                                 outer_attempt=attempt, transport_attempt=transport_sequence,
                                 stage="parse_or_schema", error_type=type(error).__name__,
                                 validation_detail=(str(error)[:300] if isinstance(error, ValueError) else None))
                logger.warning("LLM call attempt %d/%d failed (%s): %s",
                               attempt, self.config.max_retries, self.provider, type(error).__name__)
                if attempt < self.config.max_retries:
                    if (self._strict_contract and response_received
                            and getattr(resp, "finish_reason", "") not in ("max_tokens", "length")
                            and isinstance(error, ValueError) and resp.parsed is not None):
                        # Repeat the original evidence once, plus one precise format
                        # correction. Never echo the failed long explanation/thoughts.
                        correction = ("Output-format correction: " + str(error)[:240] +
                                      ". Return one corrected JSON object using the same evidence and task. "
                                      "Do not add a new physical diagnosis merely to fix the format.")
                        attempt_messages = copy.deepcopy(messages) + [{"role": "user", "content": correction}]
                    if self._strict_contract and response_received and getattr(resp, "finish_reason", "") in ("max_tokens", "length"):
                        tokens = max(tokens, min(tokens * 2, self._retry_token_limit(tokens)))
                    delay = self.config.retry_delay_sec * (1 if self.config.failure_mode == "stop" else attempt)
                    emit_attempt(context, "retry_scheduled", previous_attempt_id=latest_attempt_id,
                                 outer_attempt=attempt, reason="outer_attempt_error",
                                 delay_sec=delay,
                                 error_type=type(error).__name__, retry_max_tokens=tokens)
                    time.sleep(delay)
        # Never expose SDK URLs, tokens, response bodies or headers in an exception string.
        kinds = set(failure_stages)
        self.last_failure_class = next(iter(kinds)) if len(kinds) == 1 else 'mixed'
        emit_attempt(context, 'logical_call_accounting', outcome='exhausted',
                     failure_class=self.last_failure_class,
                     usage_summary=self.last_usage_summary,
                     attempt_ids=list(self.last_attempt_ids))
        raise LLMCallExhausted(self.config.max_retries, type(last_error).__name__,
                               self.last_failure_class, self.last_usage_summary) from None


    def test_connection(self) -> Dict[str, Any]:
        """Quick connectivity test. Returns model info and latency."""
        from .prompt_source import get_text
        t0 = time.monotonic()
        resp = self.chat(
            messages=[{"role": "user", "content": get_text("llm_connection_user", protocol=self.config.static_input_protocol)}],
            system=get_text("llm_connection_system", protocol=self.config.static_input_protocol),
            temperature=0.0,
            max_tokens=self._connection_probe_tokens(),
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
            if self._strict_contract:
                kwargs["max_retries"] = 0
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
        if self._temperature_omitted():
            kwargs.pop("temperature", None)
        if system:
            if self.config.extra_params.get("prompt_cache_system", False):
                # Same system text and independent messages; only mark the
                # stable prefix for the provider's default five-minute cache.
                kwargs["system"] = [{"type": "text", "text": system,
                                     "cache_control": {"type": "ephemeral"}}]
            else:
                kwargs["system"] = system
        kwargs.update(self._provider_request_options())
        # No forced tools or additional output schema: the existing prompt and
        # local validator remain the JSON contract for native Claude requests.
        resp = client.messages.create(**kwargs)
        content = ""
        reasoning = []
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                content += block.text
            elif getattr(block, "type", "") == "thinking":
                reasoning.append(getattr(block, "thinking", "") or "")
        # Anthropic input_tokens excludes prompt-cache reads and writes.
        input_total = (resp.usage.input_tokens + (getattr(resp.usage, "cache_read_input_tokens", None) or 0)
                       + (getattr(resp.usage, "cache_creation_input_tokens", None) or 0))
        return LLMResponse(
            content=content,
            model=resp.model,
            provider=self.provider,
            prompt_tokens=input_total,
            completion_tokens=resp.usage.output_tokens,
            total_tokens=input_total + resp.usage.output_tokens,
            finish_reason=resp.stop_reason or "",
            raw_response=resp,
            reasoning_content="\n".join(reasoning) or None,
        )

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
                **({"max_retries": 0} if self._strict_contract else {}),
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
        provider_options = self._provider_request_options()
        if provider_options:
            # OpenAI SDK extra_body forwards provider-specific JSON fields verbatim.
            kwargs["extra_body"] = provider_options
        if self._temperature_omitted():
            # The local omission policy is never forwarded in extra_body.
            kwargs.pop("temperature", None)

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
            reasoning_content=getattr(choice.message, "reasoning_content", None),
        )

    # ----- Google Gemini -----

    def _get_gemini_client(self):
        """Get or create google.genai Client (new SDK)."""
        if self._gemini_model is None:
            try:
                from google import genai
            except ImportError:
                raise ImportError("pip install google-genai")
            from google.genai import types
            http_options = {"timeout": int(self.config.timeout_sec * 1000)}
            if self._strict_contract:
                http_options["retry_options"] = types.HttpRetryOptions(attempts=1)
            if self._base_url:
                http_options["base_url"] = self._base_url
            self._gemini_model = genai.Client(
                api_key=self._api_key, vertexai=False,
                http_options=types.HttpOptions(**http_options),
            )
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
        for msg in messages:
            role = "model" if msg["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

        # Build generation config
        gen_config_kwargs = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if self._temperature_omitted():
            gen_config_kwargs.pop("temperature", None)
        if system:
            gen_config_kwargs["system_instruction"] = system
        if self.config.json_mode:
            gen_config_kwargs["response_mime_type"] = "application/json"
        if "thinking_config" in self._provider_request_options():
            gen_config_kwargs["thinking_config"] = types.ThinkingConfig(**self._provider_request_options()["thinking_config"])
        gen_config = types.GenerateContentConfig(**gen_config_kwargs)

        resp = client.models.generate_content(
            model=self.config.model,
            contents=contents,
            config=gen_config,
        )

        # Only final text is parsed; returned thought summaries are auxiliary.
        text_parts, reasoning_parts = [], []
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            for part in getattr(content, "parts", None) or []:
                value = getattr(part, "text", None)
                if value:
                    (reasoning_parts if getattr(part, "thought", False) else text_parts).append(value)
        text = "".join(text_parts)
        usage = getattr(resp, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        final_tokens = getattr(usage, "candidates_token_count", 0) or 0
        thinking_tokens = getattr(usage, "thoughts_token_count", 0) or 0
        # Gemini reports final and thinking tokens separately. Compatibility
        # completion includes both, while raw usage is preserved for comparison.
        completion_tokens = final_tokens + thinking_tokens
        reported_total = getattr(usage, "total_token_count", None)
        total_tokens = reported_total if reported_total is not None else prompt_tokens + completion_tokens
        finish_reason = ""
        if candidates:
            raw_fr = getattr(candidates[0], "finish_reason", None)
            if raw_fr is not None:
                finish_reason = str(getattr(raw_fr, "value", raw_fr)).split(".")[-1].lower()
        if finish_reason == "max_tokens":
            logger.warning("Gemini response TRUNCATED (max_output_tokens=%d).", max_tokens)
        return LLMResponse(
            content=text,
            model=getattr(resp, "model_version", None) or self.config.model,
            provider="gemini",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
            raw_response=resp,
            reasoning_content="\n".join(reasoning_parts) or None,
        )

    # ----- Utilities -----

    @staticmethod
    def _fix_json_text(text: str) -> str:
        """Fix common LLM JSON issues before parsing."""
        import re
        # 1. Replace the invalid JSON escape \' with '
        text = text.replace("\\'", "'")
        # 2. Remove trailing commas before } or ] (common LLM mistake)
        text = re.sub(r',\s*([}\]])', r'\1', text)
        # 3. Fix truncated decimals: "0.01." → "0.01"
        text = re.sub(r'(\d+\.\d+)\.(\s*[,}\]\n])', r'\1\2', text)
        # 4. Fix trailing dot on integers: ": 8." → ": 8.0"
        text = re.sub(r':\s*(\d+)\.\s*([,}\]])', r': \1.0\2', text)
        return text

    @staticmethod
    def _ensure_dict(parsed: Any) -> Optional[Dict[str, Any]]:
        """Ensure parsed JSON is a dict. Handle common non-dict returns."""
        if isinstance(parsed, dict):
            return parsed
        # A response may be a JSON string that itself contains JSON
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
    def _extract_json(text: str, prefer_last: bool = False) -> Optional[Dict[str, Any]]:
        """Try to extract JSON from LLM response text."""
        text = text.strip()
        # A response may contain several JSON values; use the last complete
        # top-level value without counting its nested objects twice.
        if prefer_last:
            decoder = json.JSONDecoder(); cursor = 0; last_value = None
            while cursor < len(text):
                start = next((i for i in range(cursor, len(text)) if text[i] in "{["), None)
                if start is None: break
                try:
                    value, end = decoder.raw_decode(text, start)
                except json.JSONDecodeError:
                    cursor = start + 1
                else:
                    last_value = value
                    cursor = end
            if isinstance(last_value, dict):
                return last_value
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
    def _validate_schema(data: Any, schema: Dict, path: str = "$") -> None:
        from .output_contract import validate_schema
        validate_schema(data, schema, path)


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
