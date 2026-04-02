import json

def truncate_large_data(messages):
    import copy
    copied = copy.deepcopy(messages)
    for m in copied:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    # For audio
                    if part.get("type") == "input_audio":
                        audio = part.get("input_audio", {})
                        if isinstance(audio, dict) and "data" in audio:
                            data_len = len(audio["data"])
                            audio["data"] = f"<audio base64 data truncated, length={data_len}>"
                    # For images
                    if part.get("type") == "image_url":
                        img = part.get("image_url", {})
                        if isinstance(img, dict) and "url" in img and isinstance(img["url"], str):
                            url = img["url"]
                            if url.startswith("data:image"):
                                img["url"] = f"<image base64 data truncated, length={len(url)}>"
    return copied

messages = [
  {
    "role": "user",
    "content": [
      {
        "type": "text",
        "text": "Transcribe this audio exactly. Output ONLY the transcript. No preamble, no markers."
      },
      {
        "type": "input_audio",
        "input_audio": {
          "data": "UklGRkLFAABXQVZFZm10IBAAAAABAAEAgD4AAAB9AAACABAAZGF0YR7FAAAAAAAAAAAAA"
        }
      }
    ]
  }
]

print(json.dumps(truncate_large_data(messages), indent=2))
