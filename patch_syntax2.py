import re

with open('plugin/writer/locale/harper.py', 'r') as f:
    content = f.read()

# I see what I did wrong. The previous patch failed because `r"[^\r\n]*(?:\r\n|\r|\n)?"` matched the whole previous line incorrectly. Let's just write exactly the lines we want for lsp_range_to_offset.

def apply_diff():
    with open('plugin/writer/locale/harper.py', 'r') as f:
        lines = f.readlines()

    out = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 1/2. Blocked _write under _HARPER_LOCK deadlocks later Harper calls
        if "cancel_event.set()" in line and "handle.join" in lines[i+1]:
            out.append(line)
            out.append(lines[i+1])
            out.append("        if not done.is_set():\n")
            out.append("            try:\n")
            out.append("                client.close()\n")
            out.append("            except Exception:\n")
            out.append("                pass\n")
            i += 2
            continue

        # 3. Offsets mapped against wrong text
        if "def _diagnostics_to_errors(text: str, results: list[Any]) -> dict[str, Any]:" in line:
            out.append("def _diagnostics_to_errors(text: str, lint_text: str, results: list[Any]) -> dict[str, Any]:\n")
            i += 1
            continue

        if "start_offset = lsp_range_to_offset(text," in line:
            out.append(line.replace("text,", "lint_text,"))
            i += 1
            continue

        if "end_offset = lsp_range_to_offset(text," in line:
            out.append(line.replace("text,", "lint_text,"))
            i += 1
            continue

        if "out = _diagnostics_to_errors(text, results)" in line:
            out.append(line.replace("text, results", "text, lint_text, results"))
            i += 1
            continue

        # 3. lsp_range_to_offset splitlines
        if "lines = [text] if (\"\\n\" not in text and \"\\r\" not in text) else text.splitlines(keepends=True)" in line:
            out.append("    import re\n")
            out.append("    lines = [m.group(0) for m in re.finditer(r\"[^\\r\\n]*(?:\\r\\n|\\r|\\n)?\", text)]\n")
            out.append("    if lines and not lines[-1]:\n")
            out.append("        lines.pop()\n")
            i += 1
            continue

        # 4. Stale heartbeat callbacks leak across lints
        if "        if heartbeat_fn is not None:" in line and "            self._heartbeat_fn = heartbeat_fn" in lines[i+1]:
            out.append("        previous_heartbeat = self._heartbeat_fn\n")
            out.append(line)
            i += 1
            continue

        if "            self._lint_cancel = previous_cancel" in line:
            out.append(line)
            out.append("            self._heartbeat_fn = previous_heartbeat\n")
            i += 1
            continue

        # 5. Lint failure resets to IDLE and restarts
        if "        log.exception(\"[harper] lint failed on ready client; empty aErrors this walk\")" in line:
            out.append(line)
            i += 1
            continue
        if "            _set_state(HarperRuntimeState.IDLE)" in line and "empty aErrors this walk" in lines[i-3]:
            out.append("            if _HARPER_STATE is not HarperRuntimeState.RESOLVING:\n")
            out.append("                _set_state(HarperRuntimeState.FAILED, failed_at=time.monotonic())\n")
            i += 1
            continue

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
            i += 9 # skip until next
            while i < len(lines) and "json_rpc_framing.read_frame" not in lines[i]:
                i += 1
            i += 1
            continue

        # 7. _initialize can succeed against dead process
        if "self._send_request(\"initialize\", init_params, deadline=deadline)" in line:
            out.append("            init_res = " + line.lstrip())
            out.append("            if not init_res or \"error\" in init_res:\n")
            out.append("                raise RuntimeError(f\"harper-ls initialize failed: {init_res}\")\n")
            i += 1
            continue

        # 8. Cancelled codeAction makes lint return partial results
        if "res = self._send_request(\"textDocument/codeAction\"" in line:
            out.append(line)
            out.append("            if res is None:\n")
            out.append("                raise TimeoutError(\"codeAction cancelled or timed out\")\n")
            i += 1
            continue

        # 9. publishDiagnostics without version after version bump
        if "if msg_version is not None and msg_version < version:" in line:
            out.append(line)
            out.append("                    if msg_version is None and version > 0:\n")
            out.append("                        continue\n")
            i += 1
            continue

        # harper_try_lint return None quickly
        if "    if not _try_begin_harper_wait():" in line:
            out.append(line)
            out.append(lines[i+1])
            out.append("    if not _HARPER_LOCK.acquire(timeout=0.1):\n")
            out.append("        _end_harper_wait()\n")
            out.append("        return None\n")
            out.append("    _HARPER_LOCK.release()\n")
            i += 2
            continue

        out.append(line)
        i += 1

    with open('plugin/writer/locale/harper.py', 'w') as f:
        f.writelines(out)

apply_diff()
