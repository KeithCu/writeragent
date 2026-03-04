import json
from plugin.framework.tool_base import ToolBase

class WebResearchTool(ToolBase):
    name = "web_research"
    description = "Search the web to answer questions or find information."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query"
            },
            "history_text": {
                "type": "string",
                "description": "Previous conversation text for context"
            }
        },
        "required": ["query"]
    }
    doc_types = ["writer", "calc", "draw"]
    tier = "agent"
    is_mutation = False

    def execute(self, ctx, query, history_text=None):
        import os
        from urllib.parse import urlparse
        from plugin.modules.core.services.config import get_api_config, get_config, user_config_dir
        from plugin.framework.http import LlmClient
        from plugin.modules.core.smol_model import LocalWriterSmolModel
        from plugin.contrib.smolagents.agents import ToolCallingAgent
        from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
        from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)

        if history_text:
            # Truncate if extremely long, though the agent will handle it
            if len(history_text) > 4000:
                history_text = "..." + history_text[-4000:]

        try:
            if status_callback:
                status_callback("Sub-agent starting web search: " + query)

            config = get_api_config(ctx.ctx)
            max_tokens = int(config.get("chat_max_tokens", 2048))
            max_steps = int(config.get("search_web_max_steps", 20))

            udir = user_config_dir(ctx.ctx)
            raw_mb = int(get_config(ctx.ctx, "web_cache_max_mb", 50))
            cache_max_mb = 0 if raw_mb <= 0 else max(1, min(500, raw_mb))
            cache_path = os.path.join(udir, "localwriter_web_cache.db") if (udir and cache_max_mb > 0) else None

            smol_model = LocalWriterSmolModel(
                LlmClient(config, ctx.ctx), max_tokens=max_tokens,
                status_callback=status_callback,
            )

            instructions = "You are a research assistant. Use the conversation context provided below to resolve any ambiguity in the user's query."
            agent = ToolCallingAgent(
                tools=[
                    DuckDuckGoSearchTool(cache_path=cache_path, cache_max_mb=cache_max_mb),
                    VisitWebpageTool(cache_path=cache_path, cache_max_mb=cache_max_mb),
                ],
                model=smol_model,
                max_steps=max_steps,
                instructions=instructions,
            )

            task = f"### CONVERSATION HISTORY:\n{history_text or 'None'}\n\n### CURRENT QUERY:\n{query}"
            
            final_ans = None
            for step in agent.run(task, stream=True):
                if isinstance(step, ToolCall):
                    status_msg = ""
                    if step.name == "web_search":
                        q = str(step.arguments.get("query", "")) if isinstance(step.arguments, dict) else ""
                        if len(q) > 25: q = q[:22] + "..."
                        status_msg = f"Search: {q}"
                    elif step.name == "visit_webpage":
                        url = str(step.arguments.get("url", "")) if isinstance(step.arguments, dict) else ""
                        domain = urlparse(url).netloc or url[:30]
                        if domain.startswith("www."):
                            domain = domain[4:]
                        status_msg = f"Read: {domain}"
                    else:
                        status_msg = str(step.name)

                    if status_callback and status_msg:
                        status_callback(f"{status_msg}...")

                elif isinstance(step, ActionStep):
                    if append_thinking_callback:
                        msg = f"Step {step.step_number}:\n"
                        if step.model_output:
                            msg += f"{step.model_output.strip()}\n"
                        elif getattr(step, "model_output_message", None) and step.model_output_message.content:
                            msg += f"{str(step.model_output_message.content).strip()}\n"

                        if step.tool_calls:
                            for tc in step.tool_calls:
                                msg += f"Running tool: {tc.name} with {tc.arguments}\n"

                        if step.observations:
                            msg += f"Observation: {str(step.observations).strip()}\n"

                        append_thinking_callback(msg + "\n")
                elif isinstance(step, FinalAnswerStep):
                    final_ans = step.output

            return {"status": "ok", "message": f'searched for "{query}"', "result": str(final_ans)}
        except Exception as e:
            return {"status": "error", "message": f"Web search failed: {str(e)}"}

class GenerateImageTool(ToolBase):
    name = "generate_image"
    description = "Generate an image from a text prompt and insert it."
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Descriptive prompt for image generation"
            },
            "aspect_ratio": {
                "type": "string",
                "enum": ["square", "landscape_16_9", "portrait_9_16", "landscape_3_2", "portrait_2_3", "1:1", "4:3", "3:4", "16:9", "9:16"],
                "default": "square"
            },
            "base_size": {
                "type": "integer",
                "description": "Base dimension for scaling",
                "default": 512
            },
            "width": {"type": "integer", "description": "Override calculated width"},
            "height": {"type": "integer", "description": "Override calculated height"},
            "provider": {"type": "string", "description": "Override default provider"}
        },
        "required": ["prompt"]
    }
    doc_types = ["writer", "calc", "draw"]
    tier = "agent"
    is_mutation = True

    def execute(self, ctx, prompt, **args):
        from plugin.framework.image_utils import ImageService
        from plugin.modules.core.services.config import get_config_dict, as_bool, get_text_model, update_lru_history
        from plugin.modules.core.image_tools import insert_image
        
        status_callback = getattr(ctx, "status_callback", None)
        config = get_config_dict(ctx.ctx)
        service = ImageService(ctx.ctx, config)

        provider = args.get("provider", config.get("image_provider", "aihorde"))
        add_to_gallery = as_bool(config.get("image_auto_gallery", True))
        add_frame = as_bool(config.get("image_insert_frame", False))
        
        base_size = args.get("base_size", config.get("image_base_size", 512))
        try:
            base_size = int(base_size)
        except (ValueError, TypeError):
            base_size = 512

        aspect = args.get("aspect_ratio", config.get("image_default_aspect", "square"))
        if aspect in ("landscape_16_9", "16:9"):
            w, h = int(base_size * 16 / 9), base_size
        elif aspect in ("portrait_9_16", "9:16"):
            w, h = base_size, int(base_size * 16 / 9)
        elif aspect in ("landscape_3_2", "4:3"): # Legacy maps 4:3 to 1.5 roughly or uses exact? document_tools used 1.5 for 3:2
            w, h = int(base_size * 1.5), base_size
        elif aspect in ("portrait_2_3", "3:4"):
            w, h = base_size, int(base_size * 1.5)
        else:
            w, h = base_size, base_size

        w = (w // 64) * 64
        h = (h // 64) * 64

        width = args.get("width", w)
        height = args.get("height", h)
        image_model_override = args.get("image_model")

        try:
            # We filter args to pass only what ImageService expects
            args_copy = {k: v for k, v in args.items() if k not in ("prompt", "base_size", "aspect_ratio", "width", "height")}
            result = service.generate_image(prompt, provider_name=provider, width=width,
                                            height=height, status_callback=status_callback,
                                            model=image_model_override, **args_copy)
            
            if isinstance(result, tuple) and len(result) == 2:
                paths, error_msg = result
            else:
                paths = result
                error_msg = "No image returned."

            if not paths:
                return {"status": "error", "message": error_msg}

            # Insert logic (Writer, Calc, Draw specific or generic)
            # For now, consistent with original core tools
            insert_image(ctx.ctx, ctx.doc, paths[0], width, height, title=prompt,
                         description="Generated by %s" % provider,
                         add_to_gallery=add_to_gallery, add_frame=add_frame)
            
            # LRU updates
            if provider in ("endpoint", "openrouter"):
                image_model_used = args.get("image_model") or config.get("image_model") or get_text_model(ctx.ctx)
                if image_model_used:
                    endpoint = str(config.get("endpoint", "")).strip()
                    update_lru_history(ctx.ctx, image_model_used.strip(), "image_model_lru", endpoint)

            return {"status": "ok", "message": "Image generated and inserted from %s." % provider}
        except Exception as e:
            return {"status": "error", "message": str(e)}

class EditImageTool(ToolBase):
    name = "edit_image"
    description = "Modify an existing image based on a prompt."
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Instructions for editing"},
            "provider": {"type": "string", "description": "Override default provider"}
        },
        "required": ["prompt"]
    }
    doc_types = ["writer", "calc", "draw"]
    tier = "agent"
    is_mutation = True

    def execute(self, ctx, prompt, **args):
        from plugin.framework.image_utils import ImageService
        from plugin.modules.core.services.config import get_config_dict, as_bool, get_text_model, update_lru_history
        from plugin.modules.core.image_tools import get_selected_image_base64, replace_image_in_place, insert_image
        
        status_callback = getattr(ctx, "status_callback", None)
        source_b64 = get_selected_image_base64(ctx.doc, ctx=ctx.ctx)
        if not source_b64:
            return {"status": "error", "message": "No image selected. Please select an image in the document first."}

        config = get_config_dict(ctx.ctx)
        service = ImageService(ctx.ctx, config)
        provider = args.get("provider", config.get("image_provider", "aihorde"))
        add_to_gallery = as_bool(config.get("image_auto_gallery", True))
        add_frame = as_bool(config.get("image_insert_frame", False))

        try:
            result = service.generate_image(prompt, provider_name=provider,
                                            source_image=source_b64,
                                            status_callback=status_callback, **args)
            if isinstance(result, tuple) and len(result) == 2:
                paths, error_msg = result
            else:
                paths = result
                error_msg = "No image returned."

            if not paths:
                return {"status": "error", "message": error_msg}

            replaced = replace_image_in_place(ctx.ctx, ctx.doc, paths[0], 512, 512, title=prompt,
                                              description="Edited by %s" % provider,
                                              add_to_gallery=add_to_gallery, add_frame=add_frame)
            if not replaced:
                insert_image(ctx.ctx, ctx.doc, paths[0], 512, 512, title=prompt,
                             description="Edited by %s" % provider,
                             add_to_gallery=add_to_gallery, add_frame=add_frame)
            
            if provider in ("endpoint", "openrouter"):
                image_model_used = config.get("image_model") or get_text_model(ctx.ctx)
                if image_model_used:
                    endpoint = str(config.get("endpoint", "")).strip()
                    update_lru_history(ctx.ctx, image_model_used.strip(), "image_model_lru", endpoint)

            return {"status": "ok", "message": "Image edited and inserted from %s." % provider}
        except Exception as e:
            return {"status": "error", "message": str(e)}
