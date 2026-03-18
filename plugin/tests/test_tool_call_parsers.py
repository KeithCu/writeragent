from plugin.contrib.tool_call_parsers import (
    get_parser,
    get_parser_for_model,
)

def test_hermes_parser():
    parser = get_parser("hermes")
    text = 'Hello\n<tool_call>{"name": "test_tool", "arguments": {"cmd": "ls"}}</tool_call>'
    content, tool_calls = parser.parse(text)
    
    assert content == "Hello"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "test_tool"

def test_hermes_parser_unclosed():
    parser = get_parser("hermes")
    text = '<tool_call>{"name": "test_tool", "arguments": {"cmd": "ls"}'
    content, tool_calls = parser.parse(text)
    
    # Hermes returns None on JSONDecodeError from unclosed JSON
    assert tool_calls is None

def test_hermes_parser_normalization():
    parser = get_parser("hermes")
    # provider emitting arguments as an object in-text
    text = '<tool_call>{"name": "test_tool", "arguments": {"cmd": "ls", "args": ["-l", "-a"]}}</tool_call>'
    content, tool_calls = parser.parse(text)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "test_tool"
    assert tool_calls[0]["function"]["arguments"] == '{"cmd": "ls", "args": ["-l", "-a"]}'

def test_hermes_parser_whitespace():
    parser = get_parser("hermes")
    # provider emitting whitespace or newlines inside and around the tags
    text = (
        'Hello\n'
        '<tool_call>  \n'
        '{"name": "test_tool", "arguments": {"cmd": "ls"}} \n'
        '  </tool_call>'
    )
    content, tool_calls = parser.parse(text)

    assert content == "Hello"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "test_tool"
    assert tool_calls[0]["function"]["arguments"] == '{"cmd": "ls"}'

def test_hermes_parser_multiple():
    parser = get_parser("hermes")
    text = (
        'Here are your calls:\n'
        '<tool_call>{"name": "tool1", "arguments": {"a": 1}}</tool_call>\n'
        '<tool_call>{"name": "tool2", "arguments": {"b": 2}}</tool_call>'
    )
    content, tool_calls = parser.parse(text)

    assert content == "Here are your calls:"
    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "tool1"
    assert tool_calls[0]["function"]["arguments"] == '{"a": 1}'
    assert tool_calls[1]["function"]["name"] == "tool2"
    assert tool_calls[1]["function"]["arguments"] == '{"b": 2}'

def test_deepseek_v3_parser():
    parser = get_parser("deepseek_v3")
    text = (
        'Thinking...\n'
        '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>get_weather\n'
        '```json\n{"city": "Paris"}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
    )
    content, tool_calls = parser.parse(text)
    
    assert content == "Thinking..."
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"

def test_deepseek_v3_parser_normalization():
    parser = get_parser("deepseek_v3")
    text = (
        '<｜tool▁calls▁begin｜><｜tool▁call▁begin｜>function<｜tool▁sep｜>get_weather\n'
        '```json\n{"city": "Paris", "days": [1, 2, 3]}\n```<｜tool▁call▁end｜><｜tool▁calls▁end｜>'
    )
    content, tool_calls = parser.parse(text)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    # DeepSeek just strips whitespace for args, if it's already a valid string, that's fine
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Paris", "days": [1, 2, 3]}'

def test_deepseek_v3_parser_whitespace():
    parser = get_parser("deepseek_v3")
    text = (
        'Thinking...\n'
        '<｜tool▁calls▁begin｜>   \n'
        '<｜tool▁call▁begin｜> function <｜tool▁sep｜> get_weather \n'
        '```json\n{"city": "Paris"}\n```<｜tool▁call▁end｜>  \n'
        '<｜tool▁calls▁end｜>'
    )
    content, tool_calls = parser.parse(text)

    assert content == "Thinking..."
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Paris"}'

def test_deepseek_v3_parser_multiple():
    parser = get_parser("deepseek_v3")
    text = (
        'Thinking...\n'
        '<｜tool▁calls▁begin｜>'
        '<｜tool▁call▁begin｜>function<｜tool▁sep｜>get_weather\n'
        '```json\n{"city": "Paris"}\n```<｜tool▁call▁end｜>'
        '<｜tool▁call▁begin｜>function<｜tool▁sep｜>calc\n'
        '```json\n{"expr": "1+1"}\n```<｜tool▁call▁end｜>'
        '<｜tool▁calls▁end｜>'
    )
    content, tool_calls = parser.parse(text)

    assert content == "Thinking..."
    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Paris"}'
    assert tool_calls[1]["function"]["name"] == "calc"
    assert tool_calls[1]["function"]["arguments"] == '{"expr": "1+1"}'

def test_mistral_parser_v11():
    parser = get_parser("mistral")
    text = 'Result[TOOL_CALLS]get_weather{"city": "Berlin"}'
    content, tool_calls = parser.parse(text)
    
    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"

def test_mistral_parser_normalization():
    parser = get_parser("mistral")
    # v11 format
    text = 'Result[TOOL_CALLS]get_weather{"city": "Berlin", "days": [1, 2]}'
    _, tool_calls = parser.parse(text)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Berlin", "days": [1, 2]}'

    # prev11 format
    text2 = 'Result[TOOL_CALLS] [{"name": "get_weather", "arguments": {"city": "Berlin", "days": [1, 2]}}]'
    _, tool_calls2 = parser.parse(text2)
    assert tool_calls2 is not None
    assert len(tool_calls2) == 1
    assert tool_calls2[0]["function"]["arguments"] == '{"city": "Berlin", "days": [1, 2]}'

def test_mistral_parser_whitespace():
    parser = get_parser("mistral")
    # v11 format with extra spaces
    text = 'Result[TOOL_CALLS]   get_weather   { "city" : "Berlin" }  '
    content, tool_calls = parser.parse(text)

    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_calls[0]["function"]["arguments"] == '{ "city" : "Berlin" }'

    # prev11 format with extra spaces/newlines
    text2 = 'Result \n [TOOL_CALLS] \n [ \n { "name" : "get_weather" , "arguments" : {"city": "Berlin"} } \n ] '
    content2, tool_calls2 = parser.parse(text2)
    assert content2 == "Result"
    assert tool_calls2 is not None
    assert len(tool_calls2) == 1
    assert tool_calls2[0]["function"]["name"] == "get_weather"
    assert tool_calls2[0]["function"]["arguments"] == '{"city": "Berlin"}'

def test_mistral_parser_v11_multiple():
    parser = get_parser("mistral")
    text = 'Result[TOOL_CALLS]get_weather{"city": "Berlin"}[TOOL_CALLS]calc{"expr": "2+2"}'
    content, tool_calls = parser.parse(text)

    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Berlin"}'
    assert tool_calls[1]["function"]["name"] == "calc"
    assert tool_calls[1]["function"]["arguments"] == '{"expr": "2+2"}'

def test_mistral_parser_prev11():
    parser = get_parser("mistral")
    text = 'Result[TOOL_CALLS] [{"name": "get_weather", "arguments": {"city": "Berlin"}}]'
    content, tool_calls = parser.parse(text)
    
    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "get_weather"

def test_mistral_parser_prev11_multiple():
    parser = get_parser("mistral")
    text = 'Result[TOOL_CALLS] [{"name": "get_weather", "arguments": {"city": "Berlin"}}, {"name": "calc", "arguments": {"expr": "2+2"}}]'
    content, tool_calls = parser.parse(text)

    assert content == "Result"
    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "get_weather"
    assert tool_calls[0]["function"]["arguments"] == '{"city": "Berlin"}'
    assert tool_calls[1]["function"]["name"] == "calc"
    assert tool_calls[1]["function"]["arguments"] == '{"expr": "2+2"}'

def test_llama_parser():
    parser = get_parser("llama3_json")
    text = 'Output: <|python_tag|>\n{"name": "calc", "arguments": {"expr": "2+2"}}'
    content, tool_calls = parser.parse(text)
    
    assert content == "Output:"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "calc"

def test_llama_parser_normalization():
    parser = get_parser("llama3_json")
    text = 'Output: <|python_tag|>\n{"name": "calc", "arguments": {"expr": "2+2", "flags": ["a"]}}'
    content, tool_calls = parser.parse(text)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "calc"
    assert tool_calls[0]["function"]["arguments"] == '{"expr": "2+2", "flags": ["a"]}'

def test_llama_parser_whitespace():
    parser = get_parser("llama3_json")
    text = (
        'Output: <|python_tag|>\n\n'
        '  {  \n'
        '  "name": "calc", \n'
        '  "arguments": {"expr": "2+2"} \n'
        '}  \n'
    )
    content, tool_calls = parser.parse(text)

    assert content == "Output:"
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "calc"
    assert tool_calls[0]["function"]["arguments"] == '{"expr": "2+2"}'

def test_llama_parser_multiple():
    parser = get_parser("llama3_json")
    text = 'Output: <|python_tag|>\n{"name": "calc", "arguments": {"expr": "2+2"}}\n{"name": "get_weather", "arguments": {"city": "Paris"}}'
    content, tool_calls = parser.parse(text)

    assert content == "Output:"
    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert tool_calls[0]["function"]["name"] == "calc"
    assert tool_calls[0]["function"]["arguments"] == '{"expr": "2+2"}'
    assert tool_calls[1]["function"]["name"] == "get_weather"
    assert tool_calls[1]["function"]["arguments"] == '{"city": "Paris"}'

def test_get_parser_for_model():
    p1 = get_parser_for_model("hermes-2-pro")
    assert p1 is not None
    # We can check it works instead of strict isinstance on the inner class
    _, tc = p1.parse('<tool_call>{"name": "test", "arguments": {}}</tool_call>')
    assert tc is not None
    
    p2 = get_parser_for_model("deepseek-coder-v3")
    assert p2 is not None
    
    p3 = get_parser_for_model("mistral-large")
    assert p3 is not None
    
    p4 = get_parser_for_model("llama-3-70b")
    assert p4 is not None
    
    assert get_parser_for_model("unknown") is None
