"""AI module — unified AI provider registry and model catalog."""

import logging
from plugin.framework.module_base import ModuleBase

log = logging.getLogger("localwriter.ai")


class Module(ModuleBase):

    def initialize(self, services):
        from plugin.modules.ai.service import AiService
        from plugin.modules.ai.dict_config_proxy import (
            DictConfigProxy, load_instances_json)
        from plugin.modules.ai.service import AiInstance

        ai = AiService()
        ai.set_config(services.config)
        services.register(ai)
        self._services = services
        self._providers = []

        cfg = services.config.proxy_for("ai")

        # 1. Initialize OpenAI-compatible providers
        from .providers.openai import OpenAICompatProvider
        instances = load_instances_json(cfg, "openai_instances")
        if instances:
            for inst_def in instances:
                # Merge instance-specific and global defaults
                data = {
                    "endpoint": inst_def.get("endpoint") or cfg.get("openai_endpoint"),
                    "model": inst_def.get("model") or cfg.get("openai_model"),
                    "api_key": inst_def.get("api_key") or "",
                    "temperature": cfg.get("ai_temperature"),
                    "max_tokens": cfg.get("ai_max_tokens"),
                }
                proxy = DictConfigProxy(data)
                provider = OpenAICompatProvider(proxy)
                self._providers.append(provider)
                
                name = inst_def.get("name", "default")
                instance_id = "ai_openai:%s" % name
                ai.register_instance(instance_id, AiInstance(
                    name=name, module_name="ai", provider=provider,
                    capabilities={"text", "tools"}
                ))
                
                if inst_def.get("image"):
                    from .providers.openai import EndpointImageProvider
                    img_provider = EndpointImageProvider(proxy)
                    ai.register_instance(instance_id + ":image", AiInstance(
                        name=name + " (image)", module_name="ai",
                        provider=img_provider, capabilities={"image"}
                    ))

        # 2. Initialize Ollama providers
        from .providers.ollama import OllamaProvider
        instances = load_instances_json(cfg, "ollama_instances")
        if instances:
            for inst_def in instances:
                data = {
                    "endpoint": inst_def.get("endpoint") or cfg.get("ollama_endpoint"),
                    "model": inst_def.get("model") or cfg.get("ollama_model"),
                    "temperature": cfg.get("ai_temperature"),
                    "max_tokens": cfg.get("ai_max_tokens"),
                }
                proxy = DictConfigProxy(data)
                provider = OllamaProvider(proxy)
                self._providers.append(provider)
                
                name = inst_def.get("name", "default")
                instance_id = "ai_ollama:%s" % name
                ai.register_instance(instance_id, AiInstance(
                    name=name, module_name="ai", provider=provider,
                    capabilities={"text", "tools"}
                ))

        # 3. Initialize AI Horde providers
        from .providers.horde import HordeProvider
        instances = load_instances_json(cfg, "horde_instances")
        if instances:
            for inst_def in instances:
                data = {
                    "api_key": inst_def.get("api_key") or cfg.get("horde_api_key"),
                    "model": inst_def.get("model") or "stable_diffusion",
                }
                proxy = DictConfigProxy(data)
                provider = HordeProvider(proxy)
                self._providers.append(provider)
                
                name = inst_def.get("name", "default")
                instance_id = "ai_horde:%s" % name
                ai.register_instance(instance_id, AiInstance(
                    name=name, module_name="ai", provider=provider,
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
