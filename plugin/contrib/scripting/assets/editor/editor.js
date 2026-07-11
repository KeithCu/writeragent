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
  var defaultStatusOkText = "Saved.";
  var pendingStatusText = "Saving…";
  var currentMode = "calc_cell";
  var dataBindingTitle = "Calc injects `data` and `data_list` from these range(s) at runtime.";

  function setStatus(text, kind) {
    var el = document.getElementById("status");
    if (el) {
      var label = text || "Ready";
      if (label.indexOf("Status: ") !== 0) {
        label = "Status: " + label;
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
    // Make the HTML chrome (toolbar, inputs, script picker) follow LO.
    // Monaco itself is handled via monaco.editor.setTheme.
    if (!tinfo) return;
    var isDark = !!(tinfo.is_dark || (tinfo.monaco && tinfo.monaco.indexOf("dark") !== -1));
    document.body.classList.toggle("dark", isDark);
    document.body.classList.toggle("light", !isDark);

    // If richer colors were provided in future, we could set CSS vars here, e.g.:
    // if (tinfo.bg != null) { document.documentElement.style.setProperty('--wa-bg', '#' + tinfo.bg.toString(16).padStart(6,'0')); }
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

  function setToolbarVisible(elementId, visible) {
    var el = document.getElementById(elementId);
    if (el) {
      el.classList.toggle("toolbar-hidden", !visible);
    }
  }

  function applyLoadMessage(msg) {
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

    if (msg.plain_text_label) {
      var labelEl = document.getElementById("plain-text-save-text");
      if (labelEl) {
        labelEl.textContent = msg.plain_text_label;
      }
    }

    var runBtn = document.getElementById("btn-run");
    if (runBtn && msg.run_label) {
      runBtn.textContent = msg.run_label;
    }

    var plainEl = getPlainTextCheckbox();
    if (plainEl && typeof msg.save_as_plain === "boolean") {
      plainEl.checked = msg.save_as_plain;
    }

    var saveBtn = document.getElementById("btn-save");
    if (saveBtn && msg.save_label) {
      saveBtn.textContent = msg.save_label;
    }

    var closeBtn = document.getElementById("btn-cancel");
    if (closeBtn) {
      closeBtn.textContent = msg.close_label || (isRunScript ? "Close" : "Cancel");
    }

    if (msg.saved_ok_text) {
      defaultStatusOkText = msg.saved_ok_text;
    } else if (msg.status_ok_text && isRunScript || msg.status_ok_text) {
      defaultStatusOkText = msg.status_ok_text;
    }

    pendingStatusText = isRunScript ? "Running…" : "Saving…";
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
      setStatus("Ready", "");
    }

    // Apply LO theme (Monaco + our toolbar chrome). Sent on every load so
    // switching cells or re-opening sees the current LO appearance.
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
      input.title = disabled ? "Data ranges apply only when saving as a =PYTHON() formula." : dataBindingTitle;
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
                if (msg.save_as_plain && !msg.status_ok_text) {
                  okText = "Saved as plain text.";
                }
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

  document.getElementById("btn-run").addEventListener("click", function () {
    var code = getEditorCode();
    if (window.pywebview && window.pywebview.api && window.pywebview.api.notify_run) {
      window.pywebview.api.notify_run(code);
      setStatus("Running…", "");
    }
  });

  document.getElementById("btn-save").addEventListener("click", function () {
    var code = getEditorCode();
    if (window.pywebview && window.pywebview.api) {
      if (currentMode === "run_script" && window.pywebview.api.notify_save_script) {
        window.pywebview.api.notify_save_script(code);
        setStatus("Saving…", "");
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

  var dataInput = getDataBindingInput();
  if (dataInput) {
    dataInput.title = dataBindingTitle;
  }

  updateDataBindingEnabled();

  if (typeof require !== "undefined") {
    require.config({ paths: { vs: "vs" } });
    require(["vs/editor/editor.main"], function () {
      // Register LaTeX language support
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
    setStatus("Monaco loader missing.", "error");
  }
})();