import inspect
import timeit

def foo(a, b, c=1, *args, **kwargs):
    pass

sig = inspect.signature(foo)

def original():
    expects_ctx = any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        for p in sig.parameters.values()
    ) or any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())

def optimized():
    expects_ctx = any(
        p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        for p in sig.parameters.values()
    )

if __name__ == "__main__":
    n = 1000000
    orig_time = timeit.timeit(original, number=n)
    opt_time = timeit.timeit(optimized, number=n)

    print(f"Original: {orig_time:.4f}s")
    print(f"Optimized: {opt_time:.4f}s")
    print(f"Improvement: {(orig_time - opt_time) / orig_time * 100:.2f}%")
