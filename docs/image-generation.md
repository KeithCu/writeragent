# WriterAgent Image Generation

Image generation and editing in WriterAgent uses the **same endpoint URL and API key as chat**; only the **image model** (`image_model`) differs from the text/chat model.

## Architecture

### Core image service

[`plugin/writer/images/image_utils.py`](../plugin/writer/images/image_utils.py):

- **`EndpointImageProvider`**: requests images via `LlmClient` (routing dedicated text-to-image models to OpenRouter's dedicated Image API via `POST /api/v1/images`, falling back to standard `modalities: ["image"]` chat completions for multimodal models).
- **`ImageService`**: merges config defaults (base size, steps) and delegates to `EndpointImageProvider`.

### Tools and document insertion

[`plugin/writer/images/images.py`](../plugin/writer/images/images.py) — `generate_image` tool (also via `delegate_to_specialized_*_toolset(domain="images")`):

- Text-to-image from a prompt.
- Img2img when `source_image='selection'` and an image is selected in the document.

[`plugin/writer/images/image_tools.py`](../plugin/writer/images/image_tools.py):

- **`insert_image`**: inserts into Writer/Calc; stable paths are linked, temp/cache paths are embedded.
- **`get_selected_image_base64`**: extracts selected image for img2img.
- **`add_image_to_gallery`**: optional Media Gallery add after generation.

## Model naming

| Key | Role |
|-----|------|
| `text_model` | Chat/text model (also exposed to `LlmClient` as `"model"` via `get_api_config()`). Writes use `set_text_model()`; recent ids per endpoint live in `model_lru@<endpoint>`. |
| `image_model` | Model id for image generation on the configured endpoint. Writes use `set_image_model()`. |
| `image_model_lru` | Recent image model ids for Settings and sidebar comboboxes. |

## Settings UI

**General tab** ([`SettingsDialog.xdl.tpl`](../extension/WriterAgentDialogs/SettingsDialog.xdl.tpl)): endpoint, API key, **Text/Chat Model**, **Image Model**, audio model, temperature, max tokens, additional instructions.

**Image Settings tab**: base size, aspect ratio, steps, seed, auto gallery, insert frame.

**Chat sidebar** ([`ChatPanelDialog.xdl`](../extension/WriterAgentDialogs/ChatPanelDialog.xdl)): text model and image model comboboxes; additional instructions come from config only (Settings).

## Config keys used by `generate_image`

| Config key | Role |
|------------|------|
| `image_model` | Image model on the chat endpoint (fallback: text model / provider defaults). |
| `image_base_size` | Default width/height base dimension. |
| `image_default_aspect` | Default aspect ratio for the tool. |
| `image_steps` | Steps passed to the endpoint when &gt; 0. |
| `image_auto_gallery` | Add generated images to Media Gallery. |
| `image_insert_frame` | Wrap inserted images in a frame. |
| `seed` | Reserved for future local generation backends. |

After a successful endpoint generation, the model used is pushed into `image_model_lru`.

## Img2img (edit selected image)

Single `generate_image(prompt, source_image=...)` API:

| Backend | How edit works |
|---------|----------------|
| **OpenRouter** | Multimodal user message: text prompt + source image as `image_url` data URL; same response parsing as create. |
| **OpenAI-compatible / Ollama / Together-style** | Same image endpoint as create; optional `source_image` / `image_url` in the request body where the shim supports it. |

Tool usage: pass `source_image='selection'` with an image selected in the document; optional `strength` (default 0.75) controls edit strength.

## Future Work

### OpenRouter Image Generation Enhancements
- **Support Additional Parameters**: Extend settings UI and model request payload to support OpenRouter image parameters such as `aspect_ratio` (e.g. 16:9, 1:1, etc.), `background` (auto/transparent/opaque), `output_format` (png/webp), and `output_compression`.
- **Image Model Metadata Checking**: Call `GET https://openrouter.ai/api/v1/images/models` (or filter `/api/v1/models` by `output_modalities=image`) dynamically to discover supported parameters (e.g., specific resolutions, aspect ratios) and populate/validate settings.

## Related docs

- Endpoint HTTP details: [`plugin/framework/client/llm_client.py`](../plugin/framework/client/llm_client.py)
- Sidebar / direct-image mode: [`docs/chat-sidebar-implementation.md`](chat-sidebar-implementation.md)
- **Planned local backends:** [diffusers-comfyui-image-backend-dev-plan.md](diffusers-comfyui-image-backend-dev-plan.md) — **ComfyUI** (new backend); local images via **Ollama/endpoint** already supported
