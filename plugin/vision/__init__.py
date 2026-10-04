# WriterAgent vision / OCR module (manifest-driven settings; helpers under plugin.vision).

from typing import Any

from plugin.framework.module_base import ModuleBase

# Templates run ``from writeragent.vision import run_vision`` in the user venv.
# AliasImporter maps that name to this package. Dropping the re-export
# (6b0be365d) made Calc and Writer image_name Run paths fail the import.
# Writer selection and extract_structure_from_image still call
# plugin.scripting.client.run_vision; this name is the venv dispatcher.
from .venv.vision import run_vision

__all__ = ["VisionModule", "run_vision"]


class VisionModule(ModuleBase):
    """Registers vision/OCR LLM tools and hosts vision.* settings."""

    def initialize(self, services: Any) -> None:
        from . import vision_tools

        services.tools.auto_discover(vision_tools)
