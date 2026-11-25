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
      ask_unsaved: (name) => sent.push({{ type: "ask_unsaved", name }}),
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_shell_saved_load_follows_ignores_stale_session_and_save_targets_active_script() -> None:
    sections_a = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0", "B": "b0", "R": "r_user"}}]
    sections_doc = [
        {"id": "user", "title": "My Scripts", "scripts": {"A": "a0", "B": "b0", "R": "r_user"}},
        {"id": "document", "title": "This Document", "scripts": {"[Doc] X": "x0"}},
    ]

    # 1. saved with load_follows followed by load in one batch: no notify_dirty carries ended session s1.
    batch_out = _run_shell_scenario(
        """
        win.pywebview.api.notify_dirty = (dirty, sid) => {
          sent.push({type: "dirty", dirty, sid});
        };
        await deliver({type: "load", mode: "calc_cell", code: "a0", session_id: "s1"});
        type("a1");
        await deliverBatch([
          {type: "saved", session_id: "s1", code: "a1", load_follows: true},
          {type: "load", mode: "calc_cell", code: "b0", session_id: "s2"},
        ]);
        """,
    )
    assert [(m["dirty"], m["sid"]) for m in batch_out["sent"] if isinstance(m, dict) and m.get("type") == "dirty"] == [(True, "s1"), (False, "s2")]

    # 2. Save between load B and its scripts_list saves B; [Doc] X saves with origin document;
    #    a load with no script sends the plain notify_save_script.
    load_save_out = _run_shell_scenario(
        """
        win.pywebview.api.notify_save_script = (code) => sent.push({type: "save_script_plain", code});
        await deliver({type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"});
        await deliver({type: "scripts_list", session_id: "s1", sections: %s, selected_script_name: "A"});
        await deliver({type: "load", mode: "run_script", code: "b0", selected_script_name: "B", session_id: "s2"});
        el("btn-save").click();
        await deliver({type: "load", mode: "run_script", code: "x0", selected_script_name: "[Doc] X", session_id: "s3"});
        el("btn-save").click();
        await deliver({type: "load", mode: "run_script", code: "anon", selected_script_name: "", session_id: "s4"});
        el("btn-save").click();
        """
        % json.dumps(sections_doc),
        include_scripts_manager=True,
    )
    saves = [m for m in load_save_out["sent"] if isinstance(m, dict) and m.get("type") in ("save_script", "save_script_plain")]
    assert saves == [
        {"type": "save_script", "name": "B", "code": "b0", "origin": "user", "requested": False},
        {"type": "save_script", "name": "[Doc] X", "code": "x0", "origin": "document", "requested": False},
        {"type": "save_script_plain", "code": "anon"},
    ]

    # 3. New "R" attached to document while My Scripts has "R": after scripts_list confirms it,
    #    typing and Save targets ("[Doc] R", "document").
    sections_doc_r = sections_a + [
        {"id": "document", "title": "This Document", "scripts": {"[Doc] R": '# A simple script\nresult = "Hello from Python!"\n'}}
    ]
    new_doc_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", document_available: true, sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "R";
        el("new-script-attach-check").checked = true;
        el("btn-new-script-create").click();
        await deliver({{type: "scripts_list", session_id: "s1", document_available: true, sections: {json.dumps(sections_doc_r)}, selected_script_name: "[Doc] R", status_ok_text: "Saved"}});
        type("doc_r");
        el("btn-save").click();
        """,
        include_scripts_manager=True,
    )
    new_saves = [(m["name"], m["origin"]) for m in new_doc_out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert new_saves == [("R", "document"), ("[Doc] R", "document")]

    # 4. Delete A then Save before scripts_list: targets A and never the plain path.
    del_out = _run_shell_scenario(
        f"""
        win.pywebview.api.delete_script = (name, origin) => sent.push({{type: "delete_script", name, origin}});
        ctx.confirm = () => true;
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-delete-script").click();
        el("btn-save").click();
        """,
        include_scripts_manager=True,
    )
    del_saves = [m for m in del_out["sent"] if isinstance(m, dict) and m.get("type") in ("save_script", "save_script_plain")]
    assert del_saves == [{"type": "save_script", "name": "A", "code": "a0", "origin": "user", "requested": False}]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_new_script_failure_keeps_script_and_text() -> None:
    sections_a = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0"}}]
    out = _run_shell_scenario(
        f"""
        win.pywebview.api.notify_dirty = (dirty) => sent.push({{type: "dirty", dirty}});
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_edit");
        el("btn-new-script").click();
        await deliver({{type: "unsaved_choice", choice: "discard"}});
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A", status_error_text: "Failed to save"}});
        sent.push({{type: "state", value: win.editor.getValue(), dirty: win.waEditorUi.isDirty()}});
        el("btn-save").click();
        """,
        include_scripts_manager=True,
    )
    states = [m for m in out["sent"] if isinstance(m, dict) and m.get("type") == "state"]
    assert states == [{"type": "state", "value": "a_edit", "dirty": True}]
    saves = [(m["name"], m["code"]) for m in out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert saves == [("B", '# A simple script\nresult = "Hello from Python!"\n'), ("A", "a_edit")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_new_script_shows_template_only_after_list() -> None:
    template = '# A simple script\nresult = "Hello from Python!"\n'
    sections_a = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0"}}]
    sections_ab = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0", "B": template}}]

    # 1. Success list switches to B with the template.
    ok_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        sent.push({{type: "before", value: win.editor.getValue()}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ab)}, selected_script_name: "B", status_ok_text: "Saved"}});
        sent.push({{type: "after", value: win.editor.getValue(), selected: el("script-select").value}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in ok_out["sent"] if isinstance(m, dict) and m.get("type") == "before"] == [{"type": "before", "value": "a0"}]
    assert [m for m in ok_out["sent"] if isinstance(m, dict) and m.get("type") == "after"] == [{"type": "after", "value": template, "selected": "B"}]

    # 2. Typing while New is in flight keeps the typed text and leaves A selected.
    typed_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        type("a_typing");
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ab)}, selected_script_name: "B", status_ok_text: "Saved"}});
        sent.push({{type: "after", value: win.editor.getValue(), selected: el("script-select").value}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in typed_out["sent"] if isinstance(m, dict) and m.get("type") == "after"] == [{"type": "after", "value": "a_typing", "selected": "A"}]

    # 3. Unrelated list first is ignored; later B list switches.
    unrelated_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        sent.push({{type: "mid", value: win.editor.getValue(), selected: el("script-select").value}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ab)}, selected_script_name: "B", status_ok_text: "Saved"}});
        sent.push({{type: "after", value: win.editor.getValue(), selected: el("script-select").value}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in unrelated_out["sent"] if isinstance(m, dict) and m.get("type") == "mid"] == [{"type": "mid", "value": "a0", "selected": "A"}]
    assert [m for m in unrelated_out["sent"] if isinstance(m, dict) and m.get("type") == "after"] == [{"type": "after", "value": template, "selected": "B"}]

    # 4. Error frame (timeout) then Save targets A with its own text.
    timeout_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_before_timeout");
        el("btn-new-script").click();
        await deliver({{type: "unsaved_choice", choice: "discard"}});
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "error", message: "timeout"}});
        el("btn-save").click();
        """,
        include_scripts_manager=True,
    )
    timeout_saves = [(m["name"], m["code"]) for m in timeout_out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert timeout_saves == [("B", template), ("A", "a_before_timeout")]

    # 4b. Error frame during Yes-before-New save clears the pending slot so later New asks and late Save does not open New.
    save_timeout_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_edit");
        el("btn-new-script").click();
        await deliver({{type: "unsaved_choice", choice: "save"}});
        await deliver({{type: "error", message: "timeout"}});
        el("btn-save").click();
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps([{"id": "user", "title": "My Scripts", "scripts": {"A": "a_edit"}}])}, selected_script_name: "A", status_ok_text: "Saved"}});
        sent.push({{type: "modal_after_save", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        type("a_edit2");
        el("btn-new-script").click();
        """,
        include_scripts_manager=True,
    )
    assert [m for m in save_timeout_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_after_save"] == [
        {"type": "modal_after_save", "hidden": True}
    ]
    assert len([m for m in save_timeout_out["sent"] if isinstance(m, dict) and m.get("type") == "ask_unsaved"]) == 2

    # 5. Empty first list (no script selected) still switches to B on success.
    empty_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "", selected_script_name: "", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: [], selected_script_name: ""}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps([{"id": "user", "title": "My Scripts", "scripts": {"B": template}}])}, selected_script_name: "B", status_ok_text: "Saved"}});
        sent.push({{type: "after", value: win.editor.getValue(), selected: el("script-select").value}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in empty_out["sent"] if isinstance(m, dict) and m.get("type") == "after"] == [{"type": "after", "value": template, "selected": "B"}]

    # 6. load_follows (without load) and load (without load_follows) each clear pendingNew.
    lf_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "scripts_list", session_id: "s1", load_follows: true}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ab)}, selected_script_name: "A"}});
        sent.push({{type: "after", value: win.editor.getValue(), selected: el("script-select").value}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in lf_out["sent"] if isinstance(m, dict) and m.get("type") == "after"] == [{"type": "after", "value": "a0", "selected": "A"}]

    load_clear_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s2"}});
        await deliver({{type: "scripts_list", session_id: "s2", sections: {json.dumps(sections_ab)}, selected_script_name: "A"}});
        sent.push({{type: "after", value: win.editor.getValue(), selected: el("script-select").value}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in load_clear_out["sent"] if isinstance(m, dict) and m.get("type") == "after"] == [{"type": "after", "value": "a0", "selected": "A"}]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_new_script_prompts_when_dirty() -> None:
    template = '# A simple script\nresult = "Hello from Python!"\n'
    sections_a = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0"}}]
    sections_a_saved = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a_edit"}}]
    sections_ab = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a_edit", "B": template}}]

    # 1. Cancel keeps dirty edits and does not open the modal.
    cancel_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_edit");
        el("btn-new-script").click();
        sent.push({{type: "modal_before", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        await deliver({{type: "unsaved_choice", choice: "cancel"}});
        sent.push({{type: "modal_after", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in cancel_out["sent"] if isinstance(m, dict) and m.get("type") == "ask_unsaved"] == [
        {"type": "ask_unsaved", "name": "A"}
    ]
    assert [m for m in cancel_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_before"] == [
        {"type": "modal_before", "hidden": True}
    ]
    assert [m for m in cancel_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_after"] == [
        {"type": "modal_after", "hidden": True}
    ]
    assert cancel_out["dirty"] is True
    assert cancel_out["text"] == "a_edit"

    # 2. Save saves A first, opens modal once scripts_list confirms, then creates B.
    save_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_edit");
        el("btn-new-script").click();
        await deliver({{type: "unsaved_choice", choice: "save"}});
        sent.push({{type: "modal_while_saving", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a_saved)}, selected_script_name: "A", status_ok_text: "Saved"}});
        sent.push({{type: "modal_after_save", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ab)}, selected_script_name: "B", status_ok_text: "Saved"}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in save_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_while_saving"] == [
        {"type": "modal_while_saving", "hidden": True}
    ]
    assert [m for m in save_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_after_save"] == [
        {"type": "modal_after_save", "hidden": False}
    ]
    saves = [(m["name"], m["code"]) for m in save_out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert saves == [("A", "a_edit"), ("B", template)]
    assert save_out["text"] == template
    assert save_out["selectValue"] == "B"
    assert save_out["dirty"] is False

    # 3. Failed Save keeps modal closed and buffer dirty.
    fail_save_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_edit");
        el("btn-new-script").click();
        await deliver({{type: "unsaved_choice", choice: "save"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A", status_error_text: "Save failed"}});
        sent.push({{type: "modal_after_fail", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in fail_save_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_after_fail"] == [
        {"type": "modal_after_fail", "hidden": True}
    ]
    assert fail_save_out["dirty"] is True
    assert fail_save_out["text"] == "a_edit"

    # 4. Discard then modal Cancel keeps edits and dirty state.
    discard_cancel_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        type("a_edit");
        el("btn-new-script").click();
        await deliver({{type: "unsaved_choice", choice: "discard"}});
        sent.push({{type: "modal_open", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        el("btn-new-script-cancel").click();
        sent.push({{type: "modal_closed", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in discard_cancel_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_open"] == [
        {"type": "modal_open", "hidden": False}
    ]
    assert [m for m in discard_cancel_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_closed"] == [
        {"type": "modal_closed", "hidden": True}
    ]
    assert discard_cancel_out["dirty"] is True
    assert discard_cancel_out["text"] == "a_edit"

    # 5. Clean buffer opens modal directly without ask_unsaved.
    clean_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_a)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        sent.push({{type: "modal_clean", hidden: el("new-script-modal").classList.contains("toolbar-hidden")}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in clean_out["sent"] if isinstance(m, dict) and m.get("type") == "ask_unsaved"] == []
    assert [m for m in clean_out["sent"] if isinstance(m, dict) and m.get("type") == "modal_clean"] == [
        {"type": "modal_clean", "hidden": False}
    ]

    # 6. Yes with no selected script sets an error status, keeps modal closed, and leaves New usable.
    empty_yes_out = _run_shell_scenario(
        """
        backend.focus = () => {};
        el("new-script-modal").classList.add("toolbar-hidden");
        await deliver({type: "load", mode: "run_script", code: "", selected_script_name: "", session_id: "s1"});
        await deliver({type: "scripts_list", session_id: "s1", sections: [], selected_script_name: ""});
        type("unsaved_orphan");
        el("btn-new-script").click();
        await deliver({type: "unsaved_choice", choice: "save"});
        sent.push({
            type: "after_empty_yes",
            hidden: el("new-script-modal").classList.contains("toolbar-hidden"),
            isError: el("status").classList.contains("status-error"),
        });
        el("btn-new-script").click();
        """,
        include_scripts_manager=True,
    )
    assert [m for m in empty_yes_out["sent"] if isinstance(m, dict) and m.get("type") == "after_empty_yes"] == [
        {"type": "after_empty_yes", "hidden": True, "isError": True}
    ]
    assert [m for m in empty_yes_out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"] == []
    assert len([m for m in empty_yes_out["sent"] if isinstance(m, dict) and m.get("type") == "ask_unsaved"]) == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switch_script_prompts_when_dirty() -> None:
    template = '# A simple script\nresult = "Hello from Python!"\n'
    sections_ac = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0", "C": "c0"}}]
    sections_abc = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a0", "B": template, "C": "c0"}}]
    sections_ac_saved = [{"id": "user", "title": "My Scripts", "scripts": {"A": "a_edit", "C": "c0"}}]

    # 1. Cancel restores previous selection and keeps edits.
    cancel_out = _run_shell_scenario(
        f"""
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ac)}, selected_script_name: "A"}});
        type("a_edit");
        el("script-select").value = "C";
        el("script-select").trigger("change");
        await deliver({{type: "unsaved_choice", choice: "cancel"}});
        """,
        include_scripts_manager=True,
    )
    assert cancel_out["selectValue"] == "A"
    assert cancel_out["text"] == "a_edit"
    assert cancel_out["dirty"] is True

    # 2. Discard switches to C and clears dirty.
    discard_out = _run_shell_scenario(
        f"""
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ac)}, selected_script_name: "A"}});
        type("a_edit");
        el("script-select").value = "C";
        el("script-select").trigger("change");
        await deliver({{type: "unsaved_choice", choice: "discard"}});
        """,
        include_scripts_manager=True,
    )
    assert discard_out["selectValue"] == "C"
    assert discard_out["text"] == "c0"
    assert discard_out["dirty"] is False

    # 3. Save saves A first, ignores late pendingNew list while prompt is open, then switches to C.
    save_out = _run_shell_scenario(
        f"""
        backend.focus = () => {{}};
        await deliver({{type: "load", mode: "run_script", code: "a0", selected_script_name: "A", session_id: "s1"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ac)}, selected_script_name: "A"}});
        el("btn-new-script").click();
        el("new-script-name-input").value = "B";
        el("btn-new-script-create").click();
        type("a_edit");
        el("script-select").value = "C";
        el("script-select").trigger("change");
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_abc)}, selected_script_name: "B"}});
        sent.push({{type: "during_prompt", value: win.editor.getValue(), selected: el("script-select").value}});
        await deliver({{type: "unsaved_choice", choice: "save"}});
        await deliver({{type: "scripts_list", session_id: "s1", sections: {json.dumps(sections_ac_saved)}, selected_script_name: "A", status_ok_text: "Saved"}});
        """,
        include_scripts_manager=True,
    )
    assert [m for m in save_out["sent"] if isinstance(m, dict) and m.get("type") == "during_prompt"] == [
        {"type": "during_prompt", "value": "a_edit", "selected": "A"}
    ]
    saves = [(m["name"], m["code"]) for m in save_out["sent"] if isinstance(m, dict) and m.get("type") == "save_script"]
    assert saves == [("B", template), ("A", "a_edit")]
    assert save_out["selectValue"] == "C"
    assert save_out["text"] == "c0"
    assert save_out["dirty"] is False
