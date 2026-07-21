import os
import re

def extract_strings_from_file(filepath):
    strings = set()
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

            # Extract dlg:value="..."
            matches = re.findall(r'dlg:value="([^"]+)"', content)
            for m in matches:
                # Filter out pure numbers or very short strings like "1", "100"
                if not m.isdigit() and len(m.strip()) > 0:
                    strings.add(m)

            # Extract dlg:title="..."
            matches = re.findall(r'dlg:title="([^"]+)"', content)
            for m in matches:
                if not m.isdigit() and len(m.strip()) > 0:
                    strings.add(m)

            # Extract dlg:label="..."
            matches = re.findall(r'dlg:label="([^"]+)"', content)
            for m in matches:
                if not m.isdigit() and len(m.strip()) > 0:
                    strings.add(m)

            # Extract stringlist items? dlg:stringlist="..."
            matches = re.findall(r'dlg:stringitem="([^"]+)"', content)
            for m in matches:
                if not m.isdigit() and len(m.strip()) > 0:
                    strings.add(m)

            # In SettingsDialog.xdl.tpl, there are strings directly in stringlist items:
            # <dlg:stringitem dlg:value="..."/>
            # Oh wait, we already handle dlg:value. But for string list items it is:
            # <dlg:stringitem dlg:value="Square"/> etc.

    except Exception as e:
        print(f"Error reading {filepath}: {e}")
    return strings

def main():
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    extension_dir = os.path.join(root_dir, 'extension', 'Dialogs')

    all_strings = set()

    for filename in os.listdir(extension_dir):
        if filename.endswith('.xdl') or filename.endswith('.xdl.tpl'):
            filepath = os.path.join(extension_dir, filename)
            strings = extract_strings_from_file(filepath)
            all_strings.update(strings)

    if all_strings:
        out_path = os.path.join(root_dir, 'plugin', 'xdl_strings.py')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write("# Auto-generated file for xgettext to extract XDL strings.\n")
            f.write("# Do not modify or commit this file.\n\n")
            for s in sorted(list(all_strings)):
                # Escape quotes
                s_escaped = s.replace('"', '\\"')
                f.write(f'_("{s_escaped}")\n')
        print(f"Extracted {len(all_strings)} strings to {out_path}")

if __name__ == "__main__":
    main()
