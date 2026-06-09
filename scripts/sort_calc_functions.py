#!/usr/bin/env python3
# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2026 KeithCu
#
# SPDX-License-Identifier: GPL-3.0-or-later
"""Programmatically sort functions in calc_functions.py alphabetically."""

from pathlib import Path

def sort_functions(file_path: Path):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Find the header (everything up to the first function definition, including preceding comments)
    first_def_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("def "):
            first_def_idx = i
            break
            
    if first_def_idx == -1:
        print("No functions found!")
        return

    # Backtrack to catch comments immediately preceding the first def
    header_end = first_def_idx
    while header_end > 0 and lines[header_end - 1].strip().startswith("#"):
        header_end -= 1

    header = "".join(lines[:header_end])
    remaining_lines = lines[header_end:]

    # Parse remaining lines into individual function blocks
    blocks = []
    current_block_lines = []
    temp_lines = []
    
    for line in remaining_lines:
        if line.startswith("def "):
            # Split point: lines up to here belong to the previous function
            split_idx = len(temp_lines)
            while split_idx > 0 and (temp_lines[split_idx - 1].strip().startswith("#") or temp_lines[split_idx - 1].strip() == ""):
                split_idx -= 1
            
            prev_lines = temp_lines[:split_idx]
            if prev_lines:
                if current_block_lines:
                    current_block_lines.extend(prev_lines)
                    blocks.append(current_block_lines)
                    current_block_lines = []
                else:
                    blocks.append(prev_lines)
            
            # The comments preceding the new def belong to the new function block
            current_block_lines = temp_lines[split_idx:]
            current_block_lines.append(line)
            temp_lines = []
        else:
            temp_lines.append(line)
            
    if temp_lines:
        current_block_lines.extend(temp_lines)
    if current_block_lines:
        blocks.append(current_block_lines)

    # Extract function name from each block
    def get_func_name(block):
        for line in block:
            if line.startswith("def "):
                return line.split("def ")[1].split("(")[0].strip()
        return ""

    # Sort blocks alphabetically by function name (case-insensitive)
    sorted_blocks = sorted(blocks, key=lambda b: get_func_name(b).lower() or "zzz")

    # Reconstruct the file with standard PEP 8 spacing (two blank lines between functions)
    result = header
    for block in sorted_blocks:
        block_str = "".join(block).rstrip()
        result += block_str + "\n\n\n"

    result = result.rstrip() + "\n"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(result)

    print(f"Sorted {len(blocks)} functions successfully!")

if __name__ == "__main__":
    script_dir = Path(__file__).resolve().parent
    calc_funcs_path = script_dir.parent / "plugin" / "scripting" / "calc_functions.py"
    sort_functions(calc_funcs_path)
