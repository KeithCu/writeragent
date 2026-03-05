# Next Steps for AI Horde Integration

This document outlines future improvements for the AI Horde integration in LocalWriter, prioritized by user impact.

## High Impact / UX

### 1. Real-time Progress Feedback
**Problem**: Generating an image can take minutes (queue + processing), but the sidebar status only shows "Running..." without updates. The [AiHordeClient](file:///home/keithcu/Desktop/Python/localwriter/core/aihordeclient/__init__.py#162-1146) already receives progress events (e.g., "Queue position 12", "Processing 45%"), but these are only logged to debug files.
**Task**: Thread the status callback from `AiHordeImageProvider` -> [SimpleInformer](file:///home/keithcu/Desktop/Python/localwriter/core/image_service.py#66-84) -> [ChatPanel](file:///home/keithcu/Desktop/Python/localwriter/chat_panel.py#956-976) status bar so users see live updates.

### 2. "Check Kudos Balance" Button
**Problem**: Users can't easily verify if their API key is working or check their kudos balance without visiting the Horde website.
**Task**: Add a small "Check Balance" button next to the API Key field in Settings (Page 2) that populates a message box with the current balance.

### 3. Translation Status Visibility
**Problem**: Prompt translation acts silently. If the translation service fails, it falls back to English without notifying the user.
**Task**: Show a "Translating prompt..." status in the sidebar before the generation starts.

## Logic / Correctness

### 4. Smart Image Dimensions for Editing
**Problem**: The [edit_image](file:///home/keithcu/Desktop/Python/localwriter/core/document_tools.py#239-274) tool currently hardcodes the replacement image size to `512x512`. This distorts the aspect ratio if the original image was different.
**Task**: Update [tool_edit_image](file:///home/keithcu/Desktop/Python/localwriter/core/document_tools.py#239-274) to read the dimensions of the selected image from LibreOffice and pass them to the generation request, ensuring the edited image matches the original's size and aspect ratio.

### 5. Configurable Batch Size (`n`)
**Problem**: The UI and tool schemas only support generating 1 image at a time, but AI Horde supports batch generation.
**Task**: Add a "Count" or "Batch Size" setting to the Image Generation tab and pass it to the API.

## Code Quality

### 6. Cleanup Vestigial Constants
**Task**: Rename `__HORDE_CLIENT_NAME__ = "AiHordeForGimp"` in [core/aihordeclient/__init__.py](file:///home/keithcu/Desktop/Python/localwriter/core/aihordeclient/__init__.py) to `"LocalWriter"`.

### 7. Refactor [SimpleInformer](file:///home/keithcu/Desktop/Python/localwriter/core/image_service.py#66-84)
**Task**: The [SimpleInformer](file:///home/keithcu/Desktop/Python/localwriter/core/image_service.py#66-84) class inside [ImageService](file:///home/keithcu/Desktop/Python/localwriter/core/image_service.py#118-184) is a minimal mock. It should be refactored to be more robust and better integrated with the [ChatPanel](file:///home/keithcu/Desktop/Python/localwriter/chat_panel.py#956-976)'s status control system.
