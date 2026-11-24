# SPDX-License-Identifier: GPL-3.0-or-later
"""Node-harness tests for editor_shell.js dirty tracking across async saves/runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SHELL_JS = Path(__file__).resolve().parents[2] / "plugin" / "contrib" / "scripting" / "assets" / "editor" / "editor_shell.js"
_SCRIPTS_MANAGER_JS = Path(__file__).resolve().parents[2] / "plugin" / "contrib" / "scripting" / "assets" / "editor" / "scripts_manager.js"


def _run_shell_scenario(script_body: str, *, include_scripts_manager: bool = False) -> dict:
    load_scripts_manager = (
        f'vm.runInContext(fs.readFileSync({json.dumps(str(_SCRIPTS_MANAGER_JS))}, "utf8"), ctx);'
        if include_scripts_manager
        else ""
    )
    driver = f"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync({json.dumps(str(_SHELL_JS))}, "utf8");
const els = {{}};
function mkClassList() {{
  const set = new Set();
  return {{
    add(c) {{ set.add(c); }},
    remove(c) {{ set.delete(c); }},
    contains(c) {{ return set.has(c); }},
    toggle(c, f) {{
      if (f === undefined) {{
        if (set.has(c)) {{ set.delete(c); return false; }}
        set.add(c); return true;
      }}
      if (f) set.add(c); else set.delete(c);
      return !!f;
    }},
  }};
}}
function dispatchListeners(list, ev) {{
  let stopped = false;
  ev ||= {{}};
  ev.stopPropagation ||= () => {{}};
  ev.stopImmediatePropagation ||= () => {{ stopped = true; }};
  ev.preventDefault ||= () => {{}};
  for (const item of list.filter((x) => x.cap).concat(list.filter((x) => !x.cap))) {{
    item.f(ev);
    if (stopped) break;
  }}
}}
function el(id) {{
  return (els[id] ||= {{
    id,
    value: "",
    checked: false,
    disabled: false,
    style: {{}},
    textContent: "",
    title: "",
    innerHTML: "",
    options: [],
    listeners: {{}},
    classList: mkClassList(),
    addEventListener(n, f, cap) {{ (this.listeners[n] ||= []).push({{ f, cap: !!cap }}); }},
    click() {{ dispatchListeners(this.listeners.click || [], {{ type: "click" }}); }},
    trigger(n, ev) {{ dispatchListeners(this.listeners[n] || [], ev || {{ type: n }}); }},
    setAttribute() {{}},
    appendChild(child) {{ this.options.push(child); }},
    querySelector: () => null,
    focus() {{}},
  }});
}}
const sent = [];
const queue = [];
let poll;
let text = "";
let onCh;
let readOnly = false;
function type(v) {{ if (!readOnly) {{ text = v; onCh && onCh(); }} }}
const backend = {{
  create: () => Promise.resolve(),
  onChange(f) {{ onCh = f; }},
  getValue: () => text,
  setValue(v) {{ text = v; onCh && onCh(); }},
  setReadOnly(on) {{ readOnly = !!on; }},
  setLanguage() {{}},
  setMarkers() {{}},
  setTheme() {{}},
}};
const win = {{
  WaEditorBackends: {{ monaco: backend }},
  WA_EDITOR_BACKEND: "monaco",
  addEventListener() {{}},
  pywebview: {{
    api: {{
      notify_dirty: (d) => sent.push(d),
      notify_save: () => sent.push("save"),
      notify_save_script: () => sent.push("save_script"),
      notify_run: () => sent.push("run"),
      save_script: (name, code, origin, requested) => sent.push({{ type: "save_script", name, code, origin, requested: !!requested }}),
      request_scripts: () => sent.push("request_scripts"),
      select_script: (name) => sent.push({{ type: "select_script", name }}),
      poll_messages: () => Promise.resolve(queue.splice(0)),
    }},
  }},
}};
const docListeners = {{}};
const doc = {{
  title: "",
  readyState: "complete",
  body: el("body"),
  head: {{ appendChild(s) {{ s.onload(); }} }},
  documentElement: el("root"),
  getElementById: el,
  createElement: () => ({{ value: "", textContent: "", disabled: false, style: {{}}, dataset: {{}}, appendChild() {{}} }}),
  addEventListener(n, f, cap) {{ (docListeners[n] ||= []).push({{ f, cap: !!cap }}); }},
  querySelectorAll: () => [],
  querySelector: () => null,
}};
win.document = doc;
const ctx = vm.createContext({{
  window: win,
  document: doc,
  setTimeout: () => 0,
  setInterval: (f, ms) => {{ if (ms !== 100) poll = f; return 0; }},
  clearInterval: () => {{}},
  console,
}});
vm.runInContext(src, ctx);
{load_scripts_manager}
dispatchListeners(docListeners.DOMContentLoaded || [], {{ type: "DOMContentLoaded" }});
const tick = () => new Promise((r) => setImmediate(r));
async function deliver(m) {{ queue.push(m); poll(); await tick(); await tick(); }}
async function deliverBatch(ms) {{ queue.push(...ms); poll(); await tick(); await tick(); }}
const snaps = {{}};
function snap(name) {{
  snaps[name] = {{
    text,
    readOnly,
    dataDisabled: !!el("data-binding-input").disabled,
    plainDisabled: !!el("chk-plain-text").disabled,
  }};
}}
(async () => {{
  await tick();
  {script_body}
  console.log(JSON.stringify({{
    sent,
    dirty: win.waEditorUi.isDirty(),
    text,
    readOnly,
    dataDisabled: !!el("data-binding-input").disabled,
    plainDisabled: !!el("chk-plain-text").disabled,
    dataBinding: el("data-binding-input").value,
    selectValue: el("script-select").value,
    snaps,
  }}));
}})();
"""
    proc = subprocess.run(
        ["node", "-e", driver],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize(
    "edit_during_save",
    [
        'text = "abc"; onCh();',
        'el("data-binding-input").value = "D1"; el("data-binding-input").trigger("input");',
        'el("chk-plain-text").checked = true; el("chk-plain-text").trigger("change");',
    ],
    ids=["code", "data-binding", "plain-checkbox"],
)
def test_editor_shell_stays_dirty_when_edited_during_save(edit_during_save: str) -> None:
    out = _run_shell_scenario(
        f"""
        await deliver({{type: "load", mode: "calc_cell", code: "a", data_binding: "C1", session_id: "s"}});
        text = "ab"; onCh();
        el("btn-save").click();
        {edit_during_save}
        await deliver({{type: "saved", session_id: "s"}});
        """
    )
    assert out["dirty"] is True
    assert out["sent"][out["sent"].index("save") + 1 :] == [True]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_clears_dirty_and_applies_rewritten_code_when_untouched() -> None:
    out = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "calc_cell", code: "a", session_id: "s"});
        text = "ab"; onCh();
        el("btn-save").click();
        await deliver({type: "saved", code: "rewritten", data_binding: "A1", session_id: "s"});
        """
    )
    assert out["dirty"] is False
    assert out["sent"][-1] is False
    assert out["text"] == "rewritten"
    assert out["dataBinding"] == "A1"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_run_edited_during_run_stays_dirty() -> None:
    out = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "run_script", code: "a", session_id: "s"});
        text = "ab"; onCh();
        el("btn-run").click();
        text = "abc"; onCh();
        await deliver({type: "saved", session_id: "s"});
        """
    )
    assert out["dirty"] is True
    assert out["sent"][out["sent"].index("run") + 1 :] == [True]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_run_no_typing_clears_dirty() -> None:
    out = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "run_script", code: "a", session_id: "s"});
        text = "ab"; onCh();
        el("btn-run").click();
        await deliver({type: "saved", session_id: "s"});
        """
    )
    assert out["dirty"] is False
    assert out["sent"][-1] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_clean_after_save_script_stays_clean_on_late_run_reply() -> None:
    out = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "run_script", code: "a", session_id: "s"});
        text = "ab"; onCh();
        el("btn-run").click();
        text = "abc"; onCh();
        el("btn-save").click();
        win.waEditorUi.markDirty(false);
        await deliver({type: "saved", session_id: "s"});
        """
    )
    assert out["dirty"] is False
    assert out["sent"][-1] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_request_save_locks_until_load() -> None:
    out = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "calc_cell", code: "a", session_id: "s1"});
        type("ab");
        await deliver({type: "request_save", session_id: "s1"});
        type("typed_while_locked");
        snap("after_request_save");
        await deliver({type: "saved", session_id: "s1", load_follows: true});
        type("typed_before_load");
        snap("after_saved");
        await deliver({type: "load", mode: "calc_cell", code: "next", session_id: "s2"});
        type("next_edited");
        """
    )
    expected_locked = {
        "text": "ab",
        "readOnly": True,
        "dataDisabled": True,
        "plainDisabled": True,
    }
    assert out["snaps"]["after_request_save"] == expected_locked
    assert out["snaps"]["after_saved"] == expected_locked
    assert out["text"] == "next_edited"
    assert out["readOnly"] is False
    assert out["dataDisabled"] is False
    assert out["plainDisabled"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_request_save_error_unlocks_and_keeps_text() -> None:
    out = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "calc_cell", code: "a", session_id: "s1"});
        type("ab");
        await deliver({type: "request_save", session_id: "s1"});
        type("ignored");
        await deliver({type: "error", session_id: "s1", message: "boom"});
        """
    )
    assert out["text"] == "ab"
    assert out["readOnly"] is False
    assert out["dataDisabled"] is False
    assert out["plainDisabled"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_saved_without_load_follows_unlocks_and_normal_save_does_not_lock() -> None:
    locked_then_unlocked = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "calc_cell", code: "a", session_id: "s1"});
        type("ab");
        await deliver({type: "request_save", session_id: "s1"});
        type("ignored");
        snap("before_saved");
        await deliver({type: "saved", session_id: "s1"});
        type("after_saved");
        """
    )
    assert locked_then_unlocked["snaps"]["before_saved"]["readOnly"] is True
    assert locked_then_unlocked["snaps"]["before_saved"]["text"] == "ab"
    assert locked_then_unlocked["text"] == "after_saved"
    assert locked_then_unlocked["readOnly"] is False

    run_script_locked = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "run_script", code: "a", session_id: "s1"});
        type("ab");
        await deliver({type: "request_save", session_id: "s1"});
        type("ignored");
        snap("before_saved");
        await deliver({type: "saved", session_id: "s1"});
        type("after_saved");
        """
    )
    assert run_script_locked["snaps"]["before_saved"]["readOnly"] is True
    assert run_script_locked["snaps"]["before_saved"]["text"] == "ab"
    assert "save_script" in run_script_locked["sent"]
    assert run_script_locked["text"] == "after_saved"
    assert run_script_locked["readOnly"] is False

    normal_save = _run_shell_scenario(
        """
        await deliver({type: "load", mode: "calc_cell", code: "a", session_id: "s1"});
        type("ab");
        el("btn-save").click();
        type("still_editable");
        """
    )
    assert normal_save["text"] == "still_editable"
    assert normal_save["readOnly"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_requested_named_save_locks_until_load_and_updates_dropdown() -> None:
    sections_a = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0", "B": "b0"}}]
    sections_b = [{"id": "user", "title": "My Scripts", "scripts": {"B": "b0", "A": "ab"}}]
    out = _run_shell_scenario(
        f"""
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("ab");
        await deliver({{type: "request_save", session_id: "s1"}});
        type("ignored_while_locked");
        snap("after_request_save");
        await deliverBatch([
          {{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_b)}, status_ok_text: "Saved.", load_follows: true}},
          {{type: "load", mode: "run_script", code: "b0", selected_script_name: "B", session_id: "s2"}},
        ]);
        snap("after_load");
        await deliver({{type: "scripts_list", session_id: "s2", sections: {json.dumps(sections_b)}, selected_script_name: "B"}});
        """,
        include_scripts_manager=True,
    )
    assert out["snaps"]["after_request_save"]["readOnly"] is True
    assert out["snaps"]["after_request_save"]["text"] == "ab"
    save_calls = [m for m in out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert save_calls == [{"type": "save_script", "name": "A", "code": "ab", "origin": "user", "requested": True}]
    # Between load_follows list and load, no dirty=false or select_script was emitted.
    save_idx = out["sent"].index(save_calls[0])
    req_idx = out["sent"].index("request_scripts", save_idx)
    assert out["sent"][save_idx + 1 : req_idx] == []
    assert out["snaps"]["after_load"]["readOnly"] is False
    assert out["snaps"]["after_load"]["text"] == "b0"
    assert out["selectValue"] == "B"

    # Error list unlocks and keeps the edited text.
    err_out = _run_shell_scenario(
        f"""
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("ab");
        await deliver({{type: "request_save", session_id: "s1"}});
        type("ignored");
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, status_error_text: "Read-only"}});
        """,
        include_scripts_manager=True,
    )
    assert err_out["readOnly"] is False
    assert err_out["text"] == "ab"

    # Normal Save does not lock and sends requested=false.
    norm_out = _run_shell_scenario(
        f"""
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("ab");
        el("btn-save").click();
        type("still_editable");
        """,
        include_scripts_manager=True,
    )
    assert norm_out["readOnly"] is False
    assert norm_out["text"] == "still_editable"
    norm_saves = [m for m in norm_out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert norm_saves == [{"type": "save_script", "name": "A", "code": "ab", "origin": "user", "requested": False}]
