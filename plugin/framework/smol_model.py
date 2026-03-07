from plugin.contrib.smolagents.models import Model, ChatMessage, MessageRole


class DummyTokenUsage:
    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

class WriterAgentSmolModel(Model):
    """
    A wrapper that implements `smolagents.models.Model` by delegating 
    requests to WriterAgent's `LlmClient` (`core.api`).
    """
    def __init__(self, llm_client, max_tokens=1024, status_callback=None, **kwargs):
        super().__init__(**kwargs)
        self.api = llm_client
        self.max_tokens = max_tokens
        self.model_id = self.api.config.get("model", "localwriter/model")
        self._status_callback = status_callback

    def generate(self, messages, stop_sequences=None, response_format=None, tools_to_call_from=None, **kwargs):
        completion_kwargs = self._prepare_completion_kwargs(
            messages=messages,
            stop_sequences=stop_sequences,
            tools_to_call_from=tools_to_call_from,
            **kwargs,
        )
        
        msg_dicts = completion_kwargs.get("messages", [])
        tools = completion_kwargs.get("tools", None)
        
        # Push heartbeat so the UI drain loop stays active during this blocking call
        if self._status_callback:
            self._status_callback("Thinking...")

        # Make the request to WriterAgent's backend
        result = self.api.request_with_tools(msg_dicts, max_tokens=self.max_tokens, tools=tools)
        
        if self._status_callback:
            self._status_callback("Model responded, processing...")

        content = result.get("content") or ""
        tool_calls_dict = result.get("tool_calls")
        
        smol_tool_calls = []
        if tool_calls_dict:
            from plugin.contrib.smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction
            for tc in tool_calls_dict:
                func_data = tc.get("function", {})
                smol_tool_calls.append(
                    ChatMessageToolCall(
                        id=tc.get("id", "call_0"),
                        type=tc.get("type", "function"),
                        function=ChatMessageToolCallFunction(
                            name=func_data.get("name", ""),
                            arguments=func_data.get("arguments", "")
                        )
                    )
                )

        usage_dict = result.get("usage", {})
        if usage_dict:
            try:
                from plugin.contrib.smolagents.models import TokenUsage
                token_usage = TokenUsage(
                    input_tokens=usage_dict.get("prompt_tokens", 0),
                    output_tokens=usage_dict.get("completion_tokens", 0)
                )
            except ImportError:
                token_usage = DummyTokenUsage(
                    input_tokens=usage_dict.get("prompt_tokens", 0),
                    output_tokens=usage_dict.get("completion_tokens", 0)
                )
        else:
            token_usage = None

        msg = ChatMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=smol_tool_calls if smol_tool_calls else None
        )
        if token_usage:
            msg.token_usage = token_usage
        return msg

