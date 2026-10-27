/**
 * WriterAgent CodeMirror 6 backend for the pywebview editor shell (editor_shell.js).
 *
 * Same backend interface as backend_monaco.js. Uses the window.CM6 global
 * from cm6/cm6.bundle.js (built by scripts/build_cm6_bundle.sh). A backend
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

  var view = null;
  var languageCompartment = null;
  var themeCompartment = null;
  var changeCallbacks = [];

  function getLanguageExtension(CM, lang) {
    if (lang === "latex") {
      return CM.StreamLanguage.define(CM.stex);
    }
    return CM.python();
  }

  function mapCompletionType(rawKind) {
    var k = String(rawKind || "").toLowerCase();
    if (k === "method") return "method";
    if (k === "function") return "function";
    if (k === "class") return "class";
    if (k === "module") return "namespace";
    if (k === "property") return "property";
    if (k === "keyword" || k === "statement") return "keyword";
    if (k === "instance" || k === "param" || k === "variable") return "variable";
    return "text";
  }

  function convertMarkers(doc, markers) {
    if (!markers || !markers.length) {
      return [];
    }
    var totalLines = doc.lines;
    var list = [];
    for (var i = 0; i < markers.length; i++) {
      var m = markers[i];
      if (!m) continue;
      var lineNum = Math.max(1, Math.min(totalLines, m.line || 1));
      var endLineNum = Math.max(1, Math.min(totalLines, m.endLine || lineNum));
      var startLine = doc.line(lineNum);
      var endLine = doc.line(endLineNum);
      var from;
      var to;
      if (m.col === undefined || m.col === null) {
        from = startLine.from;
      } else {
        from = Math.min(startLine.to, startLine.from + Math.max(0, m.col - 1));
      }
      if (m.endCol === undefined || m.endCol === null) {
        to = endLine.to;
      } else {
        to = Math.min(endLine.to, endLine.from + Math.max(0, m.endCol - 1));
      }
      if (to < from) {
        to = from;
      }
      var sev = (m.severity === "warning" || m.severity === "info") ? m.severity : "error";
      list.push({
        from: from,
        to: to,
        severity: sev,
        message: m.message || ""
      });
    }
    return list;
  }

  var backend = {
    create: function (el, opts) {
      opts = opts || {};
      return new Promise(function (resolve, reject) {
        var CM = window.CM6 || (typeof CM6 !== "undefined" ? CM6 : null);
        if (!CM) {
          reject(new Error("CodeMirror 6 missing"));
          return;
        }

        languageCompartment = new CM.Compartment();
        themeCompartment = new CM.Compartment();

        function completionSource(context) {
          if (!opts.getCompletions) {
            return null;
          }
          var word = context.matchBefore(/[\w$]*$/);
          if (!word && !context.explicit) {
            return null;
          }
          var lineObj = context.state.doc.lineAt(context.pos);
          var line = lineObj.number;
          var column = context.pos - lineObj.from + 1;
          var code = context.state.doc.toString();
          var pending = opts.getCompletions(code, line, column);
          if (!pending) {
            return null;
          }
          return Promise.resolve(pending).then(function (res) {
            if (!res || !res.items || !res.items.length) {
              return null;
            }
            var from = word ? word.from : context.pos;
            var options = res.items.map(function (item) {
              return {
                label: item.label,
                type: mapCompletionType(item.kind),
                apply: item.insertText !== undefined ? item.insertText : item.label,
                detail: item.detail || ""
              };
            });
            return {
              from: from,
              options: options
            };
          }).catch(function (err) {
            console.error("Jedi autocomplete error:", err);
            return null;
          });
        }

        var extensions = [
          CM.basicSetup,
          CM.keymap.of([CM.indentWithTab]),
          CM.indentUnit.of("    "),
          languageCompartment.of(getLanguageExtension(CM, opts.language)),
          themeCompartment.of([]),
          // Fill #editor like Monaco's automaticLayout; scroll inside the editor.
          CM.EditorView.theme({ "&": { height: "100%" }, ".cm-scroller": { overflow: "auto" } }),
          CM.lintGutter(),
          CM.autocompletion({ override: [completionSource] }),
          CM.EditorView.updateListener.of(function (update) {
            if (update.docChanged) {
              for (var i = 0; i < changeCallbacks.length; i++) {
                changeCallbacks[i]();
              }
            }
          })
        ];

        try {
          view = new CM.EditorView({
            doc: opts.value || "",
            extensions: extensions,
            parent: el
          });
          resolve(backend);
        } catch (err) {
          reject(err);
        }
      });
    },
    getValue: function () {
      return view ? view.state.doc.toString() : "";
    },
    setValue: function (text) {
      if (view) {
        view.dispatch({
          changes: { from: 0, to: view.state.doc.length, insert: text || "" }
        });
      }
    },
    setLanguage: function (lang) {
      var CM = window.CM6 || (typeof CM6 !== "undefined" ? CM6 : null);
      if (view && languageCompartment && CM) {
        view.dispatch({
          effects: languageCompartment.reconfigure(getLanguageExtension(CM, lang))
        });
      }
    },
    setTheme: function (isDark, name) {
      var CM = window.CM6 || (typeof CM6 !== "undefined" ? CM6 : null);
      if (view && themeCompartment && CM) {
        try {
          view.dispatch({
            effects: themeCompartment.reconfigure(isDark ? CM.oneDark : [])
          });
        } catch (e) {}
      }
    },
    setMarkers: function (markers) {
      var CM = window.CM6 || (typeof CM6 !== "undefined" ? CM6 : null);
      if (!view || !CM) {
        return;
      }
      var diagnostics = convertMarkers(view.state.doc, markers);
      view.dispatch(CM.setDiagnostics(view.state, diagnostics));
    },
    onChange: function (cb) {
      if (typeof cb === "function") {
        changeCallbacks.push(cb);
      }
    },
    focus: function () {
      if (view) {
        view.focus();
      }
    }
  };

  window.WaEditorBackends = window.WaEditorBackends || {};
  window.WaEditorBackends.codemirror = backend;
})();
