/**
 * WriterAgent editor shell: pywebview bridge, toolbar, save/run/cancel wiring,
 * dirty tracking, theme and UI strings. All editor-widget calls go through the
 * backend object (see backend_monaco.js for the interface). Keep this file free
 * of Monaco-specific calls; keep backends free of pywebview and UNO.
 */

// =========================================================================================
// WARNING: PARITY INVARIANT WITH PYTHON HOST & IPC PROTOCOL
// If you modify message parsing, shortcuts, save/run dispatch, or UI strings handling,
// you MUST also update the corresponding Python modules:
//   - Python Host Bridge:        plugin/scripting/editor_host.py
//   - IPC Message Protocol:      plugin/scripting/editor_ipc.py
//   - UI Strings Catalog:        plugin/scripting/editor_ui_strings.py
//   - Script Manager Frontend:   plugin/contrib/scripting/assets/editor/scripts_manager.js
//   - Monaco Backend:            plugin/contrib/scripting/assets/editor/backend_monaco.js
//   - Native Cell Editor:        plugin/calc/python/cell_editor_ui.py
//     (calc_cell Save / Save without =PY() / Data enablement / status)
// =========================================================================================

(function () {
  "use strict";

  var editor = null;
  var backend = null;
  var pendingCode = "";
  var ui = {};
  var defaultStatusOkText = "Saved.";
  var pendingStatusText = "Saving…";
  var currentMode = "calc_cell";
  var sessionId = "";
  var sessionTarget = {};
  var isDirty = false;
  var suppressDirty = false;
  var dataBindingTitle = "Calc injects `data` and `ranges` from these range(s) at runtime.";
  var dataBindingDisabledTitle = "Data ranges apply only when saving as a =PY() formula.";
  var followCodeRef = false;

  function t(key, fallback) {
    var value = ui[key];
    return value !== undefined && value !== null && value !== "" ? value : fallback;
  }

  function fmt(key) {
    var template = t(key, "");
    var args = arguments;
    return template.replace(/\{(\d+)\}/g, function (_, index) {
      var argIndex = parseInt(index, 10) + 1;
      return args[argIndex] !== undefined ? args[argIndex] : "";
    });
  }

  function setStatus(text, kind) {
    var el = document.getElementById("status");
    if (el) {
      var prefix = t("status_prefix", "Status:");
      var label = text || t("ready", "Ready");
      if (prefix && label.indexOf(prefix) !== 0) {
        label = prefix + " " + label;
      }
      el.value = label;
      el.classList.remove("status-ok", "status-error");
      if (kind === "ok") {
        el.classList.add("status-ok");
      } else if (kind === "error") {
        el.classList.add("status-error");
      }
    }
  }

  function applyTheme(tinfo) {
    if (!tinfo) return;
    var isDark = !!(tinfo.is_dark || (tinfo.monaco && tinfo.monaco.indexOf("dark") !== -1));
    document.body.classList.toggle("dark", isDark);
    document.body.classList.toggle("light", !isDark);
  }

  function formatErrorMessage(msg) {
    var text = msg.message || t("error", "Error");
    if (msg.traceback) {
      var lines = String(msg.traceback).split("\n");
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (line) {
          text = text + " — " + line;
          break;
        }
      }
    }
    return text;
  }

  function getDataBindingInput() {
    return document.getElementById("data-binding-input");
  }

  function getPlainTextCheckbox() {
    return document.getElementById("chk-plain-text");
  }

  function setToolbarVisible(elementId, visible) {
    var el = document.getElementById(elementId);
    if (el) {
      el.classList.toggle("toolbar-hidden", !visible);
    }
  }

  function applyUiChrome() {
    var runBtn = document.getElementById("btn-run");
    if (runBtn) {
      runBtn.textContent = t("run_label", "Run");
    }

    var saveBtn = document.getElementById("btn-save");
    if (saveBtn) {
      saveBtn.textContent = t("save_label", "Save");
    }

    var closeBtn = document.getElementById("btn-cancel");
    if (closeBtn) {
      closeBtn.textContent = t("close_label", currentMode === "run_script" || currentMode === "latex" ? "Close" : "Cancel");
    }

    var plainTextEl = document.getElementById("plain-text-save-text");
    if (plainTextEl) {
      plainTextEl.textContent = t("plain_text_label", "Save without =PY()");
    }

    var dataLabel = document.getElementById("data-binding-label");
    if (dataLabel) {
      dataLabel.textContent = t("data_label", "Data:");
    }

    var dataInput = getDataBindingInput();
    if (dataInput) {
      dataInput.placeholder = t("data_placeholder", "A1:C1  or  A1:C1, C1:C5");
    }

    dataBindingTitle = t(
      "data_binding_title",
      "Calc injects `data` and `ranges` from these range(s) at runtime."
    );
    dataBindingDisabledTitle = t(
      "data_binding_disabled_title",
      "Data ranges apply only when saving as a =PY() formula."
    );
    if (dataInput) {
      dataInput.title = dataBindingTitle;
    }
  }

  function maybeHintJediMissing() {
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.is_jedi_available) {
      return;
    }
    window.pywebview.api.is_jedi_available().then(function (ok) {
      if (!ok) {
        setStatus(t("jedi_missing", "Install jedi in the Python venv for completions."), "");
      }
    }).catch(function () {});
  }

  function revealToolbar() {
    var tb = document.getElementById("toolbar");
    if (tb) {
      tb.classList.remove("toolbar-loading");
    }
  }

  function applyLoadMessage(msg) {
    ui = msg.ui || {};
    if (msg.title) {
      document.title = msg.title;
    }
    var showPlainText = msg.show_plain_text !== false;
    var showDataBinding = msg.show_data_binding !== false;
    currentMode = msg.mode || "calc_cell";
    sessionId = msg.session_id || "";
    sessionTarget = msg.target && typeof msg.target === "object" ? msg.target : {};
    var isRunScript = currentMode === "run_script";
    var isCalcCell = currentMode === "calc_cell";
    followCodeRef = !!msg.follow_code_ref;

    setToolbarVisible("btn-run", isRunScript);
    setToolbarVisible("cell-addr", isCalcCell);
    var addrEl = document.getElementById("cell-addr");
    if (addrEl) {
      addrEl.textContent = isCalcCell ? (msg.cell_address || "") : "";
    }
    setToolbarVisible("plain-text-save-label", showPlainText);
    setToolbarVisible("data-binding-label", showDataBinding);
    setToolbarVisible("data-binding-input", showDataBinding);

    applyUiChrome();

    var plainEl = getPlainTextCheckbox();
    if (plainEl) {
      plainEl.checked = !!msg.save_as_plain;
    }

    // status_ok_text is a top-level load/saved field, not a ui key.
    defaultStatusOkText = t("saved_default", "Saved.");
    if (msg.saved_ok_text) {
      defaultStatusOkText = msg.saved_ok_text;
    } else if (msg.status_ok_text) {
      defaultStatusOkText = msg.status_ok_text;
    }

    pendingStatusText = isRunScript ? t("running", "Running…") : t("saving", "Saving…");

    var text = msg.data_binding || "";
    var input = getDataBindingInput();
    if (input) {
      input.value = text || "";
    }

    updateDataBindingEnabled();
    var code = msg.code || "";
    pendingCode = code || "";
    if (editor) {
      suppressDirty = true;
      editor.setValue(pendingCode);
      editor.setLanguage(msg.language || "python");
      suppressDirty = false;
      markDirty(false);
      setStatus(t("ready", "Ready"), "");
      maybeHintJediMissing();
    } else {
      markDirty(false);
    }

    if (msg.theme) {
      if (backend) {
        backend.setTheme(!!msg.theme.is_dark, msg.theme.monaco);
      }
      applyTheme(msg.theme);
    }

    revealToolbar();
  }

  setTimeout(revealToolbar, 500);

  function markDirty(dirty) {
    var next = !!dirty;
    if (isDirty === next) {
      return;
    }
    isDirty = next;
    if (window.pywebview && window.pywebview.api && window.pywebview.api.notify_dirty) {
      try {
        window.pywebview.api.notify_dirty(isDirty, sessionId, currentMode, sessionTarget);
      } catch (e) {}
    }
  }

  function updateDataBindingEnabled() {
    var plainEl = getPlainTextCheckbox();
    var input = getDataBindingInput();
    var label = document.getElementById("data-binding-label");
    var disabled = !!plainEl && plainEl.checked && !followCodeRef;
    if (input) {
      input.disabled = disabled;
      input.title = disabled ? dataBindingDisabledTitle : dataBindingTitle;
    }
    if (label) {
      label.classList.toggle("disabled", disabled);
    }
  }

  function pollMessages() {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.poll_messages) {
      window.pywebview.api.poll_messages().then(function (messages) {
        if (messages && messages.length) {
          for (var i = 0; i < messages.length; i++) {
            var msg = messages[i];
            if (msg && msg.type) {
              if (msg.session_id && sessionId && msg.session_id !== sessionId && msg.type !== "load") {
                continue;
              }
              if (msg.type === "load") {
                applyLoadMessage(msg);
              } else if (msg.type === "request_save") {
                var saveBtn = document.getElementById("btn-save");
                if (saveBtn) {
                  saveBtn.click();
                }
              } else if (msg.type === "saved") {
                markDirty(false);
                var okText = msg.status_ok_text || defaultStatusOkText;
                setStatus(okText, "ok");
              } else if (msg.type === "error") {
                setStatus(formatErrorMessage(msg), "error");
              } else if (msg.type === "theme") {
                applyTheme(msg);
              }
            }
          }
        }
      }).catch(function () {});
    }
  }

  function getEditorCode() {
    return editor ? editor.getValue() : pendingCode;
  }

  window.waEditorUi = {
    t: t,
    fmt: fmt,
    setStatus: setStatus,
    isDirty: function () {
      return isDirty;
    },
    markDirty: markDirty,
    setSuppressDirty: function (suppress) {
      suppressDirty = !!suppress;
    },
    getUi: function () {
      return ui;
    },
    applyUiFromLoad: function (msg) {
      if (msg && msg.ui) {
        ui = msg.ui;
        applyUiChrome();
      }
    }
  };

  document.getElementById("btn-run").addEventListener("click", function () {
    var code = getEditorCode();
    if (window.pywebview && window.pywebview.api && window.pywebview.api.notify_run) {
      window.pywebview.api.notify_run(code, sessionId, currentMode, sessionTarget);
      setStatus(t("running", "Running…"), "");
    }
  });

  document.getElementById("btn-save").addEventListener("click", function () {
    var code = getEditorCode();
    if (window.pywebview && window.pywebview.api) {
      if (currentMode === "run_script" && window.pywebview.api.notify_save_script) {
        window.pywebview.api.notify_save_script(code, sessionId, currentMode, sessionTarget);
        setStatus(t("saving", "Saving…"), "");
        return;
      }
      var plainEl = getPlainTextCheckbox();
      var saveAsPlain = !!plainEl && plainEl.checked;
      var input = getDataBindingInput();
      // Follow keeps Data enabled (updateDataBindingEnabled). The plain-text
      // box is checked because the code cell is setString'd, but the ranges
      // still live on the formula. Sending "" made _apply_followed_ref_save
      // treat the ranges as cleared and rewrite =PY($A$1; C1:C10) to
      // =PY($A$1). Same rule as cell_editor_ui._save.
      var dataBinding = (followCodeRef || !saveAsPlain) ? (input ? input.value.trim() : "") : "";
      window.pywebview.api.notify_save(code, saveAsPlain, dataBinding, "cell_save", sessionId, currentMode, sessionTarget);
      setStatus(pendingStatusText, "");
    }
  });

  document.getElementById("btn-cancel").addEventListener("click", function () {
    if (window.editor) {
      window.editor.setValue("");
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.notify_cancel) {
      window.pywebview.api.notify_cancel();
    }
  });

  var plainCheckbox = getPlainTextCheckbox();
  if (plainCheckbox) {
    plainCheckbox.addEventListener("change", function () {
      updateDataBindingEnabled();
      markDirty(true);
    });
  }
  var dataInputEl = getDataBindingInput();
  if (dataInputEl) {
    dataInputEl.addEventListener("input", function () {
      markDirty(true);
    });
  }

  updateDataBindingEnabled();

  function getCompletions(code, line, column) {
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.get_completions) {
      return null;
    }
    return window.pywebview.api.get_completions(code, line, column);
  }

  function loadScript(src, onLoad, onError) {
    var s = document.createElement("script");
    s.src = src;
    s.onload = onLoad;
    s.onerror = onError;
    document.head.appendChild(s);
  }

  function onLoaderError() {
    setStatus(t("editor_loader_missing", "Editor loader missing."), "error");
  }

  var backendName = window.WA_EDITOR_BACKEND === "codemirror" ? "codemirror" : "monaco";
  var scripts = backendName === "codemirror"
    ? ["cm6/cm6.bundle.js", "backend_cm6.js"]
    : ["vs/loader.js", "backend_monaco.js"];

  function loadScriptsSeq(list, idx, done) {
    if (idx >= list.length) {
      done();
      return;
    }
    loadScript(list[idx], function () {
      loadScriptsSeq(list, idx + 1, done);
    }, onLoaderError);
  }

  loadScriptsSeq(scripts, 0, function () {
    backend = window.WaEditorBackends && window.WaEditorBackends[backendName];
    if (!backend) {
      onLoaderError();
      return;
    }
    backend.create(document.getElementById("editor"), {
      value: pendingCode,
      language: "python",
      getCompletions: getCompletions
    }).then(function () {
      window.editor = editor = backend;
      backend.onChange(function () {
        if (!suppressDirty) {
          markDirty(true);
        }
      });

      setInterval(pollMessages, 80);
      pollMessages();
      maybeHintJediMissing();
    }, onLoaderError);
  });
})();
