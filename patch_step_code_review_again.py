import re

def apply_diff():
    with open('plugin/writer/locale/harper.py', 'r') as f:
        lines = f.readlines()

    out = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 6. Reader thread re-reads self.proc each iteration
        if "def _read_loop(self, out_queue: queue.Queue[dict[str, Any] | None]) -> None:" in line:
            out.append(line)
            out.append(lines[i+1])
            out.append(lines[i+2])
            out.append(lines[i+3])
            out.append(lines[i+4])
            out.append("        proc = self.proc\n")
            out.append("        if proc is None:\n")
            out.append("            log.debug(\"[harper] Reader started with None process\")\n")
            out.append("            out_queue.put(None)\n")
            out.append("            return\n")
            out.append("        stdout = proc.stdout\n")
            out.append("        if stdout is None:\n")
            out.append("            log.debug(\"[harper] Reader started with None stdout\")\n")
            out.append("            out_queue.put(None)\n")
            out.append("            return\n")
            out.append("        try:\n")
            out.append("            while True:\n")
            out.append("                msg = json_rpc_framing.read_frame(cast(\"BinaryIO\", stdout))\n")
            out.append("                if msg is None:\n")
            out.append("                    break\n")
            out.append("                out_queue.put(msg)\n")
            out.append("        except Exception:\n")
            out.append("            log.exception(\"[harper] LSP reader failed\")\n")
            out.append("        finally:\n")
            out.append("            out_queue.put(None)\n")

            i += 5 # skip over """ and try:
            while i < len(lines) and "out_queue.put(None)" not in lines[i]:
                i += 1
            i += 1 # skip out_queue.put(None) inside finally
            continue

        # Fix #5: don't overwrite RESOLVING.
        if "_set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())" in line:
            out.append("            if _HARPER_STATE is not HarperRuntimeState.RESOLVING:\n")
            out.append("                _set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())\n")
            i += 1
            continue

        # Fix #3 splitlines discrepancies:
        if "import re\n" == line and "lines = re.split" in lines[i+1]:
            out.append("    import re\n")
            out.append("    lines = [text] if (\"\\n\" not in text and \"\\r\" not in text) else [m.group(0) for m in re.finditer(r\"[^\\r\\n]*(?:\\r\\n|\\r|\\n)?\", text)]\n")
            out.append("    if lines and not lines[-1]:\n")
            out.append("        lines.pop()\n")
            i += 2
            continue

        out.append(line)
        i += 1

    with open('plugin/writer/locale/harper.py', 'w') as f:
        f.writelines(out)

apply_diff()
