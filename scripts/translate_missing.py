#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and updates)

"""
Script to automatically translate missing strings using AI.
Adds a completeness report at start and processes strings in batches.
Leading/trailing whitespace is stripped before the API call and restored on the result.
Uses `x-ai/grok-4.1-fast` (default).
"""

import os
import re
import json
import logging
import subprocess
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import time

# Ensure polib is available
try:
    import polib
except ImportError:
    print("Error: `polib` is required. Run: pip install polib")
    exit(1)

# Import WriterAgent modules for auth and configuration
try:
    # Add project root to path if running directly
    import sys
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    
    from plugin.framework.config import get_config_dict
    from plugin.framework.constants import USER_AGENT, APP_REFERER, APP_TITLE
except ImportError:
    # Fallback for when running outside the full WriterAgent environment
    USER_AGENT = "WriterAgent (https://github.com/keithcu/WriterAgent)"
    APP_REFERER = "https://github.com/keithcu/WriterAgent"
    APP_TITLE = "WriterAgent"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger("translate_missing")


def _pot_nonheader_count(pot_file: polib.POFile) -> int:
    return sum(1 for e in pot_file if e.msgid != "")


def print_status_report(locales_dir: str = "plugin/locales", pot_path: str = "plugin/locales/writeragent.pot"):
    """Print an aligned table: vs POT (same basis as find_missing) when writeragent.pot exists, else msgfmt."""
    files = glob_po_files(locales_dir)
    rows = []
    headers: List[str]
    pot_file: Optional[polib.POFile] = None
    p = Path(pot_path)
    if p.exists():
        try:
            pot_file = polib.pofile(str(p))
        except Exception:
            pot_file = None

    if pot_file:
        total_pot = _pot_nonheader_count(pot_file)
        for lang, f_path in sorted(files):
            pending = len(find_missing_translations(f_path, pot_file))
            done = total_pot - pending
            pct = (done / total_pot) * 100 if total_pot > 0 else 0.0
            rows.append([lang, str(total_pot), str(pending), str(done), f"{pct:.1f}%"])
        headers = ["Language", "POT total", "Pending", "Done", "Completion %"]
    else:
        for lang, f_path in sorted(files):
            stats = get_stats(f_path)
            if stats:
                t, fz, u, tot = stats
                pct = (t / tot) * 100 if tot > 0 else 0.0
                rows.append([lang, str(t), str(fz), str(u), str(tot), f"{pct:.1f}%"])
        headers = ["Language", "Translated", "Fuzzy", "Untranslated", "Total", "Completion %"]

    if not rows:
        print("No localization files found.")
        return

    widths = [len(h) for h in headers]
    for r in rows:
        for i, val in enumerate(r):
            widths[i] = max(widths[i], len(val))

    print("\n=== Current Localization Status ===")

    header_line = " | ".join(f"{headers[i]:<{widths[i]}}" for i in range(len(headers)))
    print(f"| {header_line} |")

    sep_line = " | ".join("-" * widths[i] for i in range(len(headers)))
    print(f"| {sep_line} |")

    for r in rows:
        row_line = " | ".join(f"{r[i]:<{widths[i]}}" for i in range(len(headers)))
        print(f"| {row_line} |")

    print("===================================\n")




def glob_po_files(locales_dir: str) -> List[Tuple[str, str]]:
    po_files = []
    locales_path = Path(locales_dir)
    if not locales_path.exists():
        return []
    for lang_dir in locales_path.iterdir():
        if lang_dir.is_dir():
            po_file = lang_dir / "LC_MESSAGES" / "writeragent.po"
            if po_file.exists():
                po_files.append((lang_dir.name, str(po_file)))
    return po_files


def get_stats(path: str) -> Optional[Tuple[int, int, int, int]]:
    """Get msgfmt statistics for a PO file."""
    try:
        result = subprocess.run(['msgfmt', '--statistics', path], stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        output = result.stderr.strip()
        
        translated = fuzzy = untranslated = 0
        match_t = re.search(r'(\d+)\s+translated', output)
        match_f = re.search(r'(\d+)\s+fuzzy', output)
        match_u = re.search(r'(\d+)\s+untranslated', output)
        
        if match_t: translated = int(match_t.group(1))
        if match_f: fuzzy = int(match_f.group(1))
        if match_u: untranslated = int(match_u.group(1))
            
        total = translated + fuzzy + untranslated
        return translated, fuzzy, untranslated, total
    except Exception:
        return None


def load_pot_file(pot_path: str = "plugin/locales/writeragent.pot") -> polib.POFile:
    pot_file = Path(pot_path)
    if not pot_file.exists():
        raise FileNotFoundError(f"POT file not found: {pot_path}")
    return polib.pofile(str(pot_file))


def find_missing_translations(po_file: str, pot_file: polib.POFile) -> List[Dict[str, str]]:
    po = polib.pofile(po_file)
    missing = []
    po_msgids = {entry.msgid for entry in po}
    
    # Also capture fuzzy strings to re-translate them
    fuzzy_msgids = {entry.msgid for entry in po if 'fuzzy' in entry.flags}
    
    for entry in pot_file:
        # Skip header
        if entry.msgid == "":
            continue
            
        # If missing entirely, completely empty translation, or fuzzy
        is_missing = (
            entry.msgid not in po_msgids or 
            entry.msgid in fuzzy_msgids or
            not any(e.msgid == entry.msgid and e.msgstr for e in po)
        )
        
        if is_missing:
            missing.append({
                "msgid": entry.msgid,
                "context": entry.comment if entry.comment else ""
            })
    return missing


def peel_edge_whitespace(s: str) -> Tuple[str, str, str]:
    """Split s into (leading_ws, core, trailing_ws). Core has no leading/trailing str.strip() whitespace."""
    m0 = re.match(r"^(\s*)", s, re.UNICODE)
    leading = m0.group(1) if m0 else ""
    rest = s[len(leading) :]
    m1 = re.search(r"(\s*)$", rest)
    trailing = m1.group(1) if m1 else ""
    core = rest[: len(rest) - len(trailing)] if trailing else rest
    return leading, core, trailing


def call_translate_batch(texts: List[str], target_lang: str, model: str = "x-ai/grok-4.1-fast", 
                         endpoint: str = "https://openrouter.ai/api/v1", api_key: Optional[str] = None) -> List[Optional[str]]:
    """Call AI with a list of strings and get corresponding translations back."""
    import urllib.request
    
    if not api_key:
        try:
            config = get_config_dict(None)
            if config:
                api_key = config.get("api_keys_by_endpoint", {}).get(endpoint, "")
                if not api_key and "api_key" in config:
                    api_key = config.get("api_key", "")
        except: pass
        
        if not api_key:
            # Try environment
            api_key = os.environ.get("OPENROUTER_API_KEY")
            if not api_key:
                log.error("No API key available. Provide it via argument, config, or OPENROUTER_API_KEY env.")
                return [None] * len(texts)

    prompt = f"""
Translate the following numbered list of English texts into the language '{target_lang}'.
Return a strictly formatted JSON array containing only the translated strings, where the string at index `i` is the translation for item `i+1`.
Do not add commentary, only return the JSON array.

Texts to translate:
"""

    for i, text in enumerate(texts):
        prompt += f"{i+1}. \"{text}\"\n"

    url = f"{endpoint}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_REFERER,
        "X-Title": APP_TITLE,
        "User-Agent": USER_AGENT
    }
    
    data = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,  # Low temperature for reliability
    }
    
    try:
        req = urllib.request.Request(url=url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=45) as response:
            if response.status != 200:
                log.error(f"API Error {response.status}")
                return [None] * len(texts)
                
            response_data = json.loads(response.read().decode("utf-8"))
            if "choices" in response_data and len(response_data["choices"]) > 0:
                content = response_data["choices"][0]["message"]["content"].strip()
                # Clean prompt artifacts just in case
                if content.startswith("```json"):
                    content = content[7:-3].strip()
                elif content.startswith("```"):
                    content = content[3:-3].strip()
                
                translations = json.loads(content)
                if isinstance(translations, list) and len(translations) == len(texts):
                    return [str(t) for t in translations]
                else:
                    log.error(f"Response size mismatch")
            else:
                log.error(f"Unexpected response")
    except Exception as e:
        log.error(f"Translate batch error: {e}")
        
    return [None] * len(texts)


def update_po_file(po_file: str, translations_dict: Dict[str, str]) -> bool:
    po = polib.pofile(po_file)
    updated = False
    
    # Track which msgids were updated
    for entry in po:
        is_fuzzy = 'fuzzy' in entry.flags
        if entry.msgid in translations_dict and (not entry.msgstr or entry.msgstr == "" or is_fuzzy):
            new_val = translations_dict[entry.msgid]
            # Programmatic layout safety enforcement
            if entry.msgid.startswith("\n") and not new_val.startswith("\n"):
                new_val = "\n" + new_val
            if entry.msgid.endswith("\n") and not new_val.endswith("\n"):
                new_val = new_val + "\n"
            entry.msgstr = new_val
            
            # Remove fuzzy flag if it exists since we supply a real translation
            if is_fuzzy:
                entry.flags.remove('fuzzy')
            updated = True


            
    # Add completely new entries from the template if not present in .po
    po_msgids = {entry.msgid for entry in po}
    for msgid, msgstr in translations_dict.items():
        if msgid not in po_msgids:
            new_val = msgstr
            if msgid.startswith("\n") and not new_val.startswith("\n"):
                new_val = "\n" + new_val
            if msgid.endswith("\n") and not new_val.endswith("\n"):
                new_val = new_val + "\n"
            entry = polib.POEntry(msgid=msgid, msgstr=new_val)
            po.append(entry)
            updated = True


    if updated:
        po.save(po_file)
        log.info(f"Saved {po_file}")
    return updated


def translate_batch_worker(texts: List[str], lang: str, model: str, api_key: Optional[str] = None) -> Dict[str, str]:
    """Worker for thread pool to translate a batch and return a map (original msgid -> msgstr)."""
    try:
        edges = [peel_edge_whitespace(t) for t in texts]
        cores = [c for _, c, _ in edges]
        results = call_translate_batch(cores, lang, model=model, api_key=api_key)
        batch_map = {}
        for original, (leading, _core, trailing), result in zip(texts, edges, results):
            if result is None:
                continue
            batch_map[original] = leading + str(result) + trailing
        return batch_map
    except Exception as e:
        log.error(f"Batch worker error: {e}")
        return {}



def main():
    import argparse
    parser = argparse.ArgumentParser(description="Auto-translate missing strings with AI")
    parser.add_argument("--execute", action="store_true", help="Perform actual updates")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Print localization status table only (does not modify .po files)",
    )
    parser.add_argument("--batch-size", type=int, default=10, help="Batch size (default: 10)")
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=5,
        help="Max concurrent API translation requests across all languages and batches (default: 5)",
    )
    parser.add_argument("--delay", type=float, default=0.05, help="Delay in seconds between thread starts (default: 0.05)")
    parser.add_argument("--lang", type=str, default=None, help="Filter for single language (e.g. 'fr')")

    parser.add_argument("--model", type=str, default="x-ai/grok-4.1-fast", help="Model name")
    parser.add_argument("--api-key", type=str, default=None, help="Force API key")
    parser.add_argument(
        "--skip-initial-status",
        action="store_true",
        help="Do not print the localization table at start (e.g. second phase of make auto-translate after status preview)",
    )
    args = parser.parse_args()

    if not args.skip_initial_status:
        print_status_report()

    if args.preview:
        return

    if not args.execute:
        print(
            "Dry run active. Run with `--execute` to perform updates, "
            "or `--preview` to show localization status only."
        )
        return

    pot_file = load_pot_file()
    po_files = glob_po_files("plugin/locales")

    work_items: List[Tuple[str, str, List[Dict[str, str]]]] = []
    for lang, f_path in po_files:
        if args.lang and args.lang != lang:
            continue

        log.info(f"Checking {lang}...")
        missing = find_missing_translations(f_path, pot_file)
        if not missing:
            log.info(f"{lang} is up to date.")
            continue

        log.info(f"Queued {len(missing)} strings for {lang} (batch size {args.batch_size})...")
        work_items.append((lang, f_path, missing))

    if not work_items:
        if not args.skip_initial_status:
            print("\n=== All locales up to date. ===\n")
        return

    batch_size = args.batch_size
    tasks: List[Tuple[str, str, List[str]]] = []
    for lang, f_path, missing in work_items:
        for i in range(0, len(missing), batch_size):
            batch = missing[i : i + batch_size]
            texts = [item["msgid"] for item in batch]
            tasks.append((f_path, lang, texts))

    aggregated: Dict[str, Dict[str, str]] = defaultdict(dict)
    total_tasks = len(tasks)
    completed_batches = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {}
        for f_path, lang, texts in tasks:
            fut = pool.submit(translate_batch_worker, texts, lang, args.model, args.api_key)
            futures[fut] = (f_path, lang)
            if args.delay > 0:
                time.sleep(args.delay)

        for fut in as_completed(futures):
            f_path, lang = futures[fut]
            try:
                batch_res = fut.result()
                aggregated[f_path].update(batch_res)
                completed_batches += 1
                log.info(f"Batch ({completed_batches}/{total_tasks}) complete ({lang}).")
                for orig, res in batch_res.items():
                    print(f"{orig} - {res}")
            except Exception as e:
                log.error(f"Batch failed with exception: {e}")

    did_update = False
    for f_path, translations in aggregated.items():
        if translations:
            if update_po_file(f_path, translations):
                did_update = True
            subprocess.run(["msgfmt", "-o", f_path.replace(".po", ".mo"), f_path])

    if did_update:
        print("\n=== Post Translation Status ===")
        print_status_report()



if __name__ == "__main__":
    main()
