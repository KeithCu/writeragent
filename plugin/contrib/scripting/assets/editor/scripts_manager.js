// =========================================================================================
// WARNING: PARITY INVARIANT WITH NATIVE PYTHON IMPLEMENTATION
// If you modify script actions, dropdown logic, templates, or origin handling in this file,
// you MUST also update the corresponding Python / XDL native implementations:
//   - Python Controller & Listeners: plugin/scripting/python_runner_ui.py
//   - Document Scripts & Data:      plugin/scripting/document_scripts.py
//   - Localized Strings Catalog:    plugin/scripting/editor_ui_strings.py
//   - Native Dialog Layout:         extension/Dialogs/PythonScriptDialog.xdl
//   - Native New Script Dialog:     extension/Dialogs/NewScriptDialog.xdl
// =========================================================================================

(function() {
  "use strict";

  var scriptSections = [];
  var scriptIndex = {};
  var currentSelectedName = "";
  var currentOrigin = "user";
  var selectedScriptName = "";
  var currentMode = "";
  var syncDropdownOnly = false;
  var documentAvailable = false;
  var documentReadonly = false;
  var documentStale = false;
  var initialRequested = false;

  function uiApi() {
    return window.waEditorUi || null;
  }

  function t(key, fallback) {
    var api = uiApi();
    if (api && api.t) {
      return api.t(key, fallback);
    }
    return fallback;
  }

  function fmt(key) {
    var api = uiApi();
    var args = arguments;
    if (api && api.fmt) {
      return api.fmt.apply(api, args);
    }
    var template = t(key, "");
    return template.replace(/\{(\d+)\}/g, function (_, index) {
      var argIndex = parseInt(index, 10) + 1;
      return args[argIndex] !== undefined ? args[argIndex] : "";
    });
  }

  function setStatus(text, kind) {
    var api = uiApi();
    if (api && api.setStatus) {
      api.setStatus(text, kind);
      return;
    }
    var el = document.getElementById("status");
    if (el) {
      el.value = "Status: " + text;
      el.classList.remove("status-ok", "status-error");
      if (kind === "ok") el.classList.add("status-ok");
      if (kind === "error") el.classList.add("status-error");
    }
  }

  // WARNING: If you change this starter template, also update starter_code in:
  // plugin/scripting/python_runner_ui.py (_NewListener)
  var NEW_SCRIPT_TEMPLATE = '# A simple script\nresult = "Hello from Python!"\n';

  function applyScriptManagerChrome() {
    var btnNew = document.getElementById("btn-new-script");
    if (btnNew) {
      btnNew.textContent = t("new_label", "New");
      btnNew.title = t("new_script_title", "New Python Script");
    }

    var scriptLabel = document.querySelector('label[for="script-select"]');
    if (scriptLabel) {
      scriptLabel.textContent = t("script_label", "Script:");
    }

    var saveAsBtn = document.getElementById("btn-save-as");
    if (saveAsBtn) {
      saveAsBtn.textContent = t("save_as_label", "Save As...");
    }

    var deleteBtn = getDeleteBtn();
    if (deleteBtn) {
      deleteBtn.textContent = t("delete_label", "Delete");
    }

    var modalTitle = document.getElementById("new-script-modal-title");
    if (modalTitle) {
      modalTitle.textContent = t("new_script_title", "New Python Script");
    }
    var nameLabel = document.getElementById("new-script-name-label");
    if (nameLabel) {
      nameLabel.textContent = t("script_name_label", "Script name:");
    }
    var attachLabel = document.getElementById("new-script-attach-label");
    if (attachLabel) {
      attachLabel.textContent = t("attach_to_document_label", "Attach to this document");
    }
    var btnCreate = document.getElementById("btn-new-script-create");
    if (btnCreate) {
      btnCreate.textContent = t("create_label", "Create");
    }
    var btnCancelModal = document.getElementById("btn-new-script-cancel");
    if (btnCancelModal) {
      btnCancelModal.textContent = t("cancel_label", "Cancel");
    }
  }

  function getSelectEl() {
    return document.getElementById("script-select");
  }

  function getDeleteBtn() {
    return document.getElementById("btn-delete-script");
  }

  function getManagerContainer() {
    return document.getElementById("script-manager-container");
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
    return [{ id: "user", title: t("my_scripts_fallback", "My Scripts"), scripts: scripts || {} }];
  }

  function applyScriptsList(msg) {
    if (msg.sections && msg.sections.length) {
      rebuildScriptIndex(msg.sections);
    } else if (msg.scripts) {
      rebuildScriptIndex(legacyScriptsToSections(msg.scripts));
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
    return origin !== "user" && origin !== "document" && origin !== "";
  }

  function builtInHelperReadOnlyMessage() {
    return t(
      "builtin_readonly",
      "Built-in helpers are read-only. Use Copy to My Scripts to customize."
    );
  }

  function updateToolbarState() {
    if (documentStale) {
      setStatus(
        t(
          "document_stale",
          "Document changed — close and reopen Run Python Script to edit document scripts."
        ),
        "error"
      );
    }
  }

  function handleScriptsManagerMessages(msg) {
    if (!msg) return;

    if (msg.type === "load") {
      if (window.waEditorUi && window.waEditorUi.applyUiFromLoad) {
        window.waEditorUi.applyUiFromLoad(msg);
      }
      applyScriptManagerChrome();

      currentMode = msg.mode || "calc_cell";
      var isRunScript = currentMode === "run_script";
      var container = getManagerContainer();
      if (container) {
        container.classList.toggle("toolbar-hidden", !isRunScript);
      }
      var btnNew = document.getElementById("btn-new-script");
      if (btnNew) {
        btnNew.classList.toggle("toolbar-hidden", !isRunScript);
      }
      if (isRunScript) {
        if (typeof msg.selected_script_name === "string") {
          selectedScriptName = msg.selected_script_name;
        }
        if (window.pywebview && window.pywebview.api && window.pywebview.api.request_scripts) {
          window.pywebview.api.request_scripts();
          initialRequested = true;
        }
      } else {
        selectedScriptName = "";
        currentSelectedName = "";
        var select = getSelectEl();
        if (select) {
          select.innerHTML = "";
          select.value = "";
        }
        closeScriptModal();
      }
    } else if (msg.type === "scripts_list") {
      applyScriptsList(msg);
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

    select.innerHTML = "";

    for (var s = 0; s < scriptSections.length; s++) {
      var section = scriptSections[s];
      var scripts = section.scripts || {};
      var names = Object.keys(scripts).sort();
      if (!names.length) {
        continue;
      }
      var group = document.createElement("optgroup");
      group.label = section.title || section.id || t("scripts_fallback", "Scripts");
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
      if (selectedScriptName && scriptIndex[selectedScriptName]) {
        select.value = selectedScriptName;
        currentOrigin = scriptIndex[selectedScriptName].origin;
      } else {
        var firstOption = select.querySelector("option");
        if (firstOption && firstOption.value && scriptIndex[firstOption.value]) {
          select.value = firstOption.value;
          currentOrigin = scriptIndex[firstOption.value].origin;
        } else {
          select.value = "";
          currentOrigin = "user";
        }
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
  }  // WARNING: If you change dropdown selection logic, also update _ScriptSelectListener in:
  // plugin/scripting/python_runner_ui.py
  function onDropdownChange() {
    var select = getSelectEl();
    if (!select) return;

    var name = select.value;
    currentSelectedName = name;
    var selectedOpt = select.options[select.selectedIndex];
    if (name && scriptIndex[name]) {
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
        setStatus(fmt("loaded_script", name), "ok");
      }
      setDataBindingVisible(currentOrigin === "analysis");
    } else {
      setDataBindingVisible(false);
    }
    if (window.pywebview && window.pywebview.api && window.pywebview.api.select_script) {
      window.pywebview.api.select_script(name || "");
    }
  }

  // Bugfix: scripts_list keys This Document with the [Doc] display name
  // (document_script_display_name / DOC_SCRIPT_DISPLAY_PREFIX). New / Save As
  // still passes the raw name the user typed. That lookup missed the row, so
  // confirm() never ran and save_document_script replaced the existing script.
  // A name that is already a display key (Save As prefills the dropdown value)
  // is left unchanged. My Scripts keys stay the raw name.
  function documentScriptListKey(name) {
    var prefix = "[Doc] ";
    return name.indexOf(prefix) === 0 ? name : prefix + name;
  }

  function scriptExistsInSection(sectionId, name) {
    var lookup = sectionId === "document" ? documentScriptListKey(name) : name;
    for (var s = 0; s < scriptSections.length; s++) {
      if (scriptSections[s].id === sectionId) {
        var scripts = scriptSections[s].scripts || {};
        return scripts[lookup] !== undefined;
      }
    }
    return false;
  }

  var currentModalAction = "new";

  function getNewScriptModal() {
    return document.getElementById("new-script-modal");
  }

  function openScriptModal(action) {
    currentModalAction = action || "new";
    var modal = getNewScriptModal();
    if (!modal) return;
    var nameInput = document.getElementById("new-script-name-input");
    var attachCheck = document.getElementById("new-script-attach-check");
    var modalTitle = document.getElementById("new-script-modal-title");
    var btnCreate = document.getElementById("btn-new-script-create");
    var canWriteDocument = documentAvailable && !documentReadonly && !documentStale;

    if (modalTitle) {
      modalTitle.textContent = currentModalAction === "save_as"
        ? t("save_as_title", "Save Script As")
        : t("new_script_title", "New Python Script");
    }
    if (btnCreate) {
      btnCreate.textContent = currentModalAction === "save_as"
        ? t("save_label", "Save")
        : t("create_label", "Create");
    }
    if (nameInput) {
      nameInput.value = currentModalAction === "save_as" ? (currentSelectedName || "") : "";
    }
    if (attachCheck) {
      if (currentModalAction === "save_as") {
        attachCheck.checked = (currentOrigin === "document") && canWriteDocument;
      } else {
        attachCheck.checked = canWriteDocument;
      }
      attachCheck.disabled = !canWriteDocument;
    }
    modal.classList.remove("toolbar-hidden");
    if (nameInput) {
      setTimeout(function() {
        nameInput.focus();
        if (currentModalAction === "save_as") {
          nameInput.select();
        }
      }, 50);
    }
  }

  function closeScriptModal() {
    var modal = getNewScriptModal();
    if (modal) {
      modal.classList.add("toolbar-hidden");
    }
    if (window.editor) {
      window.editor.focus();
    }
  }

  // WARNING: If you change script creation/save-as modal logic, also update:
  // plugin/scripting/python_runner_ui.py (_NewListener, _SaveAsListener)
  // and dialog layout in extension/Dialogs/NewScriptDialog.xdl
  function onConfirmScriptModal() {
    var nameInput = document.getElementById("new-script-name-input");
    var name = nameInput ? nameInput.value.trim() : "";
    if (!name) {
      setStatus(t("script_name_required", "Script name cannot be empty."), "error");
      if (nameInput) nameInput.focus();
      return;
    }
    var attachCheck = document.getElementById("new-script-attach-check");
    var origin = (attachCheck && attachCheck.checked && documentAvailable && !documentReadonly && !documentStale)
      ? "document"
      : "user";

    var overwrite = scriptExistsInSection(origin, name);
    if (overwrite) {
      var msgKey = origin === "document" ? "attach_overwrite_confirm" : "copy_overwrite_confirm";
      if (!confirm(fmt(msgKey, name))) {
        return;
      }
    }

    var code = (currentModalAction === "new")
      ? NEW_SCRIPT_TEMPLATE
      : (window.editor ? window.editor.getValue() : "");

    if (window.editor && currentModalAction === "new") {
      window.editor.setValue(code);
    }
    currentSelectedName = name;
    currentOrigin = origin;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_script) {
      window.pywebview.api.save_script(name, code, origin);
      setStatus(fmt("saving_script", name), "ok");
    }
    closeScriptModal();
  }

  // WARNING: If you change Save As logic, also update _SaveAsListener in:
  // plugin/scripting/python_runner_ui.py
  function onSaveAs() {
    openScriptModal("save_as");
  }

  // WARNING: If you change Delete logic, also update _DeleteListener in:
  // plugin/scripting/python_runner_ui.py
  function onDeleteScript() {
    var name = currentSelectedName;
    if (!name) {
      return;
    }

    if (confirm(fmt("delete_confirm", name))) {
      if (scriptIndex[name] && isBuiltInHelperOrigin(scriptIndex[name].origin)) {
        setStatus(t("builtin_cannot_delete", "Built-in helpers cannot be deleted."), "error");
        return;
      }
      if (window.pywebview && window.pywebview.api && window.pywebview.api.delete_script) {
        var origin = scriptIndex[name] ? scriptIndex[name].origin : currentOrigin;
        currentSelectedName = "";
        currentOrigin = "user";
        window.pywebview.api.delete_script(name, origin);
        setStatus(fmt("deleting_script", name), "ok");
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
      var isRunScript = (currentMode === "run_script") || (btnRun && !btnRun.classList.contains("toolbar-hidden"));
      if (isRunScript) {
        var container = getManagerContainer();
        if (container) container.classList.remove("toolbar-hidden");
        var btnNew = document.getElementById("btn-new-script");
        if (btnNew) btnNew.classList.remove("toolbar-hidden");
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
    applyScriptManagerChrome();

    var btnNew = document.getElementById("btn-new-script");
    if (btnNew) {
      btnNew.addEventListener("click", function() {
        openScriptModal("new");
      });
    }

    var btnCreate = document.getElementById("btn-new-script-create");
    if (btnCreate) {
      btnCreate.addEventListener("click", onConfirmScriptModal);
    }

    var btnCancelModal = document.getElementById("btn-new-script-cancel");
    if (btnCancelModal) {
      btnCancelModal.addEventListener("click", closeScriptModal);
    }

    var nameInput = document.getElementById("new-script-name-input");
    if (nameInput) {
      nameInput.addEventListener("keydown", function(e) {
        if (e.key === "Enter") {
          e.preventDefault();
          onConfirmScriptModal();
        } else if (e.key === "Escape") {
          e.preventDefault();
          closeScriptModal();
        }
      });
    }

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

    var btnSave = document.getElementById("btn-save");
    if (btnSave) {
      btnSave.addEventListener("click", function(event) {
        var container = getManagerContainer();
        var isRunScriptActive = currentMode === "run_script" && container && !container.classList.contains("toolbar-hidden");
        if (!isRunScriptActive) {
          return;
        }
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
            setStatus(fmt("saving_script", activeScript), "ok");
          }
        }
      }, true);
    }
  });

})();
