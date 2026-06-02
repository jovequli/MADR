# Switching LLM Providers

The default config uses **Claude (claude-sonnet-4-6)**. All tested providers are already supported in `scripts/llm_client.py` — you only need to:

1. Set your API key as an environment variable
2. Change `model`, `provider`, and `api_key_env` in the config JSON

No code changes required.

---

## Supported Providers

| Provider | Leader Model | Worker Model (MADR) | API Key Env Var | `timeout_sec` |
|----------|-------------|--------------------|-----------------|---------------|
| **Claude** (Anthropic, default) | `claude-sonnet-4-6` | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` | 240 |
| **Gemini** | `gemini-2.5-flash` (or `gemini-3-flash-preview`) | same | `GEMINI_API_KEY` | 240 |
| **DeepSeek** | `deepseek-v4-pro` | `deepseek-v4-flash` | `DEEPSEEK_API_KEY` | 240 |
| **OpenAI** | `gpt-5.4` | `gpt-5.4-mini` | `OPENAI_API_KEY` | 240 |
| **Grok** (xAI) | `grok-4.3` | `grok-4.3` | `GROK_API_KEY` | 240 |
| **Kimi** (Moonshot) | `kimi-k2.6` | `kimi-k2.6` | `KIMI_API_KEY` | **300** |

Additional providers in `llm_client.py` (not exercised in the paper): Qwen (`DASHSCOPE_API_KEY`), MiniMax (`MINIMAX_API_KEY`).

### SDK Dependencies

| Provider | Required Package |
|----------|-----------------|
| Claude | `pip install anthropic` |
| Gemini | `pip install google-genai` |
| DeepSeek, OpenAI, Grok, Kimi | `pip install openai` |
| Kimi (with proxy bypass) | `pip install openai httpx` |

---

## Step 1: Set Your API Key

```bash
export ANTHROPIC_API_KEY="your-key"    # Claude (default)
export GEMINI_API_KEY="your-key"       # Gemini
export DEEPSEEK_API_KEY="your-key"     # DeepSeek
export OPENAI_API_KEY="your-key"       # OpenAI
export GROK_API_KEY="your-key"         # Grok (xAI)
export KIMI_API_KEY="your-key"         # Kimi (Moonshot)
```

## Step 2: Edit Config JSON

In `MADR/configs/bengbu_MADR_default.json` or `SADR/configs/bengbu_SADR_default.json`, replace the model/provider/api_key fields.

### MADR Architecture (Leader + Worker LLMs)

Replace **both** the `leader` block **and** the LLM fields in every entry of the `workers` array.

```json
"leader": {
    "agent_id": "leader_bengbu",
    "model": "<MODEL>",
    "provider": "<PROVIDER>",
    "temperature": 0.3,
    "max_tokens": 8192,
    "api_key_env": "<API_KEY_ENV>",
    "timeout_sec": 240.0
}
```

For each worker, add:

```json
{
    "agent_id": "worker_xixian",
    "model": "<WORKER_MODEL>",
    "provider": "<PROVIDER>",
    "api_key_env": "<API_KEY_ENV>",
    "temperature": 0.4,
    "max_tokens": 8192,
    "timeout_sec": 60.0,
    ...
}
```

### SADR Architecture (Leader-only)

Only the `leader` block has LLM fields. Workers do **not** invoke the LLM and have no `model`/`provider`/`api_key_env`.

---

## Provider-Specific Configs (Copy-Paste Ready)

### Claude (default)

```json
"model": "claude-sonnet-4-6",
"provider": "anthropic",
"api_key_env": "ANTHROPIC_API_KEY"
```

Other available models: `claude-opus-4-6`, `claude-haiku-4-5-20251001`

### Gemini

```json
"model": "gemini-2.5-flash",
"provider": "gemini",
"api_key_env": "GEMINI_API_KEY"
```

Other available models: `gemini-2.5-pro`, `gemini-2.5-flash-lite`, `gemini-3-flash-preview`, `gemini-3-pro-preview`

### DeepSeek

```json
// Leader
"model": "deepseek-v4-pro",
"provider": "deepseek",
"api_key_env": "DEEPSEEK_API_KEY"

// Worker (MADR) — use the lighter flash model
"model": "deepseek-v4-flash",
"provider": "deepseek",
"api_key_env": "DEEPSEEK_API_KEY"
```

Other available models: `deepseek-chat`, `deepseek-reasoner`

### OpenAI

```json
// Leader
"model": "gpt-5.4",
"provider": "openai",
"api_key_env": "OPENAI_API_KEY"

// Worker (MADR)
"model": "gpt-5.4-mini",
"provider": "openai",
"api_key_env": "OPENAI_API_KEY"
```

Other available models: `gpt-4o`, `gpt-4o-mini`, `gpt-5.4-nano`, `gpt-5.5`

> **Note**: GPT-5.x and o-series models use `max_completion_tokens` instead of `max_tokens`. This is handled automatically by `llm_client.py`.

### Grok (xAI)

```json
"model": "grok-4.3",
"provider": "grok",
"api_key_env": "GROK_API_KEY"
```

Other available models: `grok-3`, `grok-3-mini`

### Kimi (Moonshot)

```json
"model": "kimi-k2.6",
"provider": "kimi",
"api_key_env": "KIMI_API_KEY"
```

> **Important**: Set `"timeout_sec": 300.0` for **both** the leader and the workers (Kimi K2.6 is slower). The code automatically forces `temperature=1.0` for K2.6 (its only supported value) and bypasses system proxy for Chinese domestic API endpoints.

Other available models: `kimi-k2.5`, `kimi-latest`, `moonshot-v1-auto`

---

## Provider-Specific Behavior (Handled Automatically)

| Provider | Behavior | How it's handled |
|----------|----------|------------------|
| **OpenAI GPT-5.x / o-series** | Uses `max_completion_tokens` instead of `max_tokens`; does not accept `temperature` | Auto-detected by model name |
| **Kimi K2.6** | Only supports `temperature=1.0`; needs ≥ 1024 tokens for thinking overhead | Auto-forced when model name contains `k2.6` |
| **Kimi / Qwen** | May fail if the system has a global HTTP proxy blocking Chinese API endpoints | Proxy bypass via `httpx.Client(proxy=None)` |
| **DeepSeek / Grok** | Not standard OpenAI providers but use OpenAI-compatible API format | Registered with custom `base_url` in provider defaults |
| **All providers** | Truncated JSON output (finish_reason = max_tokens) | Auto-retry with 2× tokens (up to 16384) |
