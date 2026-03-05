---
name: LiteLLM Integration Analysis
overview: Analysis of what LiteLLM offers over LocalWriter's current LLM support, and a concrete minimal integration proposal that avoids bundling the full 54MB library.
todos:
  - id: create-providers-py
    content: "Create core/providers.py with provider prefix detection, default endpoints, and auth header factories for Anthropic (x-api-key), Gemini (query param key), Azure (api-key), and OpenAI-default (Authorization: Bearer)"
    status: pending
  - id: wire-api-py
    content: Update LlmClient._headers() and _endpoint() in core/api.py to call providers.py for auto-detection when the user has not set a custom endpoint
    status: pending
  - id: update-config-py
    content: Update get_api_config() in core/config.py to include provider auto-detection based on model name prefix
    status: pending
  - id: update-agents-md
    content: Update AGENTS.md to document the new provider auto-detection system
    status: pending
isProject: false
---

For what was already adopted (streaming edge cases), see [LITELLM_INTEGRATION.md](LITELLM_INTEGRATION.md). This document describes an optional provider auto-detection proposal.

# LiteLLM Integration Analysis & Proposal

## What LocalWriter Already Has

Your `core/api.py` (`LlmClient`) already covers everything LiteLLM offers for OpenAI-compatible endpoints:

- Persistent HTTP connections (connection pooling per instance)
- SSE streaming with `[DONE]` handling and OpenRouter comment skipping
- Tool-calling: both streaming (`accumulate_delta`) and non-streaming
- Thinking/reasoning token extraction (`reasoning_content`, `thought`, `thinking`, `reasoning_details`)
- Provider-specific headers: OpenRouter (`HTTP-Referer`, `X-Title`), OpenWebUI (`/api` path)
- Stop signal propagation
- One-retry on connection errors with user-friendly error messages

Your approach (user configures endpoint URL + model) works perfectly for: Ollama, OpenWebUI, LM Studio, OpenAI, OpenRouter, Together AI, Mistral (they all have OpenAI-compatible `/v1/chat/completions`).

## What LiteLLM Uniquely Provides

The **one real gap** is support for providers that are **not OpenAI-compatible** in auth or request format:


| Provider                   | Auth Gap vs. LocalWriter                                                                      | Request Format Gap                                                 |
| -------------------------- | --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **Anthropic (native)**     | Uses `x-api-key` header + `anthropic-version: 2023-06-01` header, NOT `Authorization: Bearer` | Content blocks (`[{"type":"text","text":"..."}]`) vs plain strings |
| **Google Gemini (native)** | API key as **query param** `?key={api_key}`, no auth header                                   | Different endpoint structure                                       |
| **Azure OpenAI**           | `api-key` header (not `Authorization: Bearer`) + deployment-based URLs                        | Mostly OpenAI-compatible                                           |
| **AWS Bedrock**            | AWS SigV4 signature (cryptographic signing)                                                   | Varies by model                                                    |


Everything else LiteLLM does (router, caching, logging, proxy) is explicitly excluded.

## Why Not Bundle LiteLLM

- **Size**: 54MB, ~82K lines, 113 provider implementations — way too large for a LibreOffice extension
- **Dependencies**: Requires `pydantic`, `httpx`, `tiktoken`, `openai` SDK, and more
- **Coupling**: Heavy interdependencies make extraction difficult — `main.py` (7.4K lines), `utils.py` (9.2K lines), `router.py` (9.2K lines) are all intertwined
- **Overkill**: You only need 2-3 provider auth adaptors, not 113

## The Real-World Impact

Most users already work around the non-OpenAI-compat gap by:

- Using **OpenRouter** (OpenAI-compatible, supports Claude, Gemini, etc.) — your code already handles this
- Using **LM Studio / Ollama** for local models

Direct-to-Anthropic and direct-to-Google are a real but narrow use case.

## Proposal: A Minimal `core/providers.py`

Extract only the auth header and endpoint logic (~150-200 lines, no new dependencies). This module would:

1. **Detect provider from model name prefix** (inspired by LiteLLM's `get_llm_provider_logic.py`):

```python
PROVIDER_PREFIXES = {
    "claude-": "anthropic",
    "anthropic/": "anthropic",
    "gemini/": "gemini",
    "google/": "gemini",
    "azure/": "azure",
    "gpt-": "openai",
    "o1": "openai",
    "o3": "openai",
}
```

1. **Return the correct auth headers per provider**:

```python
def get_provider_headers(provider, api_key):
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01", ...}
    if provider == "gemini":
        return {}  # key goes in query param
    return {"Authorization": f"Bearer {api_key}"}  # default
```

1. **Return the default endpoint URL per provider** (so users don't need to configure it):

```python
DEFAULT_ENDPOINTS = {
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",  # has OAI-compat endpoint
    "openai": "https://api.openai.com",
}
```

1. **Wire into `LlmClient._headers()`** in `[core/api.py](core/api.py)`: when no endpoint is configured but a recognized model prefix is found, auto-detect provider and apply correct auth.

### Note on Anthropic Request Format

Anthropic's native API uses a slightly different message format. However: **Google Gemini now has an OpenAI-compatible endpoint** at `https://generativelanguage.googleapis.com/v1beta/openai/` that accepts standard OpenAI format. So only Anthropic needs a request transformer (~50 lines).

### What This Gives Users

Users could configure just:

- **Model**: `claude-3-5-sonnet-20241022` (no endpoint needed — auto-detected)
- **API key**: their Anthropic key
- LocalWriter auto-uses `x-api-key` header + correct Anthropic endpoint

This is the concrete, scoped benefit LiteLLM's routing logic provides — extracted into ~200 lines with zero new dependencies.

## Files to Change

- New: `[core/providers.py](core/providers.py)` — provider detection + auth header factory (~150-200 lines)
- Edit: `[core/api.py](core/api.py)` — `_headers()` and `_endpoint()` to call `providers.py` when no custom endpoint is set
- Edit: `[core/config.py](core/config.py)` — auto-detect provider in `get_api_config()` when model prefix is recognized
- Optional: `[AGENTS.md](AGENTS.md)` — document new provider auto-detection

