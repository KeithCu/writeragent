/* global require, monaco */
(function () {
  "use strict";

  var editor = null;
  var pendingCode = "";
  var windowTitle = "PYTHON Editor";

  function setStatus(text) {
    var el = document.getElementById("status");
    if (el) {
      el.textContent = text || "";
    }
  }

  function setDataBinding(text) {
    var el = document.getElementById("data-binding");
    if (!el) {
      return;
    }
    if (text) {
      el.textContent = "Data: " + text;
      el.title = "Calc injects `data` and `data_list` from these range(s) at runtime.";
    } else {
      el.textContent = "Data: (none)";
      el.title = "Add range(s) in the =PYTHON() formula to inject `data` / `data_list`.";
    }
  }

  function applyLoad(code) {
    pendingCode = code || "";
    if (editor) {
      editor.setValue(pendingCode);
      setStatus("");
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
          setDataBinding(msg.data_binding || "");
          applyLoad(msg.code || "");
        } else if (msg.type === "saved") {
          setStatus(msg.save_as_plain ? "Saved as plain text." : "Saved.");
        } else if (msg.type === "error") {
          setStatus(msg.message || "Error");
        }
      }
    }).catch(function () { /* api not ready */ });
  }

  function initMonaco() {
    require.config({ paths: { vs: "vs" } });
    require(["vs/editor/editor.main"], function () {
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
    var plainEl = document.getElementById("chk-plain-text");
    var saveAsPlain = plainEl ? plainEl.checked : false;
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.notify_save(code, saveAsPlain);
      setStatus("Saving…");
    }
  });

  document.getElementById("btn-cancel").addEventListener("click", function () {
    if (window.pywebview && window.pywebview.api) {
      window.pywebview.api.notify_cancel();
    }
    window.close();
  });

  if (typeof require !== "undefined") {
    initMonaco();
  } else {
    setStatus("Monaco loader missing.");
  }
})();
