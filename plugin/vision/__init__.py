# WriterAgent vision / OCR module (manifest-driven settings; helpers under plugin.vision).

from plugin.framework.module_base import ModuleBase


class VisionModule(ModuleBase):
    """Registers vision/OCR LLM tools and hosts vision.* settings."""

    def initialize(self, services):
        from . import vision_tools

        services.tools.auto_discover(vision_tools)
