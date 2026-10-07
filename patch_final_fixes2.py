import re

with open('plugin/writer/locale/harper.py', 'r') as f:
    content = f.read()

# Fix #6: Reader thread re-reads self.proc
content = re.sub(
    r'(def _read_loop\(self, out_queue: queue\.Queue\[dict\[str, Any\] \| None\]\) -> None:\n\s+"""[^\n]*\n\n\s+[^\n]*\n\s+"""\n\s+try:\n\s+)while self\.proc and self\.proc\.stdout:',
    r'\1proc = self.proc\n        if proc is None:\n            log.debug("[harper] Reader started with None process")\n            out_queue.put(None)\n            return\n        stdout = proc.stdout\n        if stdout is None:\n            log.debug("[harper] Reader started with None stdout")\n            out_queue.put(None)\n            return\n        while True:',
    content
)
content = re.sub(
    r'(msg = json_rpc_framing\.read_frame\(cast\("BinaryIO", )self\.proc\.stdout(\)\))',
    r'\1stdout\2',
    content
)

# Fix #5: don't overwrite RESOLVING.
content = re.sub(
    r'(with _HARPER_LOCK:\n\s+)(_set_state\(HarperRuntimeState\.FAILED, failed_at=time\.monotonic\(\)\))',
    r'\1if _HARPER_STATE is not HarperRuntimeState.RESOLVING:\n                \2',
    content
)

# Fix #3 splitlines discrepancies:
content = re.sub(
    r'import re\n\s+lines = re\.split\(r"\(\?<=\\n\)\|\(\?<=\\r\)\(\?=\[\^\\n\]\)", text\) if text else \[\]',
    r'import re\n    lines = [m.group(0) for m in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n)?", text)]\n    if lines and not lines[-1]:\n        lines.pop()',
    content
)

with open('plugin/writer/locale/harper.py', 'w') as f:
    f.write(content)
