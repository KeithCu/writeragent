# LocalWriter Handover Notes (Multimodal AI Integration)

This document provides a comprehensive brain dump of the work performed to integrate multimodal image generation and editing into LocalWriter. It is intended to help resume work in a fresh context.

## Current Architecture State

### 1. Core Image Services
- **[core/image_service.py](core/image_service.py)**: Provider-agnostic **ImageService**.
  - **Two options**: (1) **AI Horde** — its own API/key; (2) **endpoint** (config value `endpoint`) — uses the **endpoint URL/port and API key from Settings** (same as chat). Only the **model** differs: chat uses the text model, image uses **`image_model`**. Implemented via [LlmClient](core/api.py). Legacy: a previous config value for this provider is accepted and treated as `endpoint`.
  - For the endpoint provider, the image model is taken from config key `image_model`, with fallback to the text/chat model. See [core/config.py](core/config.py) `get_text_model()` and `get_api_config()`.
  - AI Horde: polling and UI non-blocking via `toolkit.processEvents()` in the informer; endpoint provider: single request/response.
  - Merges configuration defaults with tool-provided arguments.
- **[core/image_tools.py](core/image_tools.py)**: Image insertion and selection.
  - **insert_image**: Injects images into Writer/Calc documents.
  - **get_selected_image_base64**: Extracts the currently selected image as base64 for Img2Img.
  - **add_image_to_gallery**: Adds generated images to the LibreOffice Media Gallery (in this file).

### 2. Multi-modal Tools
Integrated into [core/document_tools.py](core/document_tools.py) and available to the LLM:
- **generate_image**: Creates a new image from a prompt, or edits the selected image (pass `source_image='selection'`). Replaces in place when editing. When provider is `endpoint`, the model used is updated in `image_model_lru` after success.

### 3. UI and Configuration

**Model naming (text vs image)**  
- **text_model**: The chat/text model. Stored in config as `text_model`; backward compatibility: read `text_model` or `model`. Used by chat, Extend/Edit Selection, and `get_api_config()` (exposed to LlmClient as `"model"`).
- **image_model**: The model used for image generation when `image_provider=endpoint`. Same endpoint and API key as chat (from Settings); only this model id differs. Stored as `image_model`; LRU list `image_model_lru` for recently used image models.

**Settings dialog** ([LocalWriterDialogs/SettingsDialog.xdl](LocalWriterDialogs/SettingsDialog.xdl))  
- **Tabbed**: Chat/Text tab and Image Generation tab.
- **Chat/Text tab**: Endpoint, **Text/Chat Model** (combobox, LRU `model_lru`), **Image model (same endpoint as chat)** (combobox, LRU `image_model_lru`), API key, API type, temperature, chat max tokens, context length, additional instructions.
- **Image tab**: **Provider (aihorde / same as chat)**, AI Horde API key, width/height, steps, max wait, NSFW options, auto gallery, insert frame, translate prompt options.
- If the tabbed dialog fails to load in some LibreOffice versions, the XML uses `dlg:tabpagecontainer` / `dlg:tabpage`; fallback or alternate layout may be needed.

**Chat sidebar** ([LocalWriterDialogs/ChatPanelDialog.xdl](LocalWriterDialogs/ChatPanelDialog.xdl), [chat_panel.py](chat_panel.py))  
- **AI Model** (combobox): Text/chat model; on send writes to `text_model` and updates `model_lru`.
- **Image model (same endpoint as chat)** (combobox): Image model; on send writes to `image_model` and updates `image_model_lru`.
- Additional instructions are **not** in the sidebar; they are read from config (`additional_instructions`) when building the system prompt. Configure them in Settings only.

- **[main.py](main.py)**: `field_specs` and `direct_keys` include `text_model`, `image_model`, and image-related keys. Settings apply logic updates `model_lru` for `text_model` and `image_model_lru` for `image_model`.

## Pending Tasks & Next Steps

### Critical Fixes
1. **Settings Dialog Tabs**: If the tabbed XDL fails to load in some LibreOffice builds, research correct XML for tabs (e.g. `Step` property or different instantiation). The dialog has Chat/Text and Image tabs with `text_model` and `image_model` comboboxes in the Chat tab.
2. **Img2Img Verification**: Test the **generate_image** edit flow (`source_image='selection'`) with real images (base64 extraction, provider Img2Img params such as `init_strength` for Horde).

### Enhancements
1. **Anchoring & Layout**: Improve **insert_image** to support anchoring modes and text wrapping.
2. **Progress Feedback**: Improve status/informer during long Horde generations (ETA/queue position).
3. **Endpoint image API**: The current endpoint-based image path is a best-effort fit; confirm the actual API’s image endpoint/response shape (e.g. modalities, response format) and adjust [EndpointImageProvider](core/image_service.py) if needed.

## Config → API mapping

Tool handlers in `core/document_tools.py` read config via `get_config_dict(ctx)` and pass values into the image stack:

| Config key | Passed to / API role |
|------------|------------------------|
| `text_model` (or `model`) | Chat/LLM model; `get_api_config()` exposes as `"model"` to LlmClient. |
| `image_model` | Image model when `image_provider=endpoint`; used by [ImageService.get_provider("endpoint")](core/image_service.py) (fallback: text model). |
| `image_model_lru` | Recent image model ids for combobox dropdown (Settings + Chat sidebar). |
| `image_cfg_scale` | Horde `cfg_scale` (via `prompt_strength` in ImageService / AIHordeImageProvider). |
| `image_auto_gallery` | `insert_image(..., add_to_gallery=...)` |
| `image_insert_frame` | `insert_image(..., add_frame=...)` |
| `image_provider`, `image_width`, `image_height`, etc. | ImageService defaults and tool args |

`generate_image` passes `add_to_gallery` and `add_frame` from config into `insert_image`. After a successful image generation via the chat endpoint, the model used is pushed into `image_model_lru`.

When `image_translate_prompt` is True and `image_translate_from` is set (e.g. `es` or `Spanish`), the prompt is translated to English via `opustm_hf_translate` before generation; on failure the original prompt is used.

## Image editing (img2img) and provider support

### Unified create/edit design (implemented)

The backend exposes a single `generate_image(prompt, **kwargs)`; when `source_image` (and optionally `strength`) is passed, the provider performs img2img (edit). The tool layer uses one tool `generate_image` with optional `source_image='selection'` to edit the selected image; when omitted, it creates a new image. This aligns with:

- **OpenAI Responses API**: single `image_generation` tool with `action`: `auto` | `generate` | `edit`
- **Stability / Replicate**: same endpoint with optional `image` parameter (no image → text-to-image; with image → img2img)

### Provider support matrix

| Provider | Image edit (img2img) | How it works |
|----------|----------------------|--------------|
| **AI Horde** | Supported | Uses `source_image` (base64) and `init_strength` / `strength` in the Horde client. No change needed. |
| **Endpoint (OpenRouter)** | Supported | Uses `/api/v1/chat/completions` with `modalities: ["image"]` and message content that includes both the text prompt and the source image: `content: [{ "type": "text", "text": prompt }, { "type": "image_url", "image_url": { "url": "data:image/png;base64,..." } }]`. Same response parsing as for create. |
| **Endpoint (Together / OpenAI-compatible)** | Supported | Same `POST .../images/generations` as create; request body includes optional `image_url` (string). For base64 we send a data URL: `data:image/png;base64,<b64>`. Models that support it (e.g. FLUX.2, FLUX Kontext) use it for img2img; others may ignore or return an error. |

### How to add img2img for another provider

1. In the provider’s `generate()` method, accept `source_image` (base64 str) and optionally `strength` in `**kwargs`.
2. **OpenRouter-style** (chat completions with image output): Include the source image in the chat message content (e.g. text part + `image_url` part with data URL) and keep using the same request/response path; parse images from the response as for create.
3. **Together-style** (dedicated images endpoint): Pass `source_image` or `image_url` into the image request builder (e.g. `make_image_request`); add `"image_url": <data URL or URL>` to the JSON body. Use the same POST and response parsing as for create.
4. Document the provider and any model-specific behavior (e.g. which models support edit) in this section or in [`AGENTS.md`](../AGENTS.md).

---

## Technical References
- **AI Horde Client**: [core/aihordeclient/](core/aihordeclient/) — low-level API (async submit, queue, poll, download). See [`AGENTS.md`](../AGENTS.md) for a short project overview.
- **Text vs image model**: [core/config.py](core/config.py) — `get_text_model(ctx)` for chat model; `get_api_config(ctx)` returns `"model"` for LlmClient. Image model is `image_model` (used when `image_provider=endpoint`).
- **Image extraction**: `get_selected_image_base64(model, ctx=None)` in [core/image_tools.py](core/image_tools.py) — bridge for Img2Img. Pass `ctx` from the chat panel or MainJob for Calc.
- **Error logging**: `localwriter_debug.log`; UI shows errors if `createDialog` or image generation fails.
