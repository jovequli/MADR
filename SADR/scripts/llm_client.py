#!/usr/bin/env python3
"""Unified LLM client for agent team calibration.

Supports multiple backends through OpenAI-compatible API format:
- Anthropic Claude (via native SDK)
- OpenAI / Codex
- Google Gemini (via google-genai SDK)
- DeepSeek — OpenAI-compatible
- Grok (xAI) — OpenAI-compatible
- Kimi (Moonshot) — OpenAI-compatible
- Qwen (DashScope) — OpenAI-compatible
- MiniMax — OpenAI-compatible

All backends expose the same interface: chat() -> structured JSON.
"""

from __future__ import annotations

import json
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
        "base_url": "",  # uses native SDK
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
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
    },
    "grok": {
        "api_key_env": "GROK_API_KEY",
        "base_url": "https://api.x.ai/v1",
    },
    "minimax": {
        "api_key_env": "MINIMAX_API_KEY",
        "base_url": "https://api.minimaxi.com/anthropic",
        "sdk": "anthropic",
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
    "gpt-5.4": "openai",
    "gpt-5.4-mini": "openai",
    "gpt-5.4-nano": "openai",
    "gpt-5.5": "openai",
    # Gemini
    "gemini-3-pro-preview": "gemini",
    "gemini-3-flash-preview": "gemini",
    "gemini-2.5-pro": "gemini",
    "gemini-2.5-flash": "gemini",
    "gemini-2.5-flash-lite": "gemini",
    # DeepSeek
    "deepseek-v4-pro": "deepseek",
    "deepseek-v4-flash": "deepseek",
    "deepseek-chat": "deepseek",
    "deepseek-reasoner": "deepseek",
    # Grok
    "grok-4.3": "grok",
    "grok-3": "grok",
    "grok-3-mini": "grok",
    # Kimi
    "kimi-k2.5": "kimi",
    "kimi-k2.6": "kimi",
    "moonshot-v1-auto": "kimi",
    "kimi-latest": "kimi",
    # Qwen
    "qwen3-max": "qwen",
    "qwen3-coder-plus": "qwen",
    "qwen-max": "qwen",
    "qwen-plus": "qwen",
    # MiniMax
    "MiniMax-M2.5": "minimax",
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
    if "deepseek" in low:
        return "deepseek"
    if "grok" in low:
        return "grok"
    if "minimax" in low:
        return "minimax"
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

    # ----- public API -----

    def chat(
        self,
        messages: List[Dict[str, str]],
        system: str = "",
        json_schema: Optional[Dict] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """Send a chat request and return structured response.

        Args:
            messages: List of {"role": "user"|"assistant", "content": "..."}
            system: System prompt (handled differently per provider)
            json_schema: Optional JSON schema for structured output validation
            temperature: Override default temperature
            max_tokens: Override default max_tokens

        Returns:
            LLMResponse with content, parsed JSON, and usage stats
        """
        temp = temperature if temperature is not None else self.config.temperature
        tokens = max_tokens if max_tokens is not None else self.config.max_tokens

        last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                t0 = time.monotonic()
                # Determine SDK backend: some providers use anthropic-compatible API
                sdk_hint = PROVIDER_DEFAULTS.get(self.provider, {}).get("sdk", "")
                if self.provider == "anthropic" or sdk_hint == "anthropic":
                    resp = self._call_anthropic(messages, system, temp, tokens)
                elif self.provider == "gemini":
                    resp = self._call_gemini(messages, system, temp, tokens)
                else:
                    # OpenAI-compatible (openai, kimi, qwen)
                    resp = self._call_openai_compat(messages, system, temp, tokens)
                resp.duration_sec = round(time.monotonic() - t0, 3)

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
                    elif self.provider == "anthropic" or sdk_hint == "anthropic":
                        resp = self._call_anthropic(messages, system, temp, retry_tokens)
                    else:
                        resp = self._call_openai_compat(messages, system, temp, retry_tokens)
                    resp.duration_sec = round(time.monotonic() - t0, 3)
                    if self.config.json_mode and resp.content:
                        resp.parsed = self._extract_json(resp.content)

                # Validate against schema if provided
                if json_schema and resp.parsed is not None:
                    self._validate_schema(resp.parsed, json_schema)

                return resp

            except Exception as e:
                last_error = e
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

    # ----- OpenAI-compatible -----

    def _get_openai_client(self):
        if self._openai_client is None:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError("pip install openai")
            kwargs = {
                "api_key": self._api_key,
                "base_url": self._base_url or None,
                "timeout": self.config.timeout_sec,
            }
            # Bypass system proxy for Chinese domestic APIs (kimi, qwen)
            if self.provider in ("kimi", "qwen"):
                import httpx
                kwargs["http_client"] = httpx.Client(proxy=None, timeout=self.config.timeout_sec)
            self._openai_client = OpenAI(**kwargs)
        return self._openai_client

    def _call_openai_compat(
        self, messages: List[Dict], system: str, temperature: float, max_tokens: int,
    ) -> LLMResponse:
        client = self._get_openai_client()
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        # Kimi K2.6 only supports temperature=1 and needs higher min tokens (thinking overhead)
        if "k2.6" in self.config.model:
            temperature = 1.0
            max_tokens = max(max_tokens, 1024)

        # GPT-5.x and o-series require max_completion_tokens instead of max_tokens
        model_low = self.config.model.lower()
        use_new_tokens = any(k in model_low for k in ["gpt-5", "o1", "o3", "o4"])

        kwargs: Dict[str, Any] = {
            "model": self.config.model,
            "messages": msgs,
        }
        if use_new_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["temperature"] = temperature
            kwargs["max_tokens"] = max_tokens
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
        """Basic JSON schema validation. Checks required fields exist."""
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        missing = [k for k in required if k not in data]
        if missing:
            raise ValueError(f"JSON schema validation failed. Missing required fields: {missing}")
        for k, v in data.items():
            if k in properties:
                expected_type = properties[k].get("type")
                if expected_type == "number" and not isinstance(v, (int, float)):
                    raise ValueError(f"Field '{k}' expected number, got {type(v).__name__}")
                if expected_type == "string" and not isinstance(v, str):
                    raise ValueError(f"Field '{k}' expected string, got {type(v).__name__}")
                if expected_type == "array" and not isinstance(v, list):
                    raise ValueError(f"Field '{k}' expected array, got {type(v).__name__}")
                if expected_type == "object" and not isinstance(v, dict):
                    raise ValueError(f"Field '{k}' expected object, got {type(v).__name__}")


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
