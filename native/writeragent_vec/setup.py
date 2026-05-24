from setuptools import setup, Extension
from Cython.Build import cythonize
import os

# Allow overriding arch via environment variable
# Default to generic x86-64-v2 for modern compatibility
arch = os.environ.get("WRITERAGENT_ARCH", "x86-64-v2")
if arch == "x86-64-v1":
    arch = "x86-64"

extensions = [
    Extension(
        "writeragent_vec.pack",
        ["src/writeragent_vec/pack.pyx"],
        extra_compile_args=[f"-march={arch}", "-O3"],
    )
]

setup(
    name="writeragent_vec",
    version="0.1.0",
    package_dir={"": "src"},
    packages=["writeragent_vec"],
    ext_modules=cythonize(
        extensions,
        language_level=3,
        compiler_directives={
            'emit_code_comments': False,
        }
    ),
)
