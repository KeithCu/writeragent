(function() {
  "use strict";

  var savedScripts = {};
  var currentSelectedName = "";
  var initialRequested = false;

  function getSelectEl() {
    return document.getElementById("script-select");
  }

  function getDeleteBtn() {
    return document.getElementById("btn-delete-script");
  }

  function getManagerContainer() {
    return document.getElementById("script-manager-container");
  }

  function setStatus(text, kind) {
    var el = document.getElementById("status");
    if (el) {
      el.textContent = text || "";
      el.className = "";
      if (kind === "ok") el.classList.add("status-ok");
      if (kind === "error") el.classList.add("status-error");
    }
  }

  // Intercept incoming messages from pywebview poll_messages
  function handleScriptsManagerMessages(msg) {
    if (!msg) return;

    if (msg.type === "load") {
      var isRunScript = msg.mode === "run_script";
      var container = getManagerContainer();
      if (container) {
        container.classList.toggle("toolbar-hidden", !isRunScript);
      }
      if (isRunScript && window.pywebview && window.pywebview.api && window.pywebview.api.request_scripts) {
        window.pywebview.api.request_scripts();
        initialRequested = true;
      }
    } else if (msg.type === "scripts_list") {
      savedScripts = msg.scripts || {};
      updateDropdown();
      if (msg.status_ok_text) {
        setStatus(msg.status_ok_text, "ok");
        setTimeout(function() { setStatus("", ""); }, 3000);
      }
    }
  }

  // Expose globally so Python's evaluate_js can invoke it directly
  window.handleScriptsManagerMessage = function(msg) {
    if (Array.isArray(msg)) {
      for (var i = 0; i < msg.length; i++) {
        handleScriptsManagerMessages(msg[i]);
      }
    } else {
      handleScriptsManagerMessages(msg);
    }
  };

  // Populate select dropdown based on savedScripts
  function updateDropdown() {
    var select = getSelectEl();
    if (!select) return;

    // Save current selection value
    var lastVal = currentSelectedName || select.value || "";

    // Clear options but keep the first option (Sample)
    select.innerHTML = '<option value="">Sample</option>';

    var sortedNames = Object.keys(savedScripts).sort();
    for (var i = 0; i < sortedNames.length; i++) {
      var name = sortedNames[i];
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      select.appendChild(opt);
    }

    // Restore selection if it still exists
    if (savedScripts[lastVal] !== undefined) {
      select.value = lastVal;
    } else {
      select.value = "";
    }
    currentSelectedName = select.value;
    updateDeleteButtonVisibility();
  }

  function updateDeleteButtonVisibility() {
    var deleteBtn = getDeleteBtn();
    if (deleteBtn) {
      deleteBtn.classList.remove("toolbar-hidden");
    }
  }

  // Load code from script when dropdown changes
  function onDropdownChange() {
    var select = getSelectEl();
    if (!select) return;

    var name = select.value;
    currentSelectedName = name;
    updateDeleteButtonVisibility();

    if (name && savedScripts[name] !== undefined) {
      if (window.editor) {
        window.editor.setValue(savedScripts[name]);
        setStatus("Loaded script '" + name + "'.", "ok");
        setTimeout(function() { setStatus("", ""); }, 2000);
      }
    }
  }

  // Save As... action
  function onSaveAs() {
    var defaultName = currentSelectedName || "";
    var name = prompt("Enter a name for the script:", defaultName);
    if (!name) return;
    name = name.trim();
    if (!name) return;

    if (window.editor && window.pywebview && window.pywebview.api && window.pywebview.api.save_script) {
      var code = window.editor.getValue();
      currentSelectedName = name; // set to auto-select after save
      window.pywebview.api.save_script(name, code);
      setStatus("Saving script '" + name + "'...", "ok");
    }
  }

  // Delete script action
  function onDeleteScript() {
    var select = getSelectEl();
    if (!select) return;

    var name = select.value;
    if (!name) {
      // Deleting the "Sample" scratchpad!
      if (confirm("Are you sure you want to clear the Sample scratchpad?")) {
        if (window.editor) {
          window.editor.setValue("");
        }
        if (window.pywebview && window.pywebview.api && window.pywebview.api.notify_save_script) {
          window.pywebview.api.notify_save_script("");
        }
        setStatus("Cleared Sample scratchpad.", "ok");
      }
      return;
    }

    if (confirm("Are you sure you want to delete '" + name + "'?")) {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_script) {
        currentSelectedName = ""; // reset selection
        window.pywebview.api.delete_script(name);
        setStatus("Deleting script '" + name + "'...", "ok");
      }
    }
  }

  // Setup interception on pywebview
  function setupInterception() {
    if (window.pywebview && window.pywebview.api) {
      // Intercept poll_messages
      var originalPoll = window.pywebview.api.poll_messages;
      if (originalPoll && !originalPoll.__intercepted) {
        window.pywebview.api.poll_messages = function() {
          return originalPoll.apply(this, arguments).then(function(messages) {
            if (messages && messages.length) {
              window.handleScriptsManagerMessage(messages);
            }
            return messages;
          });
        };
        window.pywebview.api.poll_messages.__intercepted = true;
      }
    }
  }

  // Bulletproof initialization polling to bypass any ready race conditions
  function ensureInitialRequest() {
    if (initialRequested) return;

    setupInterception();

    if (window.pywebview && window.pywebview.api && window.pywebview.api.request_scripts) {
      var btnRun = document.getElementById("btn-run");
      var isRunScript = btnRun && !btnRun.classList.contains("toolbar-hidden");
      if (isRunScript) {
        var container = getManagerContainer();
        if (container) container.classList.remove("toolbar-hidden");
        window.pywebview.api.request_scripts();
        initialRequested = true;
      }
    }
  }

  // Try immediately
  ensureInitialRequest();
  // Poll until success
  var pollInterval = setInterval(function() {
    ensureInitialRequest();
    if (initialRequested) {
      clearInterval(pollInterval);
    }
  }, 100);

  // Bind UI events when DOM is loaded
  document.addEventListener("DOMContentLoaded", function() {
    var select = getSelectEl();
    if (select) {
      select.addEventListener("change", onDropdownChange);
    }

    var btnSaveAs = document.getElementById("btn-save-as");
    if (btnSaveAs) {
      btnSaveAs.addEventListener("click", onSaveAs);
    }

    var btnDelete = getDeleteBtn();
    if (btnDelete) {
      btnDelete.addEventListener("click", onDeleteScript);
    }

    // Intercept standard Save button click in the capturing phase (true).
    // This stops editor.js's save handler from running and allows us to save to named scripts.
    var btnSave = document.getElementById("btn-save");
    if (btnSave) {
      btnSave.addEventListener("click", function(event) {
        var selectEl = getSelectEl();
        var activeScript = selectEl ? selectEl.value : "";
        if (activeScript) {
          // Stop editor.js save handler from executing
          event.stopImmediatePropagation();
          event.preventDefault();

          if (window.editor && window.pywebview && window.pywebview.api && window.pywebview.api.save_script) {
            var code = window.editor.getValue();
            window.pywebview.api.save_script(activeScript, code);
            setStatus("Saving script '" + activeScript + "'...", "ok");
          }
        }
      }, true); // useCapture = true ensures this fires first!
    }
  });

})();
