#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu (modifications and updates)

"""
Script to automatically translate missing strings using AI.
Adds a completeness report at start and processes strings in batches.
Leading/trailing whitespace is stripped before the API call and restored on the result.

``--execute`` uses default model ``x-ai/grok-4.1-fast`` when ``--model`` is omitted.
``--review`` requires ``--model`` (use another model than gap-fill for useful critiques)
and writes a JSON report (never modifies ``.po`` files): stdout lists every string including
``No Errors`` rows; the file's ``suggestions`` array lists only ``suggest`` / ``error`` rows.
"""

import os
import re
import json
import logging
import subprocess
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple, Any
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
    
    from plugin.framework.auth import resolve_auth_for_config, build_auth_headers
    from plugin.framework.config import get_config, get_config_dict
    from plugin.framework.constants import USER_AGENT, APP_REFERER, APP_TITLE
except ImportError:
    # Fallback for when running outside the full WriterAgent environment
    USER_AGENT = "WriterAgent (https://github.com/KeithCu/writeragent)"
    APP_REFERER = "https://github.com/KeithCu/writeragent"
    APP_TITLE = "WriterAgent"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger("translate_missing")

DEFAULT_TRANSLATE_MODEL = "x-ai/grok-4.1-fast"
DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1"
# Fixed phrase for acceptable strings in dense review JSON (model must use this verbatim for "ok").
REVIEW_NO_ERRORS = "No Errors"


def _libreoffice_gettext_rules_text() -> str:
    """Shared gettext/UI rules for translate and review prompts (WriterAgent inside LibreOffice)."""
    return (
        "WriterAgent is a LibreOffice extension: these English msgids are translated for UI that appears "
        "in LibreOffice Writer, Calc, Draw, and Impress next to native menus and dialogs. Use standard "
        "LibreOffice terminology for the target locale when the string is clearly UI chrome (menus, "
        "options, common actions).\n\n"
        "Preserve placeholders, format tokens (e.g. {0}, %s, %(name)s), and single-character menu "
        "accelerators where present. Do not corrupt product names: Writer, Calc, Draw, and Impress are "
        "proper names (Calc is not the verb \"calculate\"; in many locales Writer must not become the word "
        "for \"printer\")."
    )


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


def _strip_json_fenced_content(content: str) -> str:
    """Strip optional ``` / ```json markdown fences from a model message body."""
    c = content.strip()
    if c.startswith("```json"):
        c = c[7:].strip()
    elif c.startswith("```"):
        c = c[3:].strip()
    if c.endswith("```"):
        c = c[:-3].strip()
    return c


def _resolve_openrouter_api_key(endpoint: str, api_key: Optional[str]) -> Optional[str]:
    if api_key:
        return api_key
    gcd = globals().get("get_config_dict")
    if callable(gcd):
        try:
            raw = gcd(None)
            if isinstance(raw, dict):
                config: Dict[str, Any] = raw
                key = config.get("api_keys_by_endpoint", {}).get(endpoint, "")
                if not key and "api_key" in config:
                    key = config.get("api_key", "")
                if key:
                    return str(key)
        except Exception:
            pass
    return os.environ.get("OPENROUTER_API_KEY")


def call_translate_batch(texts: List[str], target_lang: str, model: str = "x-ai/grok-4.1-fast", 
                         endpoint: str = "https://openrouter.ai/api/v1", api_key: Optional[str] = None) -> List[Optional[str]]:
    """Call AI with a list of strings and get corresponding translations back."""
    import urllib.request

    api_key = _resolve_openrouter_api_key(endpoint, api_key)
    if not api_key:
        log.error("No API key available. Provide it via argument, config, or OPENROUTER_API_KEY env.")
        return [None] * len(texts)

    rules = _libreoffice_gettext_rules_text()
    prompt = f"""{rules}

You translate the following numbered English gettext source strings into the language '{target_lang}' for the WriterAgent extension catalogs (same rules as automated review uses).

Return ONLY a strictly formatted JSON array containing the translated strings in order: index `i` must be the translation for item `i+1`. Do not add commentary or markdown fences.

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
                content = _strip_json_fenced_content(content)
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


def entry_is_translated(entry: polib.POEntry) -> bool:
    if entry.msgid == "":
        return False
    if entry.msgid_plural:
        forms = entry.msgstr_plural or {}
        return any((v or "").strip() for v in forms.values())
    return bool((entry.msgstr or "").strip())


def collect_translated_entries(po_path: str) -> List[Dict[str, Any]]:
    """All catalog rows with non-empty translation(s), including fuzzy."""
    po = polib.pofile(po_path)
    out: List[Dict[str, Any]] = []
    for entry in po:
        if not entry_is_translated(entry):
            continue
        row: Dict[str, Any] = {
            "msgid": entry.msgid,
            "fuzzy": "fuzzy" in entry.flags,
        }
        if entry.msgid_plural:
            row["msgid_plural"] = entry.msgid_plural
            row["msgstr"] = entry.msgstr
            row["msgstr_plural"] = dict(entry.msgstr_plural) if entry.msgstr_plural else {}
        else:
            row["msgid_plural"] = None
            row["msgstr"] = entry.msgstr
            row["msgstr_plural"] = None
        out.append(row)
    return out


def parse_review_dense_response(content: str, batch_len: int) -> List[Optional[Dict[str, Any]]]:
    """Parse model JSON: ``batch_len`` objects in order, or reorder by ``index`` when lengths match."""
    none_row: List[Optional[Dict[str, Any]]] = [None] * batch_len
    try:
        data = json.loads(_strip_json_fenced_content(content))
    except json.JSONDecodeError:
        log.error("Review batch: invalid JSON in model response")
        return none_row
    if not isinstance(data, list):
        return none_row

    if len(data) != batch_len:
        log.error("Review response length mismatch: got %s expected %s", len(data), batch_len)
        return none_row

    indexed: Dict[int, Dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = item.get("index")
        if raw is None:
            continue
        try:
            idx0 = int(raw) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx0 < batch_len:
            indexed[idx0] = item

    if len(indexed) == batch_len:
        return [indexed[i] for i in range(batch_len)]

    out: List[Optional[Dict[str, Any]]] = []
    for item in data:
        out.append(item if isinstance(item, dict) else None)
    while len(out) < batch_len:
        out.append(None)
    if len(out) > batch_len:
        out = out[:batch_len]
    return out


def merge_review_dense(
    batch: List[Dict[str, Any]],
    model_rows: List[Optional[Dict[str, Any]]],
    locale: str,
) -> List[Dict[str, Any]]:
    """One report row per catalog string; ``action`` ``ok`` uses literal ``REVIEW_NO_ERRORS`` reasoning."""
    merged: List[Dict[str, Any]] = []
    for i, orig in enumerate(batch):
        mr = model_rows[i] if i < len(model_rows) else None
        base: Dict[str, Any] = {
            "locale": locale,
            "msgid": orig["msgid"],
            "fuzzy": orig["fuzzy"],
            "current_msgstr": orig["msgstr"],
            "msgid_plural": orig.get("msgid_plural"),
            "current_msgstr_plural": orig.get("msgstr_plural"),
        }
        if mr is None:
            merged.append(
                {
                    **base,
                    "action": "error",
                    "suggested_msgstr": None,
                    "suggested_msgstr_plural": None,
                    "reasoning_en": "Model or parse error for this entry.",
                }
            )
            continue

        act = str(mr.get("action", "")).lower()
        if act in ("ok", "keep", "no_error", "none", ""):
            act = "ok"
        has_sug = mr.get("suggested_msgstr") is not None and str(mr.get("suggested_msgstr", "")).strip() != ""
        has_pl = bool(mr.get("suggested_msgstr_plural"))

        if act == "suggest" and (has_sug or has_pl):
            merged.append(
                {
                    **base,
                    "action": "suggest",
                    "suggested_msgstr": mr.get("suggested_msgstr"),
                    "suggested_msgstr_plural": mr.get("suggested_msgstr_plural"),
                    "reasoning_en": str(mr.get("reasoning_en", "")).strip()
                    or "Suggested alternative (no reason given).",
                }
            )
        else:
            merged.append(
                {
                    **base,
                    "action": "ok",
                    "suggested_msgstr": None,
                    "suggested_msgstr_plural": None,
                    "reasoning_en": REVIEW_NO_ERRORS,
                }
            )
    return merged


def call_review_batch(
    batch: List[Dict[str, Any]],
    locale_code: str,
    model: str,
    endpoint: str = DEFAULT_OPENROUTER_ENDPOINT,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Dense review: ``len(batch)`` model objects in, same count of report rows out."""
    import urllib.request

    n = len(batch)
    if n == 0:
        return []

    api_key = _resolve_openrouter_api_key(endpoint, api_key)
    if not api_key:
        log.error("No API key available. Provide it via argument, config, or OPENROUTER_API_KEY env.")
        return []

    items: List[Dict[str, Any]] = []
    for i, ent in enumerate(batch):
        item: Dict[str, Any] = {
            "index": i + 1,
            "msgid": ent["msgid"],
            "current_msgstr": ent["msgstr"],
            "fuzzy": ent["fuzzy"],
        }
        if ent.get("msgid_plural"):
            item["msgid_plural"] = ent["msgid_plural"]
            item["current_msgstr_plural"] = ent.get("msgstr_plural") or {}
        items.append(item)

    schema = (
        f"Return ONLY a JSON array of exactly {n} objects, in the same order as the input (indices 1..{n}). "
        'Each object must have: "index" (1-based int), "action" ("ok" or "suggest"), '
        f'"reasoning_en" (string). '
        f'If the translation is acceptable, use "action": "ok" and set "reasoning_en" to exactly '
        f'"{REVIEW_NO_ERRORS}" with no extra words. '
        'If you flag a clear mistake (wrong meaning, product name, placeholder, or seriously bad phrasing), '
        'use "action": "suggest", set "suggested_msgstr" and/or "suggested_msgstr_plural" as needed, '
        'and brief English in "reasoning_en" (why the current string is wrong). '
        "Preserve placeholders, format tokens, and single-& menu accelerators. "
        "Do not wrap in markdown."
    )

    rules = _libreoffice_gettext_rules_text()
    prompt = (
        f"You review LibreOffice UI gettext translations for locale '{locale_code}'.\n"
        f"{rules}\n\n"
        f"{schema}\n\n"
        "Strings to review (JSON):\n"
        f"{json.dumps(items, ensure_ascii=False)}"
    )

    url = f"{endpoint}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": APP_REFERER,
        "X-Title": APP_TITLE,
        "User-Agent": USER_AGENT,
    }
    data = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }

    try:
        req = urllib.request.Request(
            url=url, data=json.dumps(data).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            if response.status != 200:
                log.error("Review API HTTP %s", response.status)
                return merge_review_dense(batch, [None] * n, locale_code)
            response_data = json.loads(response.read().decode("utf-8"))
            if "choices" in response_data and len(response_data["choices"]) > 0:
                content = response_data["choices"][0]["message"]["content"].strip()
                parsed = parse_review_dense_response(content, n)
                return merge_review_dense(batch, parsed, locale_code)
            log.error("Review: unexpected response shape")
    except Exception as e:
        log.error("Review batch error: %s", e)
    return merge_review_dense(batch, [None] * n, locale_code)


def review_batch_worker(
    locale: str, batch: List[Dict[str, Any]], model: str, api_key: Optional[str] = None
) -> List[Dict[str, Any]]:
    return call_review_batch(batch, locale, model, api_key=api_key)


def default_review_output_path(locale_codes: List[str]) -> str:
    tag = "_".join(sorted(locale_codes))
    return f"translation_review_{tag}.json"


def _elide_for_terminal(s: str, max_len: int = 100) -> str:
    t = s.replace("\r", "").replace("\n", " ")
    t = " ".join(t.split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def print_review_rows_live(rows: List[Dict[str, Any]]) -> None:
    """Echo one line per string in a dense review batch (``ok`` rows show only ``No Errors``)."""
    if not rows:
        print(REVIEW_NO_ERRORS, flush=True)
        return
    for r in rows:
        loc = str(r.get("locale", "?"))
        msgid_e = _elide_for_terminal(str(r.get("msgid", "")))
        cur_e = _elide_for_terminal(str(r.get("current_msgstr", "")), 72)
        reason_e = _elide_for_terminal(str(r.get("reasoning_en", "")), 100)
        fz = " fuzzy" if r.get("fuzzy") else ""
        act = str(r.get("action", "")).lower()
        if act == "ok":
            print(f"[{loc}{fz}] {msgid_e} | {REVIEW_NO_ERRORS}", flush=True)
            continue
        sug = r.get("suggested_msgstr")
        sug_s = str(sug).strip() if sug is not None else ""
        spl = r.get("suggested_msgstr_plural")
        if sug_s:
            sug_e = _elide_for_terminal(sug_s, 72)
            print(
                f"[{loc}{fz}] {msgid_e} | {cur_e} -> {sug_e} | {reason_e}",
                flush=True,
            )
        elif spl:
            spl_e = _elide_for_terminal(json.dumps(spl, ensure_ascii=False), 72)
            print(
                f"[{loc}{fz}] {msgid_e} | {cur_e} -> {spl_e} | {reason_e}",
                flush=True,
            )
        else:
            print(
                f"[{loc}{fz}] {msgid_e} | {cur_e} | {reason_e}",
                flush=True,
            )


def review_rows_for_json_report(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip ``ok`` / no-issue rows; JSON file keeps only ``suggest`` and ``error``."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        act = str(r.get("action", "")).lower()
        if act in ("suggest", "error"):
            out.append(r)
    return out


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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="Perform actual updates")
    mode.add_argument(
        "--preview",
        action="store_true",
        help="Print localization status table only (does not modify .po files)",
    )
    mode.add_argument(
        "--review",
        action="store_true",
        help="Review existing translations with a model; write JSON report only (no .po changes)",
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

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Chat model id. Required with --review. For --execute only, defaults to "
            + DEFAULT_TRANSLATE_MODEL
            + " when omitted."
        ),
    )
    parser.add_argument("--api-key", type=str, default=None, help="Force API key")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="With --review: JSON report path (default: translation_review_<locales>.json)",
    )
    parser.add_argument(
        "--skip-initial-status",
        action="store_true",
        help="Do not print the localization table at start (e.g. second phase of make auto-translate after status preview)",
    )
    args = parser.parse_args()

    if args.review:
        if not args.model or not str(args.model).strip():
            parser.error(
                "--review requires --model NAME (required so you pick a reviewer model; "
                "gap-fill default applies only to --execute)."
            )
    elif args.execute and not args.model:
        args.model = DEFAULT_TRANSLATE_MODEL

    if args.review:
        if not args.skip_initial_status:
            print_status_report()

        po_files = glob_po_files("plugin/locales")
        review_work_items: List[Tuple[str, str, List[Dict[str, Any]]]] = []
        for lang, f_path in sorted(po_files):
            if args.lang and args.lang != lang:
                continue
            entries = collect_translated_entries(f_path)
            if not entries:
                log.info("%s: no translated strings to review, skipping.", lang)
                continue
            review_work_items.append((lang, f_path, entries))

        if not review_work_items:
            log.error("No locales with translated strings to review (check --lang).")
            raise SystemExit(1)

        locale_codes = [lang for lang, _, _ in review_work_items]
        out_path = args.output or default_review_output_path(locale_codes)

        review_tasks: List[Tuple[str, List[Dict[str, Any]]]] = []
        for lang, _f_path, entries in review_work_items:
            for i in range(0, len(entries), args.batch_size):
                review_tasks.append((lang, entries[i : i + args.batch_size]))

        suggestions: List[Dict[str, Any]] = []
        total_tasks = len(review_tasks)
        completed = 0
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {}
            for lang, batch in review_tasks:
                fut = pool.submit(review_batch_worker, lang, batch, args.model, args.api_key)
                futures[fut] = lang
                if args.delay > 0:
                    time.sleep(args.delay)
            for fut in as_completed(futures):
                lang = futures[fut]
                try:
                    part = fut.result()
                    suggestions.extend(part)
                    completed += 1
                    log.info("Review batch (%s/%s) complete (%s).", completed, total_tasks, lang)
                    print_review_rows_live(part)
                except Exception as e:
                    log.error("Review batch failed (%s): %s", lang, e)

        issues_only = review_rows_for_json_report(suggestions)
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model": args.model,
            "endpoint": DEFAULT_OPENROUTER_ENDPOINT,
            "mode": "review",
            "locales": locale_codes,
            "reviewed_string_count": len(suggestions),
            "suggestions": issues_only,
        }
        Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info(
            "Wrote review report to %s (%s issue rows, %s strings reviewed on screen)",
            out_path,
            len(issues_only),
            len(suggestions),
        )
        return

    if not args.skip_initial_status:
        print_status_report()

    if args.preview:
        return

    if not args.execute:
        print(
            "Dry run active. Run with `--execute` to perform updates, "
            "`--preview` to show localization status only, "
            "or `--review --model ...` to write a translation review JSON report."
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
                    print(f"{orig} - {res}", flush=True)
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
