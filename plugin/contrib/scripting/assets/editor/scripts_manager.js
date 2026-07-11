(function() {
  "use strict";

  var scriptSections = [];
  var scriptIndex = {};
  var currentSelectedName = "";
  var currentOrigin = "sample";
  var sampleCode = "";
  var selectedScriptName = "";
  var syncDropdownOnly = false;
  var documentAvailable = false;
  var documentReadonly = false;
  var documentStale = false;
  var initialRequested = false;

  function getSelectEl() {
    return document.getElementById("script-select");
  }

  function getDeleteBtn() {
    return document.getElementById("btn-delete-script");
  }

  function getAttachBtn() {
    return document.getElementById("btn-attach-script");
  }

  function getCopyBtn() {
    return document.getElementById("btn-copy-to-user");
  }

  function getManagerContainer() {
    return document.getElementById("script-manager-container");
  }

  function setStatus(text, kind) {
    var el = document.getElementById("status");
    if (el) {
      var label = text || "Ready";
      if (label.indexOf("Status: ") !== 0) {
        label = "Status: " + label;
      }
      el.value = label;
      el.className = "";
      if (kind === "ok") el.classList.add("status-ok");
      if (kind === "error") el.classList.add("status-error");
    }
  }

  function rebuildScriptIndex(sections) {
    scriptIndex = {};
    scriptSections = sections || [];
    for (var s = 0; s < scriptSections.length; s++) {
      var section = scriptSections[s];
      var scripts = section.scripts || {};
      var names = Object.keys(scripts);
      for (var i = 0; i < names.length; i++) {
        var name = names[i];
        scriptIndex[name] = { code: scripts[name], origin: section.id || "user" };
      }
    }
  }

  function legacyScriptsToSections(scripts) {
    return [{ id: "user", title: "My Scripts", scripts: scripts || {} }];
  }

  function applyScriptsList(msg) {
    if (msg.sections && msg.sections.length) {
      rebuildScriptIndex(msg.sections);
    } else if (msg.scripts) {
      rebuildScriptIndex(legacyScriptsToSections(msg.scripts));
    }
    if (typeof msg.sample_code === "string") {
      sampleCode = msg.sample_code;
    }
    if (typeof msg.selected_script_name === "string") {
      selectedScriptName = msg.selected_script_name;
    }
    documentAvailable = !!msg.document_available;
    documentReadonly = !!msg.document_readonly;
    documentStale = !!msg.document_stale;
    syncDropdownOnly = true;
    updateToolbarState();
    updateDropdown();
    if (msg.status_ok_text) {
      setStatus(msg.status_ok_text, "ok");
    }
    if (msg.status_error_text) {
      setStatus(msg.status_error_text, "error");
    }
  }

  function setDataBindingVisible(visible) {
    var label = document.getElementById("data-binding-label");
    var input = document.getElementById("data-binding-input");
    if (label) {
      label.classList.toggle("toolbar-hidden", !visible);
    }
    if (input) {
      input.classList.toggle("toolbar-hidden", !visible);
    }
  }

  function isBuiltInHelperOrigin(origin) {
    return origin === "analysis" || origin === "vision";
  }

  function builtInHelperReadOnlyMessage() {
    return "Built-in helpers are read-only. Use Copy to My Scripts to customize.";
  }

  function updateToolbarState() {
    var attachBtn = getAttachBtn();
    var copyBtn = getCopyBtn();
    var canWriteDocument = documentAvailable && !documentReadonly && !documentStale;
    var isBuiltInHelper = isBuiltInHelperOrigin(currentOrigin);
    if (attachBtn) {
      attachBtn.disabled = !canWriteDocument || isBuiltInHelper;
      attachBtn.classList.toggle("toolbar-disabled", attachBtn.disabled);
    }
    if (copyBtn) {
      copyBtn.disabled = (currentOrigin !== "document" && !isBuiltInHelperOrigin(currentOrigin)) || !currentSelectedName;
      copyBtn.classList.toggle("toolbar-disabled", copyBtn.disabled);
    }
    if (documentStale) {
      setStatus("Document changed — close and reopen Run Python Script to edit document scripts.", "error");
    }
  }

  function handleScriptsManagerMessages(msg) {
    if (!msg) return;

    if (msg.type === "load") {
      var isRunScript = msg.mode === "run_script";
      var container = getManagerContainer();
      if (container) {
        container.classList.toggle("toolbar-hidden", !isRunScript);
      }
      if (isRunScript) {
        if (typeof msg.code === "string") {
          sampleCode = msg.code;
        }
        if (typeof msg.selected_script_name === "string") {
          selectedScriptName = msg.selected_script_name;
        }
        if (window.pywebview && window.pywebview.api && window.pywebview.api.request_scripts) {
          window.pywebview.api.request_scripts();
          initialRequested = true;
        }
      }
    } else if (msg.type === "scripts_list") {
      applyScriptsList(msg);
    } else if (msg.type === "saved" && currentOrigin === "sample" && window.editor) {
      sampleCode = window.editor.getValue();
    }
  }

  window.handleScriptsManagerMessage = function(msg) {
    if (Array.isArray(msg)) {
      for (var i = 0; i < msg.length; i++) {
        handleScriptsManagerMessages(msg[i]);
      }
    } else {
      handleScriptsManagerMessages(msg);
    }
  };

  function updateDropdown() {
    var select = getSelectEl();
    if (!select) return;

    var lastVal = currentSelectedName || select.value || "";
    if (!lastVal && selectedScriptName) {
      lastVal = selectedScriptName;
    }
    var lastOrigin = currentOrigin;

    select.innerHTML = "";

    for (var s = 0; s < scriptSections.length; s++) {
      var section = scriptSections[s];
      var scripts = section.scripts || {};
      var names = Object.keys(scripts).sort();
      if (!names.length) {
        continue;
      }
      var group = document.createElement("optgroup");
      group.label = section.title || section.id || "Scripts";
      for (var i = 0; i < names.length; i++) {
        var name = names[i];
        var opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        opt.dataset.origin = section.id || "user";
        group.appendChild(opt);
      }
      select.appendChild(group);
    }

    var restored = false;
    if (lastVal && scriptIndex[lastVal]) {
      select.value = lastVal;
      currentOrigin = scriptIndex[lastVal].origin;
      restored = true;
    }
    if (!restored) {
      var firstOpt = select.querySelector("option");
      if (firstOpt) {
        select.value = firstOpt.value;
        currentOrigin = firstOpt.dataset.origin || "user";
      } else {
        select.value = "";
        currentOrigin = "user";
      }
    }
    currentSelectedName = select.value;
    updateDeleteButtonVisibility();
    updateToolbarState();
    if (syncDropdownOnly) {
      syncDropdownOnly = false;
    }
  }

  function updateDeleteButtonVisibility() {
    var deleteBtn = getDeleteBtn();
    if (deleteBtn) {
      deleteBtn.classList.toggle("toolbar-hidden", false);
    }
  }

  function onDropdownChange() {
    var select = getSelectEl();
    if (!select) return;

    var name = select.value;
    currentSelectedName = name;
    var selectedOpt = select.options[select.selectedIndex];
    if (!name) {
      currentOrigin = "sample";
    } else if (scriptIndex[name]) {
      currentOrigin = scriptIndex[name].origin;
    } else if (selectedOpt && selectedOpt.dataset && selectedOpt.dataset.origin) {
      currentOrigin = selectedOpt.dataset.origin;
    } else {
      currentOrigin = "user";
    }
    updateToolbarState();

    if (name && scriptIndex[name] !== undefined) {
      if (window.editor) {
        window.editor.setValue(scriptIndex[name].code);
        setStatus("Loaded script '" + name + "'.", "ok");
      }
      setDataBindingVisible(currentOrigin === "analysis");
    } else if (!name) {
      if (window.editor) {
        window.editor.setValue(sampleCode || "");
        setStatus("Loaded Sample scratchpad.", "ok");
      }
      setDataBindingVisible(false);
    } else {
      setDataBindingVisible(false);
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.select_script) {
      window.pywebview.api.select_script(name || "");
    }
  }

  function scriptExistsInSection(sectionId, name) {
    for (var s = 0; s < scriptSections.length; s++) {
      if (scriptSections[s].id === sectionId) {
        var scripts = scriptSections[s].scripts || {};
        return scripts[name] !== undefined;
      }
    }
    return false;
  }

  function onAttach() {
    if (!documentAvailable || documentReadonly || documentStale) {
      setStatus("Cannot attach scripts to this document.", "error");
      return;
    }
    var defaultName = currentSelectedName || "";
    var name = prompt("Enter a name to attach this script to the document:\n(call it 'Init' to run it before any other Python code)", defaultName);
    if (!name) return;
    name = name.trim();
    if (!name) return;
    var overwrite = scriptExistsInSection("document", name);
    if (overwrite && !confirm("A script named '" + name + "' already exists in this document. Overwrite?")) {
      return;
    }
    if (window.editor && window.pywebview && window.pywebview.api && window.pywebview.api.attach_script) {
      var code = window.editor.getValue();
      currentSelectedName = name;
      currentOrigin = "document";
      window.pywebview.api.attach_script(name, code, overwrite);
      setStatus("Attaching script '" + name + "'...", "ok");
    }
  }

  function onCopyToUser() {
    if (!currentSelectedName || (currentOrigin !== "document" && !isBuiltInHelperOrigin(currentOrigin))) {
      return;
    }
    var name = prompt("Copy to My Scripts as:", currentSelectedName);
    if (!name) return;
    name = name.trim();
    if (!name) return;
    var overwrite = scriptExistsInSection("user", name);
    if (overwrite && !confirm("A script named '" + name + "' already exists in My Scripts. Overwrite?")) {
      return;
    }
    if (window.editor && window.pywebview && window.pywebview.api && window.pywebview.api.copy_script_to_user) {
      var code = window.editor.getValue();
      window.pywebview.api.copy_script_to_user(name, code, overwrite);
      setStatus("Copying script '" + name + "' to My Scripts...", "ok");
    }
  }

  function onSaveAs() {
    if (isBuiltInHelperOrigin(currentOrigin)) {
      setStatus(builtInHelperReadOnlyMessage(), "error");
      return;
    }
    var defaultName = currentSelectedName || "";
    var name = prompt("Enter a name for the script:", defaultName);
    if (!name) return;
    name = name.trim();
    if (!name) return;

    var origin = currentOrigin === "document" ? "document" : "user";
    if (documentAvailable && !documentReadonly && !documentStale && currentOrigin !== "document") {
      if (confirm("Save script '" + name + "' to this document?")) {
        origin = "document";
      }
    }

    if (window.editor && window.pywebview && window.pywebview.api && window.pywebview.api.save_script) {
      var code = window.editor.getValue();
      currentSelectedName = name;
      currentOrigin = origin;
      window.pywebview.api.save_script(name, code, origin);
      setStatus("Saving script '" + name + "'...", "ok");
    }
  }

  function onDeleteScript() {
    var select = getSelectEl();
    if (!select) return;

    var name = select.value;
    if (!name) {
      if (confirm("Are you sure you want to clear the Sample scratchpad?")) {
        if (window.editor) {
          window.editor.setValue("");
        }
        sampleCode = "";
        if (window.pywebview && window.pywebview.api && window.pywebview.api.notify_save_script) {
          window.pywebview.api.notify_save_script("");
        }
        setStatus("Cleared Sample scratchpad.", "ok");
      }
      return;
    }

    if (confirm("Are you sure you want to delete '" + name + "'?")) {
      if (scriptIndex[name] && isBuiltInHelperOrigin(scriptIndex[name].origin)) {
        setStatus("Built-in helpers cannot be deleted.", "error");
        return;
      }
      if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_script) {
        var origin = scriptIndex[name] ? scriptIndex[name].origin : currentOrigin;
        currentSelectedName = "";
        currentOrigin = "sample";
        window.pywebview.api.delete_script(name, origin);
        setStatus("Deleting script '" + name + "'...", "ok");
      }
    }
  }

  function setupInterception() {
    if (window.pywebview && window.pywebview.api) {
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

  ensureInitialRequest();
  var pollInterval = setInterval(function() {
    ensureInitialRequest();
    if (initialRequested) {
      clearInterval(pollInterval);
    }
  }, 100);

  document.addEventListener("DOMContentLoaded", function() {
    var select = getSelectEl();
    if (select) {
      select.addEventListener("change", onDropdownChange);
    }

    var btnSaveAs = document.getElementById("btn-save-as");
    if (btnSaveAs) {
      btnSaveAs.addEventListener("click", onSaveAs);
    }

    var btnAttach = getAttachBtn();
    if (btnAttach) {
      btnAttach.addEventListener("click", onAttach);
    }

    var btnCopy = getCopyBtn();
    if (btnCopy) {
      btnCopy.addEventListener("click", onCopyToUser);
    }

    var btnDelete = getDeleteBtn();
    if (btnDelete) {
      btnDelete.addEventListener("click", onDeleteScript);
    }

    var btnSave = document.getElementById("btn-save");
    if (btnSave) {
      btnSave.addEventListener("click", function(event) {
        var selectEl = getSelectEl();
        var activeScript = selectEl ? selectEl.value : "";
        if (activeScript) {
          event.stopImmediatePropagation();
          event.preventDefault();
          if (scriptIndex[activeScript] && isBuiltInHelperOrigin(scriptIndex[activeScript].origin)) {
            setStatus(builtInHelperReadOnlyMessage(), "error");
            return;
          }
          if (window.editor && window.pywebview && window.pywebview.api && window.pywebview.api.save_script) {
            var code = window.editor.getValue();
            var origin = scriptIndex[activeScript] ? scriptIndex[activeScript].origin : currentOrigin;
            window.pywebview.api.save_script(activeScript, code, origin);
            setStatus("Saving script '" + activeScript + "'...", "ok");
          }
        }
      }, true);
    }
  });

})();
