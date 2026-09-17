# WriterAgent Image Generation

Image generation and editing in WriterAgent uses the **same endpoint URL and API key as chat**; only the **image model** (`image_model`) differs from the text/chat model.

## Architecture

### Core image service

[`plugin/writer/images/image_utils.py`](../../plugin/writer/images/image_utils.py):

- **`EndpointImageProvider`**: requests images via `LlmClient` (routing dedicated text-to-image models to OpenRouter's dedicated Image API via `POST /api/v1/images`, falling back to standard `modalities: ["image"]` chat completions for multimodal models).
- **OpenRouter `/images` payload** ([`OpenRouterShim.build_image_request`](../../plugin/framework/client/openai_shim.py)): `output_format` is `png` (not `webp` — models such as `black-forest-labs/flux.2-klein-4b` only accept png/jpeg). When width/height map to a standard ratio it sends `aspect_ratio` (`1:1`, `16:9`, …) plus `resolution` (`512` / `1K` / `2K` / `4K` from Base Size) and omits pixel `size` — OpenRouter 400s a paired `size` + `aspect_ratio` it considers mismatched, and Gemini image models ignore pixel `size`. Odd dimensions still send `size` (`WxH`) without `aspect_ratio`.
- **OpenRouter chat / `modalities: ["image"]` path** (Gemini and other multimodal image models): on **create**, adds `image_config.aspect_ratio` and `image_config.image_size` with low tier `0.5K` (not `512` — OpenRouter chat rejects `512`). On **edit** (`source_image` set), omits `image_config` so the reference image defines geometry (sidebar Base Size / Aspect Ratio are create-only). That path previously sent no size/aspect at all, so Square / 2048 in the sidebar never reached Gemini on create.
- **Grok / xAI**: `aspect_ratio` plus `resolution` (`1k` / `2k` from Base Size). No OpenAI `size`; `quality` is left at the vendor default (no Settings quality control).
- **Together**: integer `width` / `height` (Kontext: `aspect_ratio` only). The OpenAI `size` string is dropped — Together ignores it.
- **Google native**: Imagen `parameters.aspectRatio` + `imageSize` (`1K` / `2K`); Gemini `imageConfig.aspectRatio` + `imageSize` (`512` / `1K` / `2K` / `4K`).
- **`ImageService`**: merges config defaults (base size, steps) and delegates to `EndpointImageProvider`.

### Tools and document insertion

[`plugin/writer/images/images.py`](../../plugin/writer/images/images.py) — `image_generate` tool (also via `delegate_to_specialized_*_toolset(domain="images")`):

- Text-to-image from a prompt.
- Img2img when `source_image='selection'` and an image is selected in the document. Omitting `source_image` while a graphic is selected also edits in place (parent/specialist often drop the argument after rewriting an edit into a generate-new prompt).

**Sidebar Image mode** (`chat_mode = Image`, not Chat/specialist) calls `image_generate` directly — no chat LLM. With a document graphic selected, the send path passes `source_image='selection'` so the same img2img + in-place replace runs. With nothing selected, it generates and inserts a new graphic.

[`plugin/writer/images/image_tools.py`](../../plugin/writer/images/image_tools.py):

- **`image_insert`**: inserts into Writer, Calc, Draw, and Impress; stable paths are linked, temp/cache paths are embedded. Writer letterheads use `target=header`/`footer`; a different-first-page logo needs `first_is_shared=false` then `target=header_first`/`footer_first` (same `AS_CHARACTER` + `auto_height` path). Draw/Impress take millimetres (`page`, `x_mm`, `y_mm`; omitted x/y centers on the page).
- **Compound undo**: `insert_image`, `insert_image_at_locator`, `insert_image_into_header_footer`, and `replace_graphic_source` wrap document mutations in `WriterCompoundUndo` so one Ctrl+Z reverts the whole insert or edit (gallery writes stay outside the group).
- **`get_selected_image_base64`**: extracts selected image for img2img.
- **`add_image_to_gallery`**: optional Media Gallery add after generation.

## Model naming

| Key | Role |
|-----|------|
| `text_model` | Chat/text model (also exposed to `LlmClient` as `"model"` via `get_api_config()`). Writes use `set_text_model()`; recent ids per endpoint live in `model_lru@<endpoint>`. |
| `image_model` | Model id for image generation on the configured endpoint. Writes use `set_image_model()`. Catalog defaults (when unset): OpenRouter `google/gemini-3.1-flash-lite-image`; Together `black-forest-labs/FLUX.2-dev`. |
| `image_model_lru` | Recent image model ids for Settings and sidebar comboboxes. |

## Settings UI

**General tab** ([`SettingsDialog.xdl.tpl`](../../extension/WriterAgentDialogs/SettingsDialog.xdl.tpl)): endpoint, API key, **Text/Chat Model**, **Image Model**, audio model, temperature, max tokens, additional instructions.

**Image Settings tab**: base size, aspect ratio (same five labels as the sidebar Image-mode dropdown: Square, Landscape 16:9, Portrait 9:16, Landscape 3:2, Portrait 2:3), steps, seed, auto gallery, insert frame.

**Chat sidebar** ([`ChatPanelDialog.xdl`](../../extension/WriterAgentDialogs/ChatPanelDialog.xdl)): text model and image model comboboxes; additional instructions come from config only (Settings).

## Config keys used by `image_generate`

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

Single `generate_image(prompt, source_image=...)` API. Per-dropdown wire format, gaps, and coding notes: [Image edit by Settings endpoint](#image-edit-by-settings-endpoint).

| Backend | How edit works today |
|---------|----------------------|
| **OpenRouter** | Dedicated `/api/v1/images` (image-only models such as `black-forest-labs/flux.2-klein-4b`): `input_references` with a `image_url` data URL. Chat-completions multimodal path (`modalities: ["image"]`) still sends the source as a user `image_url` part. |
| **Together / Grok / Google Gemini image / Ollama** | Same `image_completion` / `build_image_request` path as create, with per-shim edit fields (table below). |
| **Other dropdown / custom OpenAI-compat** | Same path. Today we still send top-level `image_url`; research says **omit** it unless that server documents an edit field (see Remaining). |

Tool usage: pass `source_image='selection'` with an image selected in the document; optional `strength` (default 0.75) is accepted by the tool but **not sent on the wire**. If `source_image` is omitted and a graphic is selected, `image_generate` treats that as an in-place edit. Clear the selection to create a new image.

The **parent** main agent is steered in `SPECIALIZED_TASK_RULES` and `DELEGATE_SPECIALIZED_TASK_PARAM_HINT`: when the user wants to edit/change/restyle an existing or selected image, the `delegate_to_specialized_*` `task` must instruct `image_generate(source_image='selection')` and keep the user's wording. A generate-new paraphrase (“Generate an image of … dressed as a wizard”) is what the specialist then executes as create-new.

The images specialist is steered the same way: `images_specialized_sub_agent_hint()` plus `IMAGES_SPECIALIZED_EXAMPLES` (`writer:images` / `calc:images` / `draw:images`). Edit/change/restyle of an existing or selected image (e.g. “make it look like a wizard”) must call `image_generate` with `source_image='selection'` so img2img + `replace_image_in_place` keep the graphic in the same frame. A delete plus a prompt-only generate creates a new image instead of editing.

## Image edit by Settings endpoint

Follow-up after the OpenRouter img2img fix (`input_references` on `POST /api/v1/images`). The Settings **endpoint combobox** is `ENDPOINT_PRESETS` in [`plugin/framework/client/model_fetcher.py`](../../plugin/framework/client/model_fetcher.py). Together, Grok, Google Gemini image models, and Ollama now send the selected graphic on edit; remaining presets have no documented edit field on the routes we call (vendor research in the table and [Remaining](#remaining-coding-brief)).

### Did the OpenRouter fix change create?

**No.** [`OpenRouterShim.build_image_request`](../../plugin/framework/client/openai_shim.py) only adds `input_references` when `image_url` or `source_image` is set. A create call (prompt only) still sends `prompt`, `model`, `n`, `output_format: png`, and optional `size` — same as before. The chat-completions path (`modalities: ["image"]`) was not touched; it already attached the source as a user `image_url` part for non-image-only models.

The previous bug was OpenRouter-specific: a top-level `image_url` on `/api/v1/images` is ignored (HTTP 200, text-only prompt tokens), so Flux generated a new picture. Other shims were not part of that commit.

### How edit is routed today

`image_generate` still extracts selection base64 and calls `ImageService.generate_image(..., source_image=b64)`. Then [`EndpointImageProvider.generate`](../../plugin/writer/images/image_utils.py):

| Config | Path | Source image on the wire |
|--------|------|--------------------------|
| OpenRouter + image-only model (`is_image_only_model`) | `LlmClient.image_completion` → `OpenRouterShim` → `POST /api/v1/images` | **`input_references`** (fixed) |
| OpenRouter + multimodal image model | `make_chat_request` + `modalities: ["image"]` | User content: text + `image_url` data URL |
| Together / Grok / Google / Ollama | `image_completion` → that provider’s `build_image_request` | Together `reference_images` (Kontext: `image_url`); Grok `POST /images/edits`; Gemini `inlineData`; Ollama `images[]` |
| Official OpenAI (`api.openai.com`) | `image_completion` → `OpenAIShim` | Create: `/images/generations` (no source field). Edit: `/images/edits` JSON `images[].image_url`. `dall-e-3` raises. |
| Other dropdown / custom URL | `image_completion` → `OpenAIShim` | Top-level `image_url` today; vendors below mostly do **not** document that field |

`strength` is copied into `generate_image` kwargs and then ignored by every shim.

### Per-preset: what we send vs what the vendor wants

“Uses source?” means: if the user has a graphic selected, does the HTTP body include it in a field the vendor documents for img2img? Not “did we live-test this model.”

| Settings preset | Shim | Create (today) | Edit (today) | Uses source? | Vendor edit API (for later coding) |
|-----------------|------|----------------|--------------|--------------|-------------------------------------|
| **OpenRouter** | `OpenRouterShim` | `POST /api/v1/images` or chat `modalities: ["image"]` | Dedicated: `input_references: [{type: image_url, image_url: {url}}]`. Chat: user `image_url` part. | **Yes** (after the fix, for models that advertise references) | Already the documented field. Cap count from `GET /api/v1/images/models` (`supported_parameters.input_references`, e.g. flux.2-klein-4b is 0–4). |
| **Together AI** | `TogetherShim` | `POST …/images/generations` with integer **`width`/`height`** (not OpenAI `size`). Kontext: **`aspect_ratio`** instead. | Same URL. Non-Kontext: **`reference_images: [data URL]`** (no `image_url`). Model id contains `kontext`: top-level **`image_url`**. | **Yes** | [Together reference images](https://docs.together.ai/docs/inference/images/reference-images). Default `black-forest-labs/FLUX.2-dev` (and catalog option `google/flash-image-2.5`) is array-only. |
| **X.ai (Grok)** | `GrokShim` | `POST /v1/images/generations` (`aurora` default, `response_format: b64_json`, **`aspect_ratio`** + **`resolution`** `1k`/`2k`; no `size` / `quality`) | `POST /v1/images/edits` JSON `image: {url, type: image_url}` plus the same aspect/resolution. Does not send `image_url` on `/images/generations`. Keeps the caller’s `image_model`. | **Yes** | xAI [image editing](https://docs.x.ai/developers/model-capabilities/images/editing) (JSON, not OpenAI multipart). |
| **Google Gemini** | `GoogleShim` | Imagen: native `:predict` with `aspectRatio` + `imageSize` (`1K`/`2K`). Other (e.g. `gemini-*-flash-image`): `:generateContent` with `responseModalities: [IMAGE, TEXT]` and `imageConfig.aspectRatio` + `imageSize`. Text prompt only. | Gemini: `:generateContent` with an `inlineData` part (`mimeType` + raw base64) next to the text. **Imagen + source raises** (create-only; would otherwise replace the graphic with a new image). | **Yes** (Gemini image models) | Imagen `:predict` stays text-to-image. Dropdown URL `…/v1beta/openai` is stripped; the shim talks native REST. |
| **Local (Ollama)** | `OllamaShim` | `POST /api/generate` `{model, prompt, stream: false, width, height}` | Same URL plus **`images: [raw base64]`** (no data-URL prefix, no `image_url`). | **Yes** | [Ollama generate](https://docs.ollama.com/api/generate) `images` array. Width/height are top-level, not inside `options`. |
| **Local (LM Studio)** | `OpenAIShim` | `POST …/images/generations` + optional `image_url` | Top-level `image_url` | **No** (docs list no images API) | **None.** Official OpenAI-compat is `/v1/models`, `/v1/responses`, `/v1/chat/completions`, `/v1/embeddings`, `/v1/completions` only ([LM Studio OpenAI compat](https://lmstudio.ai/docs/developer/openai-compat)). No `/v1/images/generations` or `/v1/images/edits`. Chat `image_url` parts are vision **in** for VLMs, not image **output**. A loaded third-party plugin might add an images route, but first-party server does not. Create today is likely HTTP 4xx, not silent ignore. |
| **Mistral** | `OpenAIShim` | `POST …/images/generations` | Top-level `image_url` | **No** | **None on `/images/*`.** Image **output** is the built-in `image_generation` tool: Conversations (`tools: [{type: "image_generation"}]` + agent, then download via Files) ([Image Generation](https://docs.mistral.ai/studio/agents/agent-tools/image_generation)) and Chat Completions with the same tool type ([cookbook](https://docs.mistral.ai/cookbooks/mistral-connectors-05-connectors-in-completions)). Work UI can iterate on a previous image or an upload; that is product chat, not a REST img2img field. No `POST /v1/images/generations` or `/edits`. |
| **Groq** | `OpenAIShim` | same | `image_url` on generations | **No** | **None — vision/chat only.** [API reference](https://console.groq.com/docs/api-reference) lists Chat, Responses, Audio, Models, Batches, Files — no Images. [Vision](https://console.groq.com/docs/vision) and [Responses](https://console.groq.com/docs/responses-api) take image **input** and return **text**. Model cards (e.g. [Qwen 3.8 27B](https://console.groq.com/docs/model/qwen/qwen3.8-27b)): Input text+images, Output text. |
| **DeepSeek** | `OpenAIShim` | same | same | **No** | **None — vision/chat only.** Models are `deepseek-flash` / `deepseek-v4-pro` ([pricing](https://api-docs.deepseek.com/quick_start/pricing)). Flash accepts image **input** on Chat Completions (`content[].image_url`) ([Vision](https://api-docs.deepseek.com/guides/vision)); output is text. No `/images/generations` or `/images/edits`. |
| **Cerebras** | `OpenAIShim` | same | same | **No** | **None — vision/chat only.** [Image Inputs](https://inference-docs.cerebras.ai/capabilities/image-inputs): Chat Completions `image_url` data URI (PNG/JPEG). FAQ: “Can I generate images? **No**, only image input is supported. The model returns text only.” |
| **Perplexity** | `OpenAIShim` | same | same | **No** | **None — search/chat only.** Image **input** is Agent API attachments ([Image Attachments](https://docs.perplexity.ai/docs/agent-api/image-attachments)). `return_images: true` on Sonar Chat Completions returns **web search** image URLs, not generated pixels ([Returning Images](https://docs.perplexity.ai/docs/grounded-llm/chat-completions/media/returning-images)). No `/images/generations` or `/edits`. |
| **Anthropic** | `AnthropicShim` (inherits default image builder) | `POST …/images/generations` (Anthropic does not serve this) | Top-level `image_url` on that missing route | **No** | **None — vision/chat only.** [API overview](https://platform.claude.com/docs/en/api/overview): Messages, Batches, Token Counting, Models — no Images API. [Models overview](https://platform.claude.com/docs/en/about-claude/models/overview): “text and image **input**, text **output**.” [Vision](https://platform.claude.com/docs/en/build-with-claude/vision) is `image` content blocks (`base64` / `url` / `file_id`) → text. Capability flag is `image_input`, not image output. |
| **NVIDIA NIM** | `OpenAIShim` | `POST …/images/generations` | Top-level `image_url` | **No** on the dropdown host | **Two products.** Settings URL `https://integrate.api.nvidia.com/v1` is NVIDIA’s OpenAI-compat **LLM** catalog (chat/completions), not Visual GenAI — docs do not list `/images/*` there. Self-hosted [Visual Generative AI NIM](https://docs.nvidia.com/nim/visual-genai/latest/getting-started.html): `POST /v1/images/generations` (JSON create) and `POST /v1/images/edits` (**JSON**, not OpenAI multipart) with **`image`**: data URL string or array, plus model-specific `mode` / `preprocess_image` / `steps` / `seed`. Hosted catalog visual models are a third path: `POST https://ai.api.nvidia.com/v1/genai/{org}/{model}` with `prompt` + `image` + `mode` (`Image Generation` \| `Image Editing`) ([flux.2-klein-4b infer](https://docs.api.nvidia.com/nim/reference/black-forest-labs-flux_2-klein-4b-infer)). Top-level `image_url` is **not** the documented field on any of these. |
| **Z.ai** | `OpenAIShim` | `POST {base}/api/paas/v4/images/generations` | Top-level `image_url` | **No** (field not documented) | **Create only.** [Generate Image](https://docs.z.ai/api-reference/image/generate-image): `POST https://api.z.ai/api/paas/v4/images/generations`, `Content-Type: application/json`, body `model` (`glm-image` \| `cogview-4-250304`), `prompt`, optional `quality`, `size`, `user_id`. Response `data[].url`. No `image_url` / `image` / `images` / `/images/edits`. [GLM-Image](https://docs.z.ai/guides/image/glm-image) input modality is **text**. GLM-4.xV is vision-in via `/chat/completions`, not img2img. Same silent-ignore risk as OpenRouter’s old `image_url` if we keep sending it. |

Custom typed URLs (not a preset label) use the same detection as chat (`get_provider_from_endpoint` / `PROVIDERS` in [`auth.py`](../../plugin/framework/client/auth.py)) and fall through to `OpenAIShim` unless the host matches ollama / openrouter / xai / google / anthropic.

OpenAI’s own host is in `PROVIDERS` and Quick Setup but **not** in the Settings dropdown. **`OpenAIShim` when provider is `openai`:** create stays `POST /v1/images/generations` JSON (`prompt`, `model`, `n`, `size`, `response_format: b64_json` — no `image_url` / `images` / `steps`). Edit is `POST /v1/images/edits` JSON `{model, prompt, n, size, response_format, images: [{image_url: "<data URL>"}]}` ([Create image edit](https://developers.openai.com/api/reference/resources/images/methods/edit)). `dall-e-3` + a source image **raises** (generations-only). Other OpenAI-compat hosts still use generic top-level `image_url` on generations. Multipart `image=@file`, `mask`, and `input_fidelity` are not sent.

### Same failure mode as OpenRouter

The OpenRouter bug was: **HTTP 200 + a new image**, because the field name was wrong. Together / Grok / Gemini image / Ollama / official OpenAI now send a documented edit field (Imagen and `dall-e-3` raise instead of generating). That pattern is still possible wherever we send top-level `image_url` on `/images/generations` and the server ignores unknown JSON keys: **Z.ai** (create-only, documented fields are `model`/`prompt`/`quality`/`size`/`user_id`), **self-hosted Visual GenAI NIM** (edit field is `image`, not `image_url`), and any **custom OpenAI-compat** that implements create only.

LM Studio / Groq / DeepSeek / Cerebras / Perplexity / Anthropic / Mistral / NVIDIA `integrate.api.nvidia.com` typically **fail the request** (no `/images/*`) rather than silently creating. UI still runs `replace_image_in_place` on a successful create-shaped response, so a create-only server that **does** return an image will swap the selected graphic for an unrelated generation.

### Remaining (coding brief)

Vendor docs as of 2026-09-14. Not live-tested here. Do **not** add a new `ImageProvider` or a second HTTP client unless noted. Prefer: send a documented edit field, or **raise / skip edit** (Imagen pattern) instead of `image_url` on `/images/generations`.

| Provider | Action | Request a shim would send | Why |
|----------|--------|---------------------------|-----|
| **OpenAI** (`api.openai.com`, not in dropdown) | **Done** (`OpenAIShim` when `_get_provider() == "openai"`) | **Create:** `POST {api_path}/images/generations` JSON (`prompt`, `model`, `n`, `size`, `response_format: b64_json`). **Edit:** `POST {api_path}/images/edits` JSON `{model, prompt, n, size, response_format, images: [{image_url: "<data URL>"}]}`. `dall-e-3` + source raises. | Official Images API. Other hosts still use generic `image_url` on generations. Optional later: `mask`, `input_fidelity`. |
| **Z.ai** | **Skip** edit; **verify-then-maybe** create | **Create:** already the documented route — `POST /api/paas/v4/images/generations` JSON `{model: "glm-image"\|"cogview-4-250304", prompt, size?, quality?}`. **Edit:** do **not** send `image_url`. Either omit the source (create-only) or raise like Imagen when `source_image` is set. | [Generate Image](https://docs.z.ai/api-reference/image/generate-image) has no source-image field and no `/images/edits`. Sending `image_url` is the OpenRouter failure mode. |
| **NVIDIA NIM** (dropdown `integrate.api.nvidia.com/v1`) | **Skip** | Do not call `/images/generations` or `/images/edits` on this host. Chat/LLM catalog only. | Visual GenAI is a different product/URL. Hitting `/images/*` here is not documented. |
| **NVIDIA Visual GenAI NIM** (self-hosted, e.g. `http://localhost:8000/v1`) | **Implement** only if we detect this server (or user pastes that base); still `build_image_request`, no new `ImageProvider` | **Create:** `POST /v1/images/generations` JSON `{model, prompt, n, response_format: "b64_json", steps?, seed?}`. **Edit:** `POST /v1/images/edits` JSON `{model, prompt, image: "data:image/png;base64,…", n, response_format: "b64_json"}`. FLUX.2-klein-4B / Qwen-Image-Edit-2509+ accept `image` as an **array** of data URLs (we only have one selection). FLUX.1-dev / SD3.5 Large edits also want `mode: "canny"\|"depth"` and `preprocess_image: true` — without those, ControlNet variants are not img2img in the Kontext sense. Native `POST /v1/infer` (`artifacts[].base64`) is the same models, different envelope; OpenAI-compat is enough. | [Getting started](https://docs.nvidia.com/nim/visual-genai/latest/getting-started.html), [Image Editing API](https://docs.nvidia.com/nim/visual-genai/latest/api/openai-image-editing.html). Field is **`image`**, not `image_url`. |
| **NVIDIA hosted catalog** (`ai.api.nvidia.com/v1/genai/…`) | **Skip** (does not fit default `{endpoint}{api_path}/images/*`) | Would be `POST https://ai.api.nvidia.com/v1/genai/{org}/{model}` JSON `{prompt, mode: "Image Editing", image: ["data:image/png;base64,…"], width, height, steps, seed}`. Preview playground may restrict `image` to `data:image/png;example_id,N`. | Different path and envelope than `OpenAIShim`. Out of scope unless we special-case that host. |
| **LM Studio** | **Skip** | No images request. If we want a hard fail instead of a 404 create: raise when `image_model` is used against this preset. | [OpenAI compat table](https://lmstudio.ai/docs/developer/openai-compat) has no `/images/*`. Vision chat ≠ img2img. |
| **Mistral** | **Skip** | Do not POST `/images/generations`. A later connector (new complexity) would be Chat Completions or Conversations with `tools: [{type: "image_generation"}]`, then download the file/URL from the tool result — still **create**, not a documented REST edit field. | Agents/Conversations tool, not Images API. |
| **Groq** | **Skip** | No `/images/*`. | Vision-in / text-out only. |
| **DeepSeek** | **Skip** | No `/images/*`. | Vision-in / text-out only. |
| **Cerebras** | **Skip** | No `/images/*`. | Docs: cannot generate images. |
| **Perplexity** | **Skip** | No `/images/*`. Do not treat `return_images` as generation. | Search image URLs, not model output. |
| **Anthropic** | **Skip** | Do not POST `/images/generations`. Messages vision remains chat-only. | No first-party image generate/edit in 2026 docs. |
| **Custom OpenAI-compat** (LM Studio already covered; llama.cpp, vLLM, ComfyUI proxies, openedai-images-flux, …) | **Safe default: create only** | **Create:** `POST /v1/images/generations` JSON `{prompt, model, n, size, response_format}` — **omit** `image_url` unless that server’s docs name it. **Edit:** only if the operator documents `/v1/images/edits` (then match **their** field: OpenAI JSON `images[]`, NVIDIA JSON `image`, multipart `image=@file`, A1111-style `denoising_strength`, etc.). If unknown: raise or create-without-replacing rather than send a guessed field. | Unknown servers often ignore extra JSON keys (**HTTP 200 + new image**) or 404 the path. llama.cpp / vLLM typically implement chat, not Images. Dedicated image proxies vary; do not assume OpenAI `image_url` on generations. |

#### `strength`

Tool default is `0.75`; **we send it nowhere today** (`generate_image` kwargs, then ignored). Emit later only where the **route we actually call** documents a denoising/strength field.

| Backend | Documented strength-like field on our route? |
|---------|-----------------------------------------------|
| OpenRouter `/api/v1/images` `input_references` | **No** |
| Together `/images/generations` `image_url` / `reference_images` | **No** (has `guidance_scale`, `steps` — prompt adherence / diffusion steps, not img2img strength) |
| xAI `/images/edits` | **No** |
| Google Gemini `:generateContent` / Imagen `:predict` | **No** |
| Ollama `/api/generate` `images[]` | **No** |
| OpenAI `/images/edits` | **No** — use `input_fidelity`: `high` \| `low` if we expose a similar control |
| Z.ai `/images/generations` | **No** (and no edit route) |
| NVIDIA Visual GenAI `/images/edits` | **No** `strength` — `cfg_scale`, `steps`, `seed`, and ControlNet `mode` |
| NVIDIA hosted `/genai/…` | **No** `strength` — `cfg_scale` (often pinned), `steps` |
| LM Studio / Groq / DeepSeek / Cerebras / Perplexity / Anthropic / Mistral | **N/A** (no image-output route) |
| Custom A1111 / some local diffusion proxies | **Sometimes** `strength` or `denoising_strength` (0–1) — only if that server’s docs say so; not the OpenAI Images schema |

## Future Work

### OpenRouter Image Generation Enhancements
- **Support Additional Parameters**: Settings UI and payload already send `aspect_ratio` / `resolution` (and chat `image_config.aspect_ratio` / `image_size`) derived from Aspect Ratio and Base Size. `quality` is left at the vendor default (no Settings quality control). Still unused: `background` (auto/transparent/opaque), `output_format` (png/webp besides the OpenRouter png default), and `output_compression`.
- **Image Model Metadata Checking**: Call `GET https://openrouter.ai/api/v1/images/models` (or filter `/api/v1/models` by `output_modalities=image`) dynamically to discover supported parameters (e.g., specific resolutions, aspect ratios) and populate/validate settings.

## Related docs

- Endpoint HTTP details: [`plugin/framework/client/llm_client.py`](../../plugin/framework/client/llm_client.py)
- Sidebar / direct-image mode: [`../chat/sidebar-implementation.md`](../chat/sidebar-implementation.md)
- **Planned local backends:** [diffusers-comfyui-dev-plan.md](diffusers-comfyui-dev-plan.md) — **ComfyUI** (new backend); local images via **Ollama/endpoint** already supported
