/**
 * WriterAgent Monaco Editor Host Integration
 *
 * LaTeX Language support for Monaco Editor, adapted from:
 * - Source: https://github.com/domatex/monaco-latex
 * - Path: https://unpkg.com/monaco-latex/dist/src/tokenizer.js
 */

(function () {
  "use strict";

  var editor = null;
  var pendingCode = "";
  var ui = {};
  var defaultStatusOkText = "Saved.";
  var pendingStatusText = "Saving…";
  var currentMode = "calc_cell";
  var dataBindingTitle = "Calc injects `data` and `data_list` from these range(s) at runtime.";
  var dataBindingDisabledTitle = "Data ranges apply only when saving as a =PYTHON() formula.";

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
      "Calc injects `data` and `data_list` from these range(s) at runtime."
    );
    dataBindingDisabledTitle = t(
      "data_binding_disabled_title",
      "Data ranges apply only when saving as a =PYTHON() formula."
    );
    if (dataInput) {
      dataInput.title = dataBindingTitle;
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
    var isRunScript = currentMode === "run_script";

    setToolbarVisible("btn-run", isRunScript);
    setToolbarVisible("plain-text-save-label", showPlainText);
    setToolbarVisible("data-binding-label", showDataBinding);
    setToolbarVisible("data-binding-input", showDataBinding);

    applyUiChrome();

    var plainEl = getPlainTextCheckbox();
    if (plainEl && typeof msg.save_as_plain === "boolean") {
      plainEl.checked = msg.save_as_plain;
    }

    defaultStatusOkText = t("saved_default", t("status_ok_text", "Saved."));
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
      editor.setValue(pendingCode);
      monaco.editor.setModelLanguage(editor.getModel(), msg.language || "python");
      setStatus(t("ready", "Ready"), "");
    }

    if (msg.theme) {
      var monacoTheme = msg.theme.monaco || (msg.theme.is_dark ? "vs-dark" : "vs");
      try {
        monaco.editor.setTheme(monacoTheme);
      } catch (e) {
        // Monaco may not be fully ready; ignore, creation default is vs
      }
      applyTheme(msg.theme);
    }
  }

  function updateDataBindingEnabled() {
    var plainEl = getPlainTextCheckbox();
    var input = getDataBindingInput();
    var label = document.getElementById("data-binding-label");
    var disabled = !!plainEl && plainEl.checked;
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
              if (msg.type === "load") {
                applyLoadMessage(msg);
              } else if (msg.type === "saved") {
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
      window.pywebview.api.notify_run(code);
      setStatus(t("running", "Running…"), "");
    }
  });

  document.getElementById("btn-save").addEventListener("click", function () {
    var code = getEditorCode();
    if (window.pywebview && window.pywebview.api) {
      if (currentMode === "run_script" && window.pywebview.api.notify_save_script) {
        window.pywebview.api.notify_save_script(code);
        setStatus(t("saving", "Saving…"), "");
        return;
      }
      var plainEl = getPlainTextCheckbox();
      var saveAsPlain = !!plainEl && plainEl.checked;
      var input = getDataBindingInput();
      var dataBinding = saveAsPlain ? "" : (input ? input.value.trim() : "");
      window.pywebview.api.notify_save(code, saveAsPlain, dataBinding);
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
    window.close();
  });

  var plainCheckbox = getPlainTextCheckbox();
  if (plainCheckbox) {
    plainCheckbox.addEventListener("change", updateDataBindingEnabled);
  }

  updateDataBindingEnabled();

  if (typeof require !== "undefined") {
    require.config({ paths: { vs: "vs" } });
    require(["vs/editor/editor.main"], function () {
      monaco.languages.register({
        id: "latex",
        extensions: [".tex", ".sty", ".cls"],
        aliases: ["LaTeX", "latex", "tex"],
        mimetypes: ["text/latex", "text/tex"]
      });

      monaco.languages.setLanguageConfiguration("latex", {
        comments: { lineComment: "%" },
        brackets: [["{", "}"], ["[", "]"], ["(", ")"]],
        autoClosingPairs: [
          { open: "{", close: "}" },
          { open: "[", close: "]" },
          { open: "(", close: ")" },
          { open: "$", close: "$" }
        ],
        surroundingPairs: [
          { open: "{", close: "}" },
          { open: "[", close: "]" },
          { open: "(", close: ")" },
          { open: "$", close: "$" }
        ]
      });

      monaco.languages.setMonarchTokensProvider("latex", {
        defaultToken: "",
        tokenPostfix: ".latex",
        displayName: "LaTeX",
        name: "latex",
        mimeTypes: ["text/latex", "text/tex"],
        fileExtensions: ["tex", "sty", "cls"],
        lineComment: "% ",
        builtin: [
          "addcontentsline", "addtocontents", "addtocounter", "address", "addtolength",
          "addvspace", "alph", "appendix", "arabic", "author", "backslash", "baselineskip",
          "baselinestretch", "bf", "bibitem", "bigskipamount", "bigskip", "boldmath",
          "boldsymbol", "cal", "caption", "cdots", "centering", "chapter", "circle",
          "cite", "cleardoublepage", "clearpage", "cline", "closing", "color", "copyright",
          "dashbox", "date", "ddots", "documentclass", "domlinecolor", "dotfill", "em",
          "emph", "ensuremath", "epigraph", "euro", "fbox", "flushbottom", "fnsymbol",
          "footnote", "footnotemark", "footnotesize", "footnotetext", "frac", "frame",
          "framebox", "frenchspacing", "hfill", "hline", "href", "hrulefill", "hspace",
          "huge", "Huge", "hyphenation", "include", "includegraphics", "includeonly",
          "indent", "input", "it", "item", "kill", "label", "large", "Large", "LARGE",
          "LaTeX", "LaTeXe", "ldots", "left", "lefteqn", "line", "linebreak", "linethickness",
          "linewidth", "listoffigures", "listoftables", "location", "makebox", "maketitle",
          "markboth", "mathcal", "mathop", "mbox", "medskip", "multicolumn", "multiput",
          "newcommand", "newcolumntype", "newcounter", "newenvironment", "newfont",
          "newlength", "newline", "newpage", "newsavebox", "newtheorem", "nocite",
          "noindent", "nolinebreak", "nonfrenchspacing", "normalsize", "nopagebreak",
          "not", "onecolumn", "opening", "oval", "overbrace", "overline", "pagebreak",
          "pagenumbering", "pageref", "pagestyle", "par", "paragraph", "parbox",
          "parindent", "parskip", "part", "protect", "providecommand", "put",
          "raggedbottom", "raggedleft", "raggedright", "raisebox", "ref", "renewcommand",
          "right", "rm", "roman", "rule", "savebox", "sbox", "sc", "scriptsize",
          "section", "setcounter", "setlength", "settowidth", "sf", "shortstack",
          "signature", "sl", "slash", "small", "smallskip", "sout", "space", "sqrt",
          "stackrel", "stepcounter", "subparagraph", "subsection", "subsubsection",
          "tableofcontents", "telephone", "TeX", "textbf", "textcolor", "textit",
          "textmd", "textnormal", "textrm", "textsc", "textsf", "textsl", "texttt",
          "textup", "textwidth", "textheight", "thanks", "thispagestyle", "tiny",
          "title", "today", "tt", "twocolumn", "typeout", "typein", "uline",
          "underbrace", "underline", "unitlength", "usebox", "usecounter", "uwave",
          "value", "vbox", "vcenter", "vdots", "vector", "verb", "vfill", "vline",
          "vphantom", "vspace", "RequirePackage", "NeedsTeXFormat", "usepackage",
          "documentstyle", "def", "edef", "defcommand", "if", "ifdim", "ifnum", "ifx",
          "fi", "else", "begingroup", "endgroup", "definecolor", "eifstrequal", "eeifstrequal"
        ],
        tokenizer: {
          root: [
            [
              /(\\begin)(\s*)(\{)([\w\-\*\@]+)(\})/,
              [
                "keyword.predefined",
                "white",
                "@brackets",
                { token: "tag.env-$4", bracket: "@open" },
                "@brackets"
              ]
            ],
            [
              /(\\end)(\s*)(\{)([\w\-\*\@]+)(\})/,
              [
                "keyword.predefined",
                "white",
                "@brackets",
                { token: "tag.env-$4", bracket: "@close" },
                "@brackets"
              ]
            ],
            [/\\\\[^a-zA-Z@]/, "keyword"],
            [/\\@[a-zA-Z@]+/, "keyword.at"],
            [
              /\\([a-zA-Z@]+)/,
              {
                cases: {
                  "$1@builtin": "keyword.predefined",
                  "@default": "keyword"
                }
              }
            ],
            { include: "@whitespace" },
            [/[{}()\[\]]/, "@brackets"],
            [/#+\d/, "number.arg"],
            [/\-?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:em|ex|pt|pc|sp|cm|mm|in)/, "number.len"]
          ],
          whitespace: [
            [/[ \t\r\n]+/, "white"],
            [/%.*$/, "comment"]
          ]
        }
      });

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
            return res && res.items ? {
              suggestions: res.items.map(function (item) {
                var kind = monaco.languages.CompletionItemKind.Text;
                var k = String(item.kind).toLowerCase();
                if (k === "method") kind = monaco.languages.CompletionItemKind.Method;
                else if (k === "function") kind = monaco.languages.CompletionItemKind.Function;
                else if (k === "class") kind = monaco.languages.CompletionItemKind.Class;
                else if (k === "module") kind = monaco.languages.CompletionItemKind.Module;
                else if (k === "property") kind = monaco.languages.CompletionItemKind.Property;
                else if (k === "keyword" || k === "statement") kind = monaco.languages.CompletionItemKind.Keyword;
                else if (k === "instance" || k === "param" || k === "variable") kind = monaco.languages.CompletionItemKind.Variable;
                return {
                  label: item.label,
                  kind: kind,
                  insertText: item.insertText,
                  detail: item.detail || "",
                  documentation: item.documentation || ""
                };
              })
            } : { suggestions: [] };
          }).catch(function (err) {
            console.error("Jedi autocomplete error:", err);
            return { suggestions: [] };
          });
        }
      });

      window.editor = editor = monaco.editor.create(document.getElementById("editor"), {
        value: pendingCode,
        language: "python",
        theme: "vs",
        automaticLayout: true,
        minimap: { enabled: false },
        fontSize: 13,
        scrollBeyondLastLine: false
      });

      setInterval(pollMessages, 80);
      pollMessages();
    });
  } else {
    setStatus(t("monaco_loader_missing", "Monaco loader missing."), "error");
  }
})();
