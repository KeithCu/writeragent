# Loki Assistant: Technical Analysis & Integration Report

This document summarizes the technical findings from the `loki-assistent` directory, providing a deep dive into its implementation of advanced LibreOffice automation features. This report is intended for Product Managers and Senior Developers to evaluate potential features for integration into `WriterAgent`.

## Executive Summary

`Loki Assistant` is a mature LibreOffice extension that excels in **native UI integration** and **robust document automation**. While `WriterAgent` focuses on a modern sidebar-based chat experience, `Loki` provides a "native-first" approach using context menus, status bars, and programmatic shortcut management.

---

## 1. Text-to-Speech (TTS) Implementation

The TTS implementation is highly robust, featuring cross-platform support and a sophisticated "toggle" mechanism that persists across LibreOffice's transient macro execution environment.

### Technical Architecture
- **State Management**: Since LibreOffice can re-instantiate the extension class on every macro call, `Loki` uses a global lock and a persistent state file (`loki_tts_state.json`) in the system `/tmp` directory to track active processes and PIDs.
- **Debouncing**: Implements a `0.5s` monotonic clock debounce to prevent "key-repeat" flailing when the user holds down the toggle shortcut (Ctrl+L).

### Platform Backends
- **Windows**: Invokes PowerShell's `System.Speech.Synthesis.SpeechSynthesizer` via a hidden background process. It writes the source text to a temporary UTF-8 `.txt` file to avoid command-line length limits.
- **Linux**: Uses the `spd-say` (Speech Dispatcher) utility.
- **Audio Detection**: On Linux, it uses `pacmd list-sink-inputs` and `pgrep` to detect if speech is currently playing, allowing the toggle to work as a "Stop" button.

> [!NOTE]
> **Source**: `main.py:3231` (`toggle_speech`) and `main.py:3424` (`_is_voice_playback_running`).

---

## 2. Native UI Integration

### Dynamic Context Menus
Unlike static `Addons.xcu` entries, `Loki` uses the `XContextMenuInterceptor` interface.
- **Mechanism**: It registers a listener with the `XContextMenuInterception` of the active document controller.
- **Benefit**: This allows the extension to add or remove menu items on the fly based on the current text selection or AI service availability.
- **Code Reference**: `main.py:662` (`LokiContextMenuInterceptor`) and `main.py:6549` (`register_context_menu_interceptor`).

### Status Bar Feedback
For long-running AI operations, `Loki` utilizes the native LibreOffice status bar.
- **Implementation**: `frame.createStatusIndicator()` is used to show progress percentages or text updates without blocking the UI or requiring a modal dialog.
- **Code Reference**: `loki/core/document_handler.py:231` (`show_status_message`).

---

## 3. Advanced Graphic & Diagram Handling

### Robust Graphic Embedding
`WriterAgent` currently uses `GraphicURL` for images. `Loki` implements a more professional approach:
- **GraphicProvider**: It uses the `com.sun.star.graphic.GraphicProvider` service to programmatically convert a file path into an `XGraphic` object.
- **Embedding**: By assigning the `XGraphic` object directly, the image is **embedded** in the document, preventing broken links if the temporary source file is deleted.
- **Code Reference**: `main.py:638` (`_try_embed_graphic`).

### PlantUML Integration
`Loki` can generate and render diagrams natively.
- **Workflow**: The LLM is prompted to "return ONLY PlantUML code". This code is then passed to a rendering chain:
    1.  `java -jar plantuml.jar` (if available).
    2.  System `plantuml` CLI.
    3.  Pandoc with `pandoc-plantuml` filter.
- **Code Reference**: `main.py:322` (`_lo_render_plantuml_to_svg_or_png_file`) and `main.py:8922` (`GenerateImage` handler).

---

## 4. Service Integration (Pollinations API)

`Loki` includes a built-in provider for **Pollinations AI**, which is a zero-config, API-key-free alternative for both text and image generation.
- **Text**: `https://text.pollinations.ai`
- **Images**: `https://image.pollinations.ai`
- **Dynamic Models**: It fetches and caches the list of supported models from the Pollinations endpoint every 24 hours.

> [!TIP]
> This would be an excellent "onboarding" provider for `WriterAgent` users who haven't set up an OpenAI or Anthropic key yet.

---

## 5. Deployment & Configuration

### Native Options Pages
`Loki` implements a multi-tab options dialog directly inside `Extras → Options → AI-Assistant`.
- **Implementation**: Uses `XContainerWindowEventHandler` to handle events for native `.xdl` files.
- **Localization**: Features a sophisticated runtime localization system that detects the LibreOffice UI language from `OfficeResourceLoader` and applies JSON translations at runtime.

---

## Future Integration Path for WriterAgent

1.  **Refactor Image Insertion**: Adopt the `GraphicProvider` embedding logic from `main.py:638`.
2.  **Add PlantUML Tool**: Create a specialized writer tool that uses the PlantUML rendering chain found in `main.py:322`.
3.  **Pollinations Fallback**: Integrate the `PollinationsProvider` as a default fallback for image generation.
4.  **UI Feedback**: Use the `StatusIndicator` for tool-loop progress instead of just sidebar text.
