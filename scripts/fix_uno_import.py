#!/usr/bin/env python3
import sys
import os
import subprocess
import site

def find_system_uno():
    """Find the system's uno.py and LibreOffice program directory."""
    uno_path = None
    lo_program = None

    # 1. Try importing from system python
    try:
        # We run a subprocess to avoid polluting current process
        result = subprocess.run(
            ["python3", "-c", "import uno; print(uno.__file__)"],
            capture_output=True, text=True, check=True
        )
        uno_path = os.path.dirname(result.stdout.strip())
    except Exception:
        pass

    # 2. Try common locations if 1 fails
    if not uno_path:
        search_paths = [
            "/usr/lib/python3/dist-packages",
            "/usr/lib/python3.14/site-packages",
            "/usr/lib/python3.13/site-packages",
            "/usr/lib/python3.12/site-packages",
            "/usr/lib64/python3.14/site-packages",
        ]
        for p in search_paths:
            if os.path.exists(os.path.join(p, "uno.py")):
                uno_path = p
                break

    # 3. Find LO program dir (containing pyuno.so)
    try:
        result = subprocess.run(
            ["which", "soffice"],
            capture_output=True, text=True, check=True
        )
        soffice_path = os.path.realpath(result.stdout.strip())
        lo_program = os.path.dirname(soffice_path)
    except Exception:
        pass

    if not lo_program:
        search_libs = [
            "/usr/lib/libreoffice/program",
            "/usr/lib64/libreoffice/program",
            "/opt/libreoffice/program",
        ]
        for p in search_libs:
            if os.path.exists(os.path.join(p, "pyuno.so")):
                lo_program = p
                break

    return uno_path, lo_program

def main():
    venv_base = os.environ.get("VIRTUAL_ENV") or os.path.abspath(".venv")
    if not os.path.exists(venv_base):
        print(f"Error: Virtual environment not found at {venv_base}")
        sys.exit(1)

    print(f"Using virtual environment: {venv_base}")

    uno_path, lo_program = find_system_uno()
    
    if not uno_path:
        print("Error: Could not find system uno.py. Please ensure LibreOffice python-uno is installed.")
        sys.exit(1)
    
    if not lo_program:
        print("Warning: Could not find LibreOffice program directory (pyuno.so).")
        # We continue anyway as uno.py might be enough for some tools
    
    # Find site-packages in venv
    # We look for lib/pythonX.Y/site-packages
    lib_dir = os.path.join(venv_base, "lib")
    if not os.path.exists(lib_dir):
         print(f"Error: {lib_dir} not found.")
         sys.exit(1)
         
    py_dirs = [d for d in os.listdir(lib_dir) if d.startswith("python")]
    if not py_dirs:
        print(f"Error: No python directory found in {lib_dir}")
        sys.exit(1)
        
    site_packages = os.path.join(lib_dir, py_dirs[0], "site-packages")
    if not os.path.exists(site_packages):
        print(f"Error: {site_packages} not found.")
        sys.exit(1)

    pth_file = os.path.join(site_packages, "uno.pth")
    print(f"Creating {pth_file}...")
    
    with open(pth_file, "w") as f:
        f.write("# Added by scripts/fix_uno_import.py\n")
        f.write(f"{uno_path}\n")
        if lo_program:
            f.write(f"{lo_program}\n")

    print(f"Successfully added paths to {pth_file}:")
    print(f"  - {uno_path}")
    if lo_program:
        print(f"  - {lo_program}")
    
    # 5. Install types-unopy for static analysis
    print("\nInstalling types-unopy for static analysis...")
    try:
        venv_python = os.path.join(venv_base, "bin", "python")
        subprocess.run(
            [venv_python, "-m", "pip", "install", "types-unopy"],
            check=True
        )
        print("Successfully installed types-unopy.")
    except Exception as e:
        print(f"Warning: Could not install types-unopy: {e}")

    # Verification
    print("\nVerifying import...")
    venv_python = os.path.join(venv_base, "bin", "python")
    try:
        subprocess.run(
            [venv_python, "-c", "import uno; print('Import successful!')"],
            check=True
        )
    except subprocess.CalledProcessError:
        print("Warning: Verification import failed inside the venv.")

if __name__ == "__main__":
    main()
