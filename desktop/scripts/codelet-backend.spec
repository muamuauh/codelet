# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the standalone codelet FastAPI backend sidecar (onedir)."""

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

# Ensure conda Library\bin DLLs (libssl, libcrypto, ffi, libexpat, liblzma, bz2)
# are discoverable by PyInstaller even when invoked outside `conda activate`.
_conda_lib_bin = Path(sys.prefix) / "Library" / "bin"
if _conda_lib_bin.is_dir():
    os.environ["PATH"] = str(_conda_lib_bin) + os.pathsep + os.environ.get("PATH", "")

SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parent.parent
ENTRY = REPO_ROOT / "codelet" / "web" / "_backend_entry.py"

hidden = set(collect_submodules("codelet"))
hidden.update(
    [
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "websockets",
        "websockets.legacy",
        "websockets.legacy.server",
        "multipart",
        "python_multipart",
        "yaml",
        "httpx",
        "httpcore",
        "anyio._backends._asyncio",
    ]
)

datas = [
    (str(REPO_ROOT / "codelet" / "web" / "static"), "codelet/web/static"),
    (str(REPO_ROOT / "codelet" / "skills" / "builtin"), "codelet/skills/builtin"),
]

a = Analysis(
    [str(ENTRY)],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=sorted(hidden),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "datasets", "tkinter", "matplotlib", "scipy", "numpy", "pandas", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="codelet-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="codelet-backend",
)
