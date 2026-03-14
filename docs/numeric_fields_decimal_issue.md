# Numeric Fields Decimal Issue Analysis

## The Issue

In the Settings dialog, integer fields configured as `widget: number` (like `extend_selection_max_tokens` and `edit_selection_max_new_tokens`) are displaying fractional values in the UI (e.g., `70.00` instead of `70`), despite being typed as `int` in the module schema.

## Findings

1. **XDL Generation (`scripts/manifest_xdl.py`)**:
   - The code correctly recognizes numeric fields and sets `dlg:spin="true"`.
   - The generator assigns the `dlg:decimal-accuracy` property based on type:
     ```python
     attrs[_dlg("decimal-accuracy")] = "1" if schema.get("type") == "float" else "0"
     ```
   - Looking at the generated XDL files (e.g., `build/generated/dialogs/chatbot.xdl`), the fields correctly have `dlg:decimal-accuracy="0"` present.

2. **Field Configuration (`plugin/framework/settings_dialog.py`)**:
   - The configuration properties correctly read the type and values as integer.
   - However, when constructing the field payload for the UI, `str(val)` is used regardless of the underlying type:
     ```python
     field = {"name": ctrl_id, "value": str(val)}
     ```

3. **Dialog Population (`plugin/framework/legacy_ui.py`)**:
   - During dialog creation, the runtime attempts to populate the controls with the string-converted values.
   - For `numericfield` controls, they expose a `setText` method (or `.Text` model property), which the generic loading logic uses:
     ```python
     if hasattr(ctrl, "setText"):
         ctrl.setText(field["value"])
     ```
   - Alternatively, it might fall back to `ctrl.getModel().Text = field["value"]`.

## Root Cause Hypotheses

There are a few reasons why a `NumericField` in LibreOffice dialogs might forcefully display decimals even when `decimal-accuracy` is `0`:

### Hypothesis A: Property vs Method (Value vs Text)
LibreOffice `NumericField` models primarily use the `.Value` property (which is a `double`) rather than `.Text` to manage their numerical content. By assigning a string value via `.Text` or `setText()`, LibreOffice might internally parse it and fall back to its default generic float formatting (which typically uses 2 decimal places), overriding the `decimal-accuracy` constraint.

### Hypothesis B: Formatting defaults
If a `NumericField` does not have a strict formatting enforced or if `decimal-accuracy` alone isn't sufficient to force integer-only display in this specific context, LibreOffice might default to appending `.00`.

## Things to Try (Local Testing)

If you are continuing to debug this locally, please try the following changes in `plugin/framework/legacy_ui.py`:

1. **Use `.Value` property for Number widgets**:
   Instead of falling back to `.setText()`, explicitly detect numeric fields and assign their `.Value` property using a typed number (float/int) rather than a string.

   ```python
   # Inside `settings_box` where controls are populated
   field_type = field.get("type", "text")

   if hasattr(ctrl.getModel(), "Value") and field_type in ("int", "float"):
       try:
           # Numeric fields expect a double/float for Value
           ctrl.getModel().Value = float(field["value"])
       except ValueError:
           pass
   elif hasattr(ctrl, "setText"):
       # Existing string/text logic
       ctrl.setText(field["value"])
   ```

2. **Update reading logic in `settings_box`**:
   Similarly, when saving the configuration from the dialog, prefer reading the `.Value` property if it's available, rather than `getText()` which might contain the formatted `70.00` string.

   ```python
   # Inside `settings_box` where controls are read
   if hasattr(ctrl.getModel(), "Value") and field_type in ("int", "float"):
       control_val = ctrl.getModel().Value
       if field_type == "int":
           result[field["name"]] = int(control_val)
       else:
           result[field["name"]] = control_val
   else:
       # Existing getText() logic
   ```

3. **Check if `decimal-accuracy` is applied successfully**:
   Open one of the generated XDLs (e.g., `build/generated/dialogs/chatbot.xdl`) directly in the LibreOffice Basic Dialog Editor and see if the fields natively show `0` or `0.00` at design time. If they show `0`, then the runtime population (Hypothesis A) is definitively to blame.

4. **Formatting enforcement (`ValueStep` / `ValueMin` / `ValueMax`)**:
   Sometimes numeric fields act weirdly if their formatting limits aren't explicitly declared. If the above fails, you can try setting `DecimalAccuracy = 0` programmatically on the control model immediately before setting its value in `legacy_ui.py`.
