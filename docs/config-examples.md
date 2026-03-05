# Configuration Examples for LocalWriter

## OpenWebUI (Local)

Copy this to your LibreOffice config folder as `localwriter.json`:

```json
{
    "endpoint": "http://localhost:3000",
    "model": "llama2",
    "api_key": "",
    "api_type": "chat",
    "is_openwebui": true,
    "openai_compatibility": false,
    "extend_selection_max_tokens": 70,
    "extend_selection_system_prompt": "",
    "edit_selection_max_new_tokens": 0,
    "edit_selection_system_prompt": ""
}
```

## Ollama (Local)

```json
{
    "endpoint": "http://localhost:11434",
    "model": "llama2",
    "api_key": "",
    "api_type": "chat",
    "is_openwebui": false,
    "openai_compatibility": false,
    "extend_selection_max_tokens": 70,
    "extend_selection_system_prompt": "",
    "edit_selection_max_new_tokens": 0,
    "edit_selection_system_prompt": ""
}
```

## OpenAI

```json
{
    "endpoint": "https://api.openai.com",
    "model": "gpt-3.5-turbo",
    "api_key": "YOUR_API_KEY_HERE",
    "api_type": "chat",
    "is_openwebui": false,
    "openai_compatibility": true,
    "extend_selection_max_tokens": 100,
    "extend_selection_system_prompt": "",
    "edit_selection_max_new_tokens": 50,
    "edit_selection_system_prompt": ""
}
```

**⚠️ IMPORTANT:** Never commit your actual API keys to git!

## LM Studio (Local)

```json
{
    "endpoint": "http://localhost:1234",
    "model": "local-model",
    "api_key": "",
    "api_type": "chat",
    "is_openwebui": false,
    "openai_compatibility": false,
    "extend_selection_max_tokens": 70,
    "extend_selection_system_prompt": "",
    "edit_selection_max_new_tokens": 0,
    "edit_selection_system_prompt": ""
}
```

## Configuration File Location

On macOS, the configuration file is located at:
```
~/Library/Application Support/LibreOffice/4/user/localwriter.json
```

You can copy one of the examples above to this location, replacing `YOUR_API_KEY_HERE` with your actual API key if needed.

## Environment Variables (Alternative)

If you want to keep secrets out of the config file entirely, you could use environment variables. However, this would require modifying the extension code to read from environment variables as a fallback.
