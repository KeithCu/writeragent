"""AI module — unified AI provider registry and model catalog."""

import logging
from plugin.framework.module_base import ModuleBase

log = logging.getLogger("writeragent.ai")


class Module(ModuleBase):

    def initialize(self, services):
        from plugin.modules.ai.service import AiService, AiInstance
        ai = AiService()
        ai.set_config(services.config)
        services.register(ai)
        self._services = services
        self._providers = []

        cfg = services.config.proxy_for("ai")

        # 1. OpenAI-compatible Provider
        from .providers.openai import OpenAICompatProvider, EndpointImageProvider
        openai_provider = OpenAICompatProvider(cfg)
        self._providers.append(openai_provider)
        ai.register_instance("openai", AiInstance(
            name="OpenAI Endpoint", module_name="ai", provider=openai_provider,
            capabilities={"text", "tools"}
        ))
        
        # OpenAI Image Provider
        openai_img_provider = EndpointImageProvider(cfg)
        ai.register_instance("openai_image", AiInstance(
            name="OpenAI Image", module_name="ai", provider=openai_img_provider,
            capabilities={"image"}
        ))

        # 2. Ollama Provider
        from .providers.ollama import OllamaProvider
        ollama_provider = OllamaProvider(cfg)
        self._providers.append(ollama_provider)
        ai.register_instance("ollama", AiInstance(
            name="Ollama", module_name="ai", provider=ollama_provider,
            capabilities={"text", "tools"}
        ))

        # 3. AI Horde Provider
        from .providers.horde import HordeProvider
        horde_provider = HordeProvider(cfg)
        self._providers.append(horde_provider)
        ai.register_instance("horde", AiInstance(
            name="AI Horde", module_name="ai", provider=horde_provider,
            capabilities={"image"}
        ))

    def shutdown(self):
        for provider in getattr(self, "_providers", []):
            if hasattr(provider, "close"):
                provider.close()


def get_openai_model_options(services):
    """Options provider for the OpenAI-compatible model select widgets."""
    options = [{"value": "", "label": "(none)"}]
    ai = services.get("ai")
    if ai:
        catalog = ai.get_model_catalog(
            providers=["openai", "openrouter", "together", "mistral"])
        for m in sorted(catalog.get("text", []),
                        key=lambda x: x.get("priority", 0), reverse=True):
            options.append({
                "value": m["id"],
                "label": m.get("display_name", m["id"]),
            })
    return options


def get_ollama_model_options(services):
    """Options provider for the Ollama model select widgets."""
    options = [{"value": "", "label": "(none)"}]
    ai = services.get("ai")
    if ai:
        catalog = ai.get_model_catalog(providers=["ollama"])
        for m in sorted(catalog.get("text", []),
                        key=lambda x: x.get("priority", 0), reverse=True):
            options.append({
                "value": m["id"],
                "label": m.get("display_name", m["id"]),
            })
    return options
