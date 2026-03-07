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
        from plugin.framework.config import get_api_config, get_config, user_config_dir
        from plugin.modules.http.client import LlmClient
        from plugin.framework.smol_model import LocalWriterSmolModel
        from plugin.contrib.smolagents.agents import ToolCallingAgent
        from plugin.contrib.smolagents.default_tools import DuckDuckGoSearchTool, VisitWebpageTool
        from plugin.contrib.smolagents.memory import ActionStep, FinalAnswerStep, ToolCall

        status_callback = getattr(ctx, "status_callback", None)
        append_thinking_callback = getattr(ctx, "append_thinking_callback", None)
        stop_checker = getattr(ctx, "stop_checker", None)

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
                if stop_checker and stop_checker():
                    return {"status": "error", "message": "Web search stopped by user."}
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
