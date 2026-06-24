# Simplified Direct Multimodal Vision Support for Chat Sidebar

## Goal Description

Implement direct multimodal vision support in the chatbot sidebar. When a vision-capable LLM model is selected, the chatbot will automatically detect if a graphic is currently selected in the document at the moment the user clicks "Send", export it, and hand it to the model directly in the chat message payload as a native image part.

Additionally, this plan adds support for handling base64 images returned by tools such as `get_document_content(include_images=True)`. When a tool response contains embedded `data:image/...` content (in HTML or text), the system extracts the images and attaches them as native multimodal blocks for the target provider (Anthropic, Gemini, OpenAI-compatible). Huge base64 strings are stripped from text content and replaced with lightweight markers to avoid wasting tokens.

**Key constraints (kept deliberately simple):**
- No new UI elements (no image preview, no drag-and-drop in sidebar).
- No selection change listeners.
- Reuse existing `get_selected_image_base64` / graphic export paths.
- Follow the same patterns as native audio support (`has_native_audio`, persistent cache, catalog + metadata + heuristics).

See the complementary local OCR path in [image-recognition.md](image-recognition.md) (Docling/Paddle venv helpers for precise text extraction and document insert). Native multimodal LLM vision is for *semantics* ("explain this diagram", "what's in this screenshot") and is not a replacement for the local vision stack.

---

## Detection Architecture (preferred order)

`has_native_vision(ctx, model_id, endpoint)` must use this priority (modeled directly on `has_native_audio`):

1. **Persistent user config cache** — `vision_support_map` (`{ "endpoint@model": true/false }`). Successful first use or explicit probes can record results.
2. **Static catalog** — `DEFAULT_MODELS` entries that carry `ModelCapability.VISION | ModelCapability.CHAT`.
3. **Dynamic provider metadata** (when available):
   - OpenRouter / Together and similar: `architecture.input_modalities` containing `"image"` during `/v1/models` fetch.
   - Ollama: `POST /api/show` → `capabilities` array containing `"vision"`.
4. **Name-based heuristics** (last resort only). A single small, well-commented predicate. No growing per-family lists.

Provide `set_native_vision_support(ctx, model_id, endpoint, supported)` to persist results (mirrors audio).

This approach avoids hard-coded model lists for families like Qwen (qwen2-vl / qwen2.5-vl / qwen3-vl + all their tags), Gemma vision variants, Granite, etc.

---

## Proposed Changes

### Model Capabilities & Client Shims

Identify vision capability dynamically and update provider shims + the central client to support multimodal content arrays and to normalize images out of tool results.

#### [MODIFY] [default_models.py](plugin/framework/default_models.py)
- Ensure every model that is vision-capable declares `ModelCapability.VISION` (in addition to CHAT/TOOLS as appropriate).

#### [MODIFY] [model_fetcher.py](plugin/framework/client/model_fetcher.py)
- Implement `has_native_vision(ctx, model_id, endpoint)` following the tiered strategy above.
- Implement `set_native_vision_support(ctx, model_id, endpoint, supported)`.
- Add persistent cache support using config key `"vision_support_map"` (exactly parallel to `"audio_support_map"`).
- Extend `_parse_v1_models_response` (or a dedicated helper) to also return vision-input model IDs when `architecture.input_modalities` contains `"image"`. Store results in a process cache (e.g. `_model_fetch_vision_cache`, keyed the same way as the existing image-output cache).
- Add an Ollama-specific helper to query `POST /api/show` (lightweight, cached) and read the `capabilities` list. Only called when the provider is detected as Ollama.
- Update `get_model_capability` or add a small helper if we want to surface the VISION bit dynamically in the future.

#### [ADD / MODIFY] Shared image extraction helper (llm_client.py or new small module)
- Add (or centralize) logic to walk messages and extract `data:image/...` payloads (from raw strings or from already-structured `image_url` blocks).
- When found inside a non-`user` message:
  - Replace the huge base64 payload in the original text/HTML with a short marker such as `[Image Ref]` (or `[Image: <short description if available>]`).
  - Return the extracted image(s) so they can be re-attached appropriately.
- This logic is used both for tool-result images and for any future cases.

#### [MODIFY] [anthropic_shim.py](plugin/framework/client/anthropic_shim.py)
- In `build_chat_request`, support `content` that is already a list of parts.
- When building the final messages for the wire:
  - Convert `image_url` blocks and any remaining `data:image` strings found in text to Anthropic `image` blocks.
  - Images are allowed inside `tool_result` content for Anthropic — take advantage of this where possible for fidelity.
- Parse any `data:image` strings that arrive from tool results in the history.

#### [MODIFY] [google_shim.py](plugin/framework/client/google_shim.py)
- Support list-style content.
- Convert `image_url` / extracted `data:image` strings into Gemini `inlineData` parts.
- Gemini accepts `inlineData` inside function responses — preserve location when reasonable.

#### [MODIFY] [llm_client.py](plugin/framework/client/llm_client.py) (OpenAIShim + LlmClient)
- `OpenAIShim.build_chat_request` (and the path used by Ollama/Grok/etc. via inheritance) must accept list `content` on messages.
- In `make_chat_request` (or a helper called from it, before handing to the shim):
  - Run the shared image extraction pass over the full message list.
  - For OpenAI-compatible providers: any images found in `tool`, `assistant`, or `system` roles **must** be moved into a `user` role message (OpenAI rule). Prefer the most recent preceding user message or synthesize a minimal user message containing only the image(s) + a marker.
  - After moving, ensure the original huge base64 has been stripped from the non-user message.
- The normalization should be provider-aware (some providers are stricter than others).
- Log at debug level (images will be redacted by existing `redact_sensitive_payload_for_log` logic).

---

### Chat Send & Multimodal Message Assembly

#### [MODIFY] [tool_loop.py](plugin/chatbot/tool_loop.py)
- In `_do_send_chat_with_tools` (or the equivalent point that builds the first user message of a turn):
  ```python
  from plugin.framework.client.model_fetcher import has_native_vision, get_current_endpoint
  from plugin.framework.config import get_text_model
  ...
  model = get_text_model(self.ctx)
  endpoint = get_current_endpoint(self.ctx)
  if has_native_vision(self.ctx, model, endpoint):
      b64 = get_selected_image_base64(...)  # reuse existing helper (takes document model + ctx)
      if b64:
          image_part = {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
          content = []
          if query_text:
              content.append({"type": "text", "text": query_text})
          content.append(image_part)
          self.session.add_user_message(content)
          self._append_response((query_text or "") + " [Image Attached]", role="user")
          # Do *not* re-attach on later rounds of the same tool loop
  else:
      self.session.add_user_message(query_text)
      ...
  ```
- Only attach the *currently selected* image on the very first user message of this send. Do not re-attach it in subsequent tool rounds.
- The document model resolution must use the same logic as the rest of the panel (prefer frame controller model).
- Reuse `plugin.writer.images.image_tools.get_selected_image_base64` (already used by the local vision stack).

---

## Provider-Specific Notes

### Ollama

**Primary detection (2025+):**
- Query `POST /api/show` with `{"model": "<name>"}`.
- Look for `"vision"` in the `capabilities` array.
- This is the supported, reliable signal. Cache the result per (normalized endpoint, model).

**Wire format:**
- When the user configures the standard OpenAI-compatible base (usually `http://localhost:11434`), chat goes through `/v1/chat/completions`.
- Ollama's OpenAI-compatible endpoint accepts the standard `{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}` structure inside user messages.
- `OllamaShim` inherits from `OpenAIShim` for chat requests, so the multimodal payload works without a separate native `/api/chat` code path.
- Tool-result image extraction + move-to-user rule still applies (Ollama's `/v1` endpoint follows the same OpenAI validation).

**Caveats:**
- Some vision-tagged models on Ollama do not advertise tool-calling capability. The main chat tool loop may be limited or fall back.
- Use the persistent `vision_support_map` so the first successful (or failing) interaction with a local model teaches the system permanently.

Name heuristics are only a fallback for Ollama when `/api/show` is unreachable or for completely unknown servers.

### xAI Grok

- Detect via catalog first (`grok-*-vision*` entries), then keyword `grok` + `vision`.
- Fully OpenAI-compatible on `/v1/chat/completions`.
- Same strict "images only in user messages" rule → tool-result extraction workaround is required.
- `GrokShim` inherits the OpenAI path.

### Other OpenAI-compatible locals (LM Studio, llama.cpp server, vLLM, etc.)

- Almost never publish `input_modalities`.
- Rely on the persistent cache + improved name heuristics as last resort.
- Users can manually confirm capability via the cache after one successful image interaction.

---

## Tool Result Image Extraction (get_document_content and similar)

Images arrive inside tool results primarily as `data:image/...;base64,...` strings embedded in HTML (see `writer/format.py:strip_embedded_image_data` and `_DATA_URI_IMAGE_RE`, and the `include_images` path in `writer/content.py`).

Required behavior:
1. Before the wire request is built, scan the message history for such payloads (both raw strings and structured blocks).
2. Extract the base64 + mime type.
3. Replace the payload in the *source* message with a short marker to keep token usage reasonable.
4. Re-attach the image as a proper native part on a message the provider will accept.
   - OpenAI-compatible (Ollama, Grok, OpenRouter, Together, LM Studio, ...): must end up in a `user` role.
   - Anthropic: can stay inside `tool_result`.
   - Gemini: flexible.
5. The extraction pass should be idempotent and should not re-extract markers.

This normalization is best performed in the `LlmClient` layer (inside or just before `make_chat_request`) so all code paths (main chat, future sub-agents, etc.) benefit and the wire logic stays in one place.

Existing log redaction already handles `data:image` payloads — keep that behavior.

---

## Verification Plan

### Automated Tests
- `pytest tests/framework/client/test_model_fetcher.py`
  - `has_native_vision` tiered logic (cache, catalog, OpenRouter input_modalities, mocked Ollama `/api/show`, heuristics).
  - Correct population of the new vision cache from `/v1/models`.
- `pytest tests/framework/client/test_client_llm.py`
  - Multimodal content list construction in OpenAI shim.
  - Image extraction + marker replacement from tool-style HTML containing `data:image`.
  - Re-attachment rules per provider family (OpenAI-strict vs Anthropic/Gemini).
  - No re-attachment of the same image across tool rounds.
- Add a small fixture for a realistic Ollama `/api/show` vision response and an OpenRouter model row with `input_modalities`.

### Manual Verification
1. LibreOffice document with an embedded image → select the image → choose a known vision model (Gemini Flash, Claude 3.x, GPT-4o, Mistral Large with vision, local qwen2.5-vl or llama3.2-vision via Ollama) → send "Describe this image".
2. Verify the model response discusses the actual visual content.
3. In a vision-model chat, ask the agent to rewrite or summarize a section that contains an image. Force or observe a `get_document_content(include_images=true)` tool call. Confirm the vision model receives the image (not just a `[Image Ref]` marker as text).
4. Repeat with an Ollama vision model. Confirm `/api/show` was the source of truth (or that the persistent cache recorded the result).
5. Tool-loop round 2: after the first image-bearing user message, subsequent assistant/tool turns must not duplicate the image.
6. Non-vision model + selected image: image is ignored (plain text path).

---

## Limitations and Notes

- Image payload size: exported graphics are full resolution PNGs encoded as base64. Large images can consume many tokens and may hit provider context or request-size limits. (Future work: optional downscaling / quality parameter.)
- Not all vision models support tools. On some local stacks the agent may have reduced capability when a vision model is chosen.
- Only raster images embedded in the document are supported for the "selection at send" path. Vector content in Draw/Impress is handled via other means (LO-DOM tree).
- The feature only activates for the *main* chat model. Sub-agents / specialized delegates have their own rules.
- Complementary to the local vision helpers: use `domain=vision` + `extract_text_from_image` (or the Run Python Script Vision Helpers) when you need accurate OCR + document-side insertion. Use native multimodal when you want the LLM to *see and reason* about the image.

---

## Implementation Notes & Invariants

- Follow the existing audio pattern for cache keys, config persistence, and the shape of `has_native_*` / `set_native_*`.
- All wire behavior changes belong in `llm_client.py` + the shims. Do not add a second HTTP path.
- Reuse `get_selected_image_base64` and the graphic export machinery already used by the vision venv stack.
- Message history stored in the session / DB will contain list-style content when an image was attached. The UI rendering path must tolerate this (or render a placeholder).
- Keep changes minimal: no new dialogs, no new menu items, no listeners on selection.

---

## Suggested Agent Prompt (for future implementation)

> Implement direct multimodal vision per the revised plan in docs/image-recognition-multimodal-LLMs.md. Add tiered `has_native_vision` + persistent `vision_support_map` exactly mirroring the audio support code. Prioritize OpenRouter `input_modalities` and Ollama `/api/show` capabilities. Update shims and `make_chat_request` to normalize `data:image` payloads out of tool results and re-attach them correctly per provider rules. Support list `content` on messages. In the send path only attach a selected image on the first user message of the turn using the existing `get_selected_image_base64`. Add the required tests. Update the Ollama section behavior in the doc if the implementation reveals new details. Run `make test` at the end.
