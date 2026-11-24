# SPDX-License-Identifier: GPL-3.0-or-later
"""Node-harness tests for editor_shell.js dirty tracking across async saves/runs."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SHELL_JS = Path(__file__).resolve().parents[2] / "plugin" / "contrib" / "scripting" / "assets" / "editor" / "editor_shell.js"


def _run_shell_scenario(script_body: str) -> dict:
    driver = f"""
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync({json.dumps(str(_SHELL_JS))}, "utf8");
const els = {{}};
function el(id) {{
  return (els[id] ||= {{
    id,
    value: "",
    checked: false,
    disabled: false,
    style: {{}},
    textContent: "",
    title: "",
    h: {{}},
    classList: {{ add() {{}}, remove() {{}}, toggle() {{}} }},
    addEventListener(n, f) {{ this.h[n] = f; }},
    click() {{ this.h.click && this.h.click(); }},
    trigger(n) {{ this.h[n] && this.h[n](); }},
    setAttribute() {{}},
    appendChild() {{}},
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
      poll_messages: () => Promise.resolve(queue.splice(0)),
    }},
  }},
}};
const doc = {{
  title: "",
  body: el("body"),
  head: {{ appendChild(s) {{ s.onload(); }} }},
  documentElement: el("root"),
  getElementById: el,
  createElement: () => ({{}}),
  addEventListener() {{}},
  querySelectorAll: () => [],
  querySelector: () => null,
}};
win.document = doc;
vm.runInNewContext(src, {{
  window: win,
  document: doc,
  setTimeout: () => 0,
  setInterval: (f) => {{ poll = f; return 0; }},
  console,
}});
const tick = () => new Promise((r) => setImmediate(r));
async function deliver(m) {{ queue.push(m); poll(); await tick(); await tick(); }}
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
