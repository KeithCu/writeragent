/* global require, monaco */
(function () {
  "use strict";

  var editor = null;
  var pendingCode = "";
  var statusClearTimer = null;
  var dataBindingTitle = "Calc injects `data` and `data_list` from these range(s) at runtime.";

  function setStatus(text, kind) {
    var el = document.getElementById("status");
    if (!el) {
      return;
    }
    if (statusClearTimer) {
      clearTimeout(statusClearTimer);
      statusClearTimer = null;
    }
    el.textContent = text || "";
    el.classList.remove("status-ok", "status-error");
    if (kind === "ok") {
      el.classList.add("status-ok");
    } else if (kind === "error") {
      el.classList.add("status-error");
    }
  }

  function formatErrorMessage(msg) {
    var text = msg.message || "Error";
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

  function setDataBinding(text) {
    var input = getDataBindingInput();
    if (input) {
      input.value = text || "";
    }
  }

  function getDataBindingValue() {
    var input = getDataBindingInput();
    return input ? input.value.trim() : "";
  }

  function updateDataBindingEnabled() {
    var plainEl = getPlainTextCheckbox();
    var input = getDataBindingInput();
    var label = document.getElementById("data-binding-label");
    var disabled = plainEl ? plainEl.checked : false;
    if (input) {
      input.disabled = disabled;
      input.title = disabled
        ? "Data ranges apply only when saving as a =PYTHON() formula."
        : dataBindingTitle;
    }
    if (label) {
      label.classList.toggle("disabled", disabled);
    }
  }

  function applyLoad(code) {
    pendingCode = code || "";
    if (editor) {
      editor.setValue(pendingCode);
      setStatus("", "");
    }
  }

  function pollMessages() {
    if (!window.pywebview || !window.pywebview.api) {
      return;
    }
    window.pywebview.api.poll_messages().then(function (messages) {
      if (!messages || !messages.length) {
        return;
      }
      for (var i = 0; i < messages.length; i++) {
        var msg = messages[i];
        if (!msg || !msg.type) {
          continue;
        }
        if (msg.type === "load") {
          if (msg.title) {
            document.title = msg.title;
          }
          if (msg.plain_text_label) {
            var labelEl = document.getElementById("plain-text-save-text");
            if (labelEl) {
              labelEl.textContent = msg.plain_text_label;
            }
          }
          var plainEl = getPlainTextCheckbox();
          if (plainEl && typeof msg.save_as_plain === "boolean") {
            plainEl.checked = msg.save_as_plain;
          }
          setDataBinding(msg.data_binding || "");
          updateDataBindingEnabled();
          applyLoad(msg.code || "");
        } else if (msg.type === "saved") {
          setStatus(msg.save_as_plain ? "Saved as plain text." : "Saved.", "ok");
          statusClearTimer = setTimeout(function () {
            setStatus("", "");
          }, 3000);
        } else if (msg.type === "error") {
          setStatus(formatErrorMessage(msg), "error");
        }
      }
    }).catch(function () { /* api not ready */ });
  }

  function registerJediCompletions() {
    monaco.languages.registerCompletionItemProvider("python", {
      triggerCharacters: ["."],
      provideCompletionItems: function (model, position) {
        if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.get_completions) {
          return { suggestions: [] };
        }
        var code = model.getValue();
        var line = position.lineNumber;
        var column = position.column;

        return window.pywebview.api.get_completions(code, line, column).then(function (res) {
          if (!res || !res.items) {
            return { suggestions: [] };
          }
          var suggestions = res.items.map(function (item) {
            var kind = monaco.languages.CompletionItemKind.Text;
            var k = String(item.kind).toLowerCase();
            if (k === "method") {
              kind = monaco.languages.CompletionItemKind.Method;
            } else if (k === "function") {
              kind = monaco.languages.CompletionItemKind.Function;
            } else if (k === "class") {
              kind = monaco.languages.CompletionItemKind.Class;
            } else if (k === "module") {
              kind = monaco.languages.CompletionItemKind.Module;
            } else if (k === "property") {
              kind = monaco.languages.CompletionItemKind.Property;
            } else if (k === "keyword" || k === "statement") {
              kind = monaco.languages.CompletionItemKind.Keyword;
            } else if (k === "instance" || k === "param" || k === "variable") {
              kind = monaco.languages.CompletionItemKind.Variable;
            }
            return {
              label: item.label,
              kind: kind,
              insertText: item.insertText,
              detail: item.detail || "",
              documentation: item.documentation || ""
            };
          });
          return { suggestions: suggestions };
        }).catch(function (err) {
          console.error("Jedi autocomplete error:", err);
          return { suggestions: [] };
        });
      }
    });
  }

  function initMonaco() {
    require.config({ paths: { vs: "vs" } });
    require(["vs/editor/editor.main"], function () {
      registerJediCompletions();
      editor = monaco.editor.create(document.getElementById("editor"), {
        value: pendingCode,
        language: "python",
        theme: "vs",
        automaticLayout: true,
        minimap: { enabled: false },
        fontSize: 13,
        scrollBeyondLastLine: false,
      });
      setInterval(pollMessages, 80);
      pollMessages();
    });
  }

  document.getElementById("btn-save").addEventListener("click", function () {
    var code = editor ? editor.getValue() : pendingCode;
    var plainEl = getPlainTextCheckbox();
    var saveAsPlain = plainEl ? plainEl.checked : false;
    var dataBinding = saveAsPlain ? "" : getDataBindingValue();
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.notify_save(code, saveAsPlain, dataBinding);
      setStatus("Saving…", "");
    }
  });

  document.getElementById("btn-cancel").addEventListener("click", function () {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.notify_cancel();
    }
    window.close();
  });

  var plainCheckbox = getPlainTextCheckbox();
  if (plainCheckbox) {
    plainCheckbox.addEventListener("change", updateDataBindingEnabled);
  }

  var dataInput = getDataBindingInput();
  if (dataInput) {
    dataInput.title = dataBindingTitle;
  }
  updateDataBindingEnabled();

  if (typeof require !== "undefined") {
    initMonaco();
  } else {
    setStatus("Monaco loader missing.", "error");
  }
})();
