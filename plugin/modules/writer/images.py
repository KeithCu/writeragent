"""Writer image generation and editing tools."""

import logging

from plugin.framework.tool_base import ToolBase
from plugin.modules.core.image_tools import (
    insert_image, replace_image_in_place, get_selected_image_base64,
)

log = logging.getLogger("localwriter.writer")


class GenerateImage(ToolBase):
    """Generate an image from a text prompt and insert it."""

    name = "generate_image"
    intent = "media"
    description = (
        "Generate an image from a text prompt and insert it "
        "into the document."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The visual description of the image to generate.",
            },
            "width": {
                "type": "integer",
                "description": "Width in pixels (default 512).",
            },
            "height": {
                "type": "integer",
                "description": "Height in pixels (default 512).",
            },
        },
        "required": ["prompt"],
    }
    doc_types = ["writer", "calc", "draw", "impress"]
    is_mutation = True
    long_running = True

    def execute(self, ctx, **kwargs):
        prompt = kwargs.get("prompt")
        width = kwargs.get("width", 512)
        height = kwargs.get("height", 512)

        paths, error = ctx.services.ai.generate_image(
            prompt, width=width, height=height)

        if not paths:
            return {
                "status": "error",
                "message": error or "Generation failed: no image returned.",
            }

        insert_image(ctx.ctx, ctx.doc, paths[0], width, height, title=prompt)
        return {
            "status": "ok",
            "message": "Image generated and inserted.",
        }


class EditImage(ToolBase):
    """Edit the currently selected image using img2img."""

    name = "edit_image"
    intent = "media"
    description = (
        "Edit the selected image using a text prompt (Img2Img). "
        "If no image is selected, it will fail."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The visual description of the desired image version.",
            },
            "strength": {
                "type": "number",
                "description": "How much to change the image (0.0=none, 1.0=full). Default 0.6.",
            },
        },
        "required": ["prompt"],
    }
    doc_types = ["writer", "calc", "draw", "impress"]
    is_mutation = True
    long_running = True

    def execute(self, ctx, **kwargs):
        prompt = kwargs.get("prompt")
        strength = kwargs.get("strength", 0.6)

        source_b64 = get_selected_image_base64(ctx.doc, ctx.ctx)
        if not source_b64:
            return {
                "status": "error",
                "message": "No image selected. Please select an image first.",
            }

        paths, error = ctx.services.ai.generate_image(
            prompt, source_image=source_b64, strength=strength,
        )

        if not paths:
            return {
                "status": "error",
                "message": error or "Editing failed: no image returned.",
            }

        replaced = replace_image_in_place(
            ctx.ctx, ctx.doc, paths[0], 512, 512, title=prompt,
        )
        if not replaced:
            insert_image(ctx.ctx, ctx.doc, paths[0], 512, 512, title=prompt)

        return {
            "status": "ok",
            "message": "Image edited and inserted.",
        }
