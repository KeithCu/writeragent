import timeit

def original(content, stop_sequences):
    if content is None or not stop_sequences:
        return content

    for stop_seq in stop_sequences:
        split = content.split(stop_seq)
        content = split[0]
    return content

def optimized(content, stop_sequences):
    if content is None or not stop_sequences:
        return content

    for stop_seq in stop_sequences:
        idx = content.find(stop_seq)
        if idx != -1:
            content = content[:idx]
    return content

content = "This is a very long string " * 1000 + "STOP" + " and more stuff " * 1000
stop_sequences = ["STOP", "END", "HALT"]

# Benchmark
orig_time = timeit.timeit("original(content, stop_sequences)", globals=globals(), number=10000)
opt_time = timeit.timeit("optimized(content, stop_sequences)", globals=globals(), number=10000)

print(f"Original: {orig_time:.5f}s")
print(f"Optimized: {opt_time:.5f}s")
print(f"Speedup: {orig_time/opt_time:.2f}x")
