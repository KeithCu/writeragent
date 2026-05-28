#!/usr/bin/env python3
"""
Harvest script for writeragent_vec Cython binaries.
Extracts .so and .pyd files from wheels and places them in plugin/contrib/vec_pack.
"""

import os
import shutil
import zipfile
import platform
import subprocess
import json
import tempfile
from pathlib import Path
import argparse

REPO_ROOT = Path(__file__).parent.parent.resolve()
DEST_DIR = REPO_ROOT / "plugin" / "contrib" / "vec_pack"
WORKFLOW_NAME = "build-vec-wheels.yml"

def strip_binary(filepath):
    """Strip debug symbols from a binary file to reduce size."""
    filepath = Path(filepath)
    if not filepath.exists() or filepath.stat().st_size < 1024:
        return
    
    # Try llvm-strip first, then strip
    for stripper in ["llvm-strip", "strip"]:
        try:
            result = subprocess.run([stripper, str(filepath)], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print(f"  Stripped {filepath.name}")
                return
        except (FileNotFoundError, OSError):
            continue

def get_repo_name():
    """Detect the repository name from git remotes."""
    try:
        # Try gh repo view first
        result = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        
        # Fallback to git remote
        result = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True)
        if result.returncode == 0:
            url = result.stdout.strip()
            # Handle git@github.com:owner/repo.git or https://github.com/owner/repo.git
            if ":" in url:
                return url.split(":")[-1].replace(".git", "")
            elif "/" in url:
                parts = url.rstrip("/").split("/")
                return f"{parts[-2]}/{parts[-1].replace('.git', '')}"
    except Exception:
        pass
    return None

def fetch_wheels(temp_dir):
    """Use GitHub CLI to download the latest successful wheels."""
    print("Fetching latest wheels from GitHub Actions...")
    
    repo = get_repo_name()
    repo_args = ["-R", repo] if repo else []
    if repo:
        print(f"  Targeting repository: {repo}")

    try:
        # 1. Get the latest successful run ID
        run_cmd = ["gh", "run", "list", "--workflow", WORKFLOW_NAME, "--status", "success", "--limit", "1", "--json", "databaseId"] + repo_args
        result = subprocess.run(run_cmd, capture_output=True, text=True, check=True)
        runs = json.loads(result.stdout)
        
        if not runs:
            print("Error: No successful workflow runs found for " + WORKFLOW_NAME)
            return False
            
        run_id = str(runs[0]["databaseId"])
        print(f"  Found latest successful run: {run_id}")
        
        # 2. Download the artifacts
        print(f"  Downloading artifacts to {temp_dir}...")
        download_cmd = ["gh", "run", "download", run_id, "-D", str(temp_dir)] + repo_args
        subprocess.run(download_cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error fetching wheels via gh: {e}")
        if e.stderr:
            print(e.stderr)
        return False
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error parsing GitHub CLI output: {e}")
        return False

def harvest_wheels(input_dir):
    """Extract and strip binaries from the specified directory (recursive)."""
    input_dir = Path(input_dir)
    found_wheels = list(input_dir.rglob("*.whl"))
    print(f"Found {len(found_wheels)} wheels in {input_dir}")

    for wheel in found_wheels:
        # Skip Windows ARM64 and macOS builds for now as requested
        if "win_arm64" in wheel.name:
            print(f"Skipping {wheel.name} (Windows ARM64 disabled)")
            continue
        if "macosx" in wheel.name:
            print(f"Skipping {wheel.name} (macOS disabled)")
            continue

        print(f"Processing {wheel.name}...")
        with zipfile.ZipFile(wheel, 'r') as zip_ref:
            for file_info in zip_ref.infolist():
                filename = file_info.filename
                # Look for .so or .pyd files inside the package directory
                if (filename.startswith("writeragent_vec/") and 
                    (filename.endswith(".so") or filename.endswith(".pyd"))):
                    
                    target_name = os.path.basename(filename)
                    target_path = DEST_DIR / target_name
                    
                    with zip_ref.open(filename) as src, open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    
                    print(f"  Extracted {target_name}")
                    strip_binary(target_path)

def main():
    parser = argparse.ArgumentParser(description="Harvest writeragent_vec binaries from wheels.")
    parser.add_argument("input_dir", nargs="?", help="Directory containing .whl files (optional if using --fetch)")
    parser.add_argument("--fetch", action="store_true", help="Fetch latest wheels from GitHub Actions using gh CLI")
    args = parser.parse_args()

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    
    # Copy __init__.py if it doesn't exist or to ensure it's up to date
    init_src = REPO_ROOT / "native" / "writeragent_vec" / "src" / "writeragent_vec" / "__init__.py"
    if init_src.exists():
        shutil.copy2(init_src, DEST_DIR / "__init__.py")

    if args.fetch:
        with tempfile.TemporaryDirectory() as tmp_dir:
            if fetch_wheels(tmp_dir):
                harvest_wheels(tmp_dir)
    elif args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.exists():
            print(f"Error: Input directory {input_dir} does not exist.")
            return
        harvest_wheels(input_dir)
    else:
        parser.print_help()
        return

    print(f"\nDone! Binaries are in {DEST_DIR}")

if __name__ == "__main__":
    main()
