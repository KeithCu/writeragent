import time

def original_method(paragraphs, max_chars=100):
    preview_parts = []
    for para_text in paragraphs:
        if para_text:
            preview_parts.append(para_text)
            if sum(len(p) for p in preview_parts) >= max_chars:
                break
    full_preview = " ".join(preview_parts)
    if len(full_preview) > max_chars:
        full_preview = full_preview[:max_chars] + "..."
    return full_preview

def optimized_method(paragraphs, max_chars=100):
    preview_parts = []
    current_length = 0
    for para_text in paragraphs:
        if para_text:
            preview_parts.append(para_text)
            current_length += len(para_text)
            if current_length >= max_chars:
                break
    full_preview = " ".join(preview_parts)
    if len(full_preview) > max_chars:
        full_preview = full_preview[:max_chars] + "..."
    return full_preview

# Generate paragraphs
paragraphs = ["a"] * 20000
max_chars = 20000

# warmup
original_method(paragraphs, max_chars)
optimized_method(paragraphs, max_chars)

start = time.time()
for _ in range(10):
    original_method(paragraphs, max_chars)
end = time.time()
print(f"Original: {end - start:.5f}s")

start = time.time()
for _ in range(10):
    optimized_method(paragraphs, max_chars)
end = time.time()
print(f"Optimized: {end - start:.5f}s")
