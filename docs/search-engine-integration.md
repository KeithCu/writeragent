# Search Engine Integration Guide

** Not implemented yet, just a plan **
**For Developers: Extending WriterAgent with Custom Search Engines**

This document covers how to configure and extend WriterAgent's search capabilities using JSON-based engine definitions. Designed for developers who want to integrate private/enterprise search engines or customize existing ones.

---

## Table of Contents

1. [Overview](#overview)
2. [Supported Engine Types](#supported-engine-types)
   - [DDG (DuckDuckGo)](#ddg-duckduckgo)
   - [HTTP (Custom REST/GraphQL APIs)](#http-custom-restgraphql-apis)
   - [Elasticsearch](#elasticsearch)
   - [Chrome DevTools Protocol (CDP)](#chrome-devtools-protocol-cdp)
3. [Configuration Schema](#configuration-schema)
4. [Implementation Details](#implementation-details)
   - [Engine Dispatcher](#engine-dispatcher)
   - [Templating System](#templating-system)
   - [Validation](#validation)
5. [Adding a New Engine Type](#adding-a-new-engine-type)
6. [Security Considerations](#security-considerations)
7. [Performance Optimization](#performance-optimization)
8. [Troubleshooting](#troubleshooting)

---

## Overview

WriterAgent's search system is designed to be **extensible without requiring code changes**. Instead of writing Python plugins, you define search engines in JSON and let the built-in dispatcher handle execution. This approach:

- **Eliminates security risks** (no arbitrary code execution)
- **Simplifies maintenance** (all logic lives in the core plugin)
- **Enables rapid integration** (just edit a JSON file)

---

## Supported Engine Types

### DDG (DuckDuckGo)

**Protocol**: HTTP GET with query parameters
**Use Case**: Public web search (no authentication required)
**Configuration**:
```json
{
  "type": "ddg",
  "enabled": true
}
```

**Implementation Notes**:
- Uses DuckDuckGo's Lite API (`https://api.duckduckgo.com`)
- Returns results in JSON format
- No rate limiting enforced by DuckDuckGo (but be polite)

**Example Response**:
```json
{
  "Abstract": "Summary text",
  "Results": [
    {"Title": "...", "URL": "...", "Text": "..."}
  ]
}
```

---

### HTTP (Custom REST/GraphQL APIs)

**Protocol**: HTTP/HTTPS (GET/POST) with optional authentication
**Use Case**: Private APIs, enterprise search, or any RESTful service

**Configuration**:
```json
{
  "type": "http",
  "enabled": true,
  "endpoint": "https://api.example.com/search",
  "method": "POST",
  "headers": {
    "Authorization": "Bearer {API_KEY}",
    "Content-Type": "application/json"
  },
  "body": {
    "query": "{QUERY}",
    "limit": 5,
    "filters": {"date": "week"}
  },
  "response_path": "results.items"
}
```

**Key Features**:
- **Templating**: `{QUERY}` and `{API_KEY}` are replaced at runtime
- **JSONPath**: Extract results from nested responses using `response_path`
- **Flexible Auth**: Supports API keys, basic auth, or no auth

**Supported Methods**:
- `GET`: Query parameters appended to URL
- `POST`: JSON body sent as-is

**Example Workflow**:
1. Replace `{QUERY}` with user's search term
2. Replace `{API_KEY}` with stored credential
3. Send request to `endpoint`
4. Extract results using `response_path` (JSONPath syntax)

**Common Pitfalls**:
- Forgetting to URL-encode GET parameters
- Misconfiguring `response_path` (use [JSONPath Online Evaluator](https://jsonpath.com) to test)
- Not handling pagination (consider adding `max_results` to config)

---

### Elasticsearch

**Protocol**: Elasticsearch Query DSL (REST over HTTP)
**Use Case**: Enterprise search, document repositories

**Configuration**:
```json
{
  "type": "elasticsearch",
  "enabled": true,
  "endpoint": "https://es.example.com",
  "index": "documents",
  "auth": {
    "type": "basic",
    "user": "admin",
    "pass": "{API_KEY}"
  },
  "query": {
    "match": {
      "content": "{QUERY}"
    }
  },
  "size": 10
}
```

**Implementation Details**:
- Uses Elasticsearch's `_search` endpoint
- Supports all Query DSL features (match, bool, range, etc.)
- Authentication via HTTP Basic Auth or API keys

**Example Request**:
```json
POST /documents/_search
{
  "query": {
    "match": {
      "content": "user query"
    }
  },
  "size": 10
}
```

**Performance Tips**:
- Use `fields` instead of `_source` to reduce payload size
- Add `timeout` to prevent long-running queries
- Consider `search_after` for deep pagination

---

## Configuration Schema

The schema is defined in `search_schema.json` and validates all engine configurations:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "WriterAgent Search Engine Config",
  "type": "object",
  "properties": {
    "engines": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "properties": {
          "type": {
            "type": "string",
            "enum": ["ddg", "http", "elasticsearch", "chrome_devtools"]
          },
          "enabled": {"type": "boolean"},
          "endpoint": {"type": "string"},
          "method": {"type": "string", "enum": ["GET", "POST"]},
          "headers": {"type": "object"},
          "body": {"type": "object"},
          "response_path": {"type": "string"},
          "auth": {
            "type": "object",
            "properties": {
              "type": {"type": "string", "enum": ["none", "api_key", "basic"]},
              "key": {"type": "string"},
              "user": {"type": "string"},
              "pass": {"type": "string"}
            },
            "required": ["type"]
          },
          "chrome_devtools": {
            "type": "object",
            "properties": {
              "port": {"type": "integer"},
              "wait_for_selector": {"type": "string"},
              "timeout": {"type": "integer"}
            },
            "required": ["port"]
          }
        },
        "required": ["type"]
      }
    }
  }
}
```

---

## Implementation Details

### Engine Dispatcher

The dispatcher routes searches to the appropriate engine based on the `type` field:

```python
# plugin/modules/chatbot/search.py
class SearchEngine:
    def __init__(self, engine_config):
        self.config = engine_config

    def search(self, query):
        engine_type = self.config["type"]
        if engine_type == "ddg":
            return self._ddg_search(query)
        elif engine_type == "http":
            return self._http_search(query)
        elif engine_type == "elasticsearch":
            return self._elasticsearch_search(query)
        elif engine_type == "chrome_devtools":
            return self._chrome_devtools_search(query)
        else:
            raise ValueError(f"Unknown engine: {engine_type}")

    def _http_search(self, query):
        import requests
        import jsonpath_ng

        # Replace templates
        headers = self._replace_templates(self.config.get("headers", {}), query)
        body = self._replace_templates(self.config.get("body", {}), query)
        endpoint = self._replace_templates(self.config["endpoint"], query)

        # Send request
        response = requests.request(
            method=self.config.get("method", "GET"),
            url=endpoint,
            headers=headers,
            json=body if self.config.get("method") == "POST" else None
        )
        response.raise_for_status()

        # Extract results
        data = response.json()
        if "response_path" in self.config:
            expr = jsonpath_ng.parse(self.config["response_path"])
            return [match.value for match in expr.find(data)]
        return data

    def _replace_templates(self, data, query):
        if isinstance(data, dict):
            return {k: self._replace_templates(v, query) for k, v in data.items()}
        elif isinstance(data, str):
            return data.replace("{QUERY}", query).replace("{API_KEY}", self._get_api_key())
        return data

    def _get_api_key(self):
        # Retrieve from secure storage
        pass
```

### Templating System

The templating system replaces placeholders in the config:

- `{QUERY}`: User's search term
- `{API_KEY}`: Stored API key (retrieved securely)

**Example**:
```json
{
  "body": {
    "query": "{QUERY}",
    "api_key": "{API_KEY}"
  }
}
```

**Security Note**: Always validate the final URL/body after templating to prevent injection.

### Validation

All configs are validated against the schema using `jsonschema`:

```python
from jsonschema import validate

with open("search_schema.json") as f:
    schema = json.load(f)

validate(instance=config, schema=schema)
```

---

## Adding a New Engine Type

To add a new engine type (e.g., Meilisearch):

1. **Update the Schema**:
   - Add the new type to the `enum` in `search_schema.json`
   - Define required properties

2. **Implement the Engine**:
   - Add a new method to `SearchEngine` (e.g., `_meilisearch_search`)
   - Handle authentication, requests, and response parsing

3. **Document It**:
   - Add a section to this doc
   - Include a config example

**Example: Meilisearch Engine**

```python
def _meilisearch_search(self, query):
    import requests

    url = f"{self.config['endpoint']}/indexes/{self.config['index']}/search"
    headers = {"Authorization": f"Bearer {self._get_api_key()}"}
    payload = {
        "q": query,
        "limit": self.config.get("limit", 10)
    }
    
    response = requests.post(url, json=payload, headers=headers)
    response.raise_for_status()
    return response.json()["hits"]
```

---

## Security Considerations

1. **Input Sanitization**:
   - Strip `{}` from user queries to prevent template injection
   - Validate URLs to prevent SSRF

2. **Authentication**:
   - Store API keys securely (use LibreOffice's credential manager or OS keychain)
   - Never log credentials

3. **Rate Limiting**:
   - Add `max_requests_per_minute` to configs
   - Implement exponential backoff for retries

4. **Validation**:
   - Use `jsonschema` to reject invalid configs
   - Validate responses before processing

---

## Performance Optimization

1. **Caching**:
   - Cache results for repeated queries
   - Use `cache_ttl` in config to control expiration

2. **Batching**:
   - Group multiple queries into one request (if API supports it)

3. **Timeouts**:
   - Add `timeout` to configs (default: 10s)

4. **Parallel Requests**:
   - For multi-engine searches, use `asyncio.gather`

---

## Troubleshooting

### Common Issues

1. **"Engine not found"**:
   - Check `type` in config matches a supported engine
   - Verify `search_config.json` is valid JSON

2. **"Invalid response_path"**:
   - Test your JSONPath using [JSONPath Online Evaluator](https://jsonpath.com)
   - Ensure the path matches the API response structure

3. **"Connection refused" (Chrome DevTools)**:
   - Verify Chrome is running with `--remote-debugging-port=9222`
   - Check the port number in config

4. **"401 Unauthorized"**:
   - Verify API key is correct
   - Check auth type (basic vs. bearer)

### Debugging Tips

- Enable debug logging in `writeragent.json`:
  ```json
  {
    "logging": {
      "search": "debug"
    }
  }
  ```
- Check `writeragent_debug.log` for request/response details

---

## References

- [DuckDuckGo API Docs](https://duckduckgo.com/api)
- [Elasticsearch Query DSL](https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl.html)
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)
- [JSONPath Syntax](https://goessner.net/articles/JsonPath/)

---

## Conclusion

WriterAgent's search system is designed for **flexibility without complexity**. By using JSON configs and a built-in dispatcher, you can integrate almost any search engine without writing Python code. This approach keeps the plugin **secure, maintainable, and extensible** while empowering users to customize their experience.

For questions or contributions, open an issue on [GitHub](https://github.com/KeithCu/writeragent).