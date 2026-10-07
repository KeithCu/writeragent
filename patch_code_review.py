import re

with open('plugin/writer/locale/harper.py', 'r') as f:
    content = f.read()

# Fix #6: Reader thread re-reads self.proc
# I noticed I accidentally reverted the `_read_loop` fix earlier when restoring from `git checkout`.
read_loop_pattern = r'(def _read_loop\(self, out_queue: queue\.Queue\[dict\[str, Any\] \| None\]\) -> None:\n\s+"""[^\n]*\n\n\s+[^\n]*\n\s+"""\n\s+try:\n\s+)while self\.proc and self\.proc\.stdout:\n\s+msg = json_rpc_framing\.read_frame\(cast\("BinaryIO", self\.proc\.stdout\)\)'
read_loop_repl = r'\1proc = self.proc\n        if proc is None:\n            log.debug("[harper] Reader started with None process")\n            out_queue.put(None)\n            return\n        stdout = proc.stdout\n        if stdout is None:\n            log.debug("[harper] Reader started with None stdout")\n            out_queue.put(None)\n            return\n        while True:\n            msg = json_rpc_framing.read_frame(cast("BinaryIO", stdout))'
content = re.sub(read_loop_pattern, read_loop_repl, content)

# Fix #5: don't overwrite RESOLVING.
# `with _HARPER_LOCK:\n            _set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())`
content = re.sub(
    r'(with _HARPER_LOCK:\n\s+)(_set_state\(HarperRuntimeState\.FAILED, failed_at=time\.monotonic\(\)\))',
    r'\1if _HARPER_STATE is not HarperRuntimeState.RESOLVING:\n                \2',
    content
)

# Fix #3 splitlines discrepancies:
# Instead of `re.split(r"(?<=\n)|(?<=\r)(?=[^\n])", text) if text else []`
# Let's use `lines = [text] if ("\n" not in text and "\r" not in text) else text.splitlines(keepends=True)` like original but adjusted for original finding "split only on \n/\r\n/\r" because splitlines splits on vertical tab etc.
# Wait, let's look at the exact split that Harpers LSP needs. `normalize_spaces_1to1` converts other spaces to ` ` and leaves `\r\n` untouched.
# So `splitlines` behaves correctly if we only want to split on \r, \n, \r\n and NOT other line break characters. `text.splitlines()` splits on \u2028 (Line Separator). But LSP doesn't split lines on \u2028.
# That's why "a\u2028b teh" fails.
content = re.sub(
    r'import re\n\s+lines = re\.split\(r"\(\?<=\\n\)\|\(\?<=\\r\)\(\?=\[\^\\n\]\)", text\) if text else \[\]',
    r'import re\n    lines = [m.group(0) for m in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n)?", text)]\n    if lines and not lines[-1]:\n        lines.pop()',
    content
)


with open('plugin/writer/locale/harper.py', 'w') as f:
    f.write(content)
