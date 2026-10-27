/**
 * WriterAgent Monaco backend for the pywebview editor shell (editor_shell.js).
 *
 * LaTeX Language support for Monaco Editor, adapted from:
 * - Source: https://github.com/domatex/monaco-latex
 * - Path: https://unpkg.com/monaco-latex/dist/src/tokenizer.js
 *
 * Backend interface (shared with future backends, e.g. CodeMirror 6). A backend
 * must not touch window.pywebview or UNO; the shell passes everything in.
 *   create(el, opts) -> Promise   opts: { value, language, getCompletions }
 *                                 getCompletions(code, line, column) -> Promise of
 *                                 { items: [{label, kind, insertText, detail,
 *                                 documentation}] } or null when unavailable.
 *                                 Rejects when the editor library cannot load.
 *   getValue() -> string
 *   setValue(text)
 *   setLanguage(lang)             "python" or "latex"
 *   setTheme(isDark, name)        name: backend-specific theme hint (may be empty)
 *   setMarkers(markers)           [{line, col, endLine, endCol, message, severity}]
 *                                 severity: "error" | "warning" | "info"
 *   onChange(cb)                  cb() on every content change
 *   focus()
 */

(function () {
  "use strict";

  var editor = null;
  var completeTimer = null;
  var completeSeq = 0;

  function registerLatex() {
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
  }

  function registerPythonCompletion(getCompletions) {
    monaco.languages.registerCompletionItemProvider("python", {
      triggerCharacters: ["."],
      provideCompletionItems: function (model, position, context, token) {
        if (!getCompletions) {
          return { suggestions: [] };
        }
        var code = model.getValue();
        var line = position.lineNumber;
        var column = position.column;
        var seq = ++completeSeq;
        return new Promise(function (resolve) {
          if (completeTimer) {
            clearTimeout(completeTimer);
          }
          completeTimer = setTimeout(function () {
            completeTimer = null;
            if (token && token.isCancellationRequested) {
              resolve({ suggestions: [] });
              return;
            }
            var pending = getCompletions(code, line, column);
            if (!pending) {
              resolve({ suggestions: [] });
              return;
            }
            pending.then(function (res) {
              if (seq !== completeSeq || (token && token.isCancellationRequested)) {
                resolve({ suggestions: [] });
                return;
              }
              resolve(res && res.items ? {
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
              } : { suggestions: [] });
            }).catch(function (err) {
              console.error("Jedi autocomplete error:", err);
              resolve({ suggestions: [] });
            });
          }, 150);
        });
      }
    });
  }

  var SEVERITY = { error: 8, warning: 4, info: 2 };

  var backend = {
    create: function (el, opts) {
      opts = opts || {};
      return new Promise(function (resolve, reject) {
        if (typeof require === "undefined") {
          reject(new Error("Monaco loader missing"));
          return;
        }
        require.config({ paths: { vs: "vs" } });
        require(["vs/editor/editor.main"], function () {
          registerLatex();
          registerPythonCompletion(opts.getCompletions);
          editor = monaco.editor.create(el, {
            value: opts.value || "",
            language: opts.language || "python",
            theme: "vs",
            automaticLayout: true,
            minimap: { enabled: false },
            fontSize: 13,
            scrollBeyondLastLine: false
          });
          resolve(backend);
        }, reject);
      });
    },
    getValue: function () {
      return editor ? editor.getValue() : "";
    },
    setValue: function (text) {
      if (editor) {
        editor.setValue(text);
      }
    },
    setLanguage: function (lang) {
      if (editor) {
        monaco.editor.setModelLanguage(editor.getModel(), lang || "python");
      }
    },
    setTheme: function (isDark, name) {
      try {
        monaco.editor.setTheme(name || (isDark ? "vs-dark" : "vs"));
      } catch (e) {
        // Monaco may not be fully ready; ignore, creation default is vs
      }
    },
    setMarkers: function (markers) {
      if (!editor) {
        return;
      }
      monaco.editor.setModelMarkers(editor.getModel(), "writeragent", (markers || []).map(function (m) {
        return {
          startLineNumber: m.line,
          startColumn: m.col || 1,
          endLineNumber: m.endLine || m.line,
          endColumn: m.endCol || editor.getModel().getLineMaxColumn(m.endLine || m.line),
          message: m.message || "",
          severity: SEVERITY[m.severity] || SEVERITY.error
        };
      }));
    },
    onChange: function (cb) {
      if (editor) {
        editor.onDidChangeModelContent(function () {
          cb();
        });
      }
    },
    focus: function () {
      if (editor) {
        editor.focus();
      }
    }
  };

  window.WaEditorBackends = window.WaEditorBackends || {};
  window.WaEditorBackends.monaco = backend;
})();
