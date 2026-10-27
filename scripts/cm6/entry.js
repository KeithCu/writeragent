// Entry point for cm6.bundle.js. Exposes the window.CM6 global used by
// backend_cm6.js. Keep this list small: Python plus the LaTeX (stex) legacy
// mode only. No pywebview or UNO code belongs here.
export { basicSetup, minimalSetup } from "codemirror";
export { EditorState, Compartment, StateEffect, StateField } from "@codemirror/state";
export { EditorView, keymap, placeholder } from "@codemirror/view";
export { indentWithTab, defaultKeymap, history, historyKeymap } from "@codemirror/commands";
export {
  indentUnit,
  StreamLanguage,
  syntaxHighlighting,
  defaultHighlightStyle,
  LanguageSupport,
} from "@codemirror/language";
export {
  autocompletion,
  completionKeymap,
  startCompletion,
  closeCompletion,
} from "@codemirror/autocomplete";
export { linter, lintGutter, setDiagnostics, forceLinting } from "@codemirror/lint";
export { search, searchKeymap } from "@codemirror/search";
export { python, pythonLanguage } from "@codemirror/lang-python";
export { stex } from "@codemirror/legacy-modes/mode/stex";
export { oneDark } from "@codemirror/theme-one-dark";
