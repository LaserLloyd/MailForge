# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Windows fallback build (build spec §1, §10).

FOLDER-MODE only (`COLLECT`), NEVER onefile: onefile triggers AV false
positives and has a slow cold start (spec §1/§10). The resulting folder is then
wrapped by Inno Setup (see installer/inno/siftforge.iss), and the
distributable should be code-signed to further reduce AV false positives.

Build (Windows, after `uv pip install pyinstaller`):

    pyinstaller packaging/pyinstaller.spec

Output: dist/siftforge/  (the folder Inno Setup packages).
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Repo root = parent of packaging/ (spec uses this spec file at packaging/).
REPO = Path(os.getcwd())
SRC = REPO / "src" / "siftforge"

# Bundle non-Python package assets the runtime reads via importlib.resources:
#  - db/schema.sql            (§7 schema)
#  - llm/prompts/*            (versioned spotlight prompt templates)
datas = []
datas += [(str(SRC / "db" / "schema.sql"), "siftforge/db")]
datas += [(str(SRC / "llm" / "prompts"), "siftforge/llm/prompts")]
# Installer assets the service installers read at runtime (systemd template is
# Linux-only but harmless to ship; WinSW exe/descriptor are needed on Windows).
datas += [(str(REPO / "installer"), "installer")]
# pull in package-data declared by deps where PyInstaller can detect it
datas += collect_data_files("nicegui")

# Hidden imports: optional/deferred modules loaded lazily. Collect the package's
# own submodules so dynamic imports (security/*, service/*) are bundled.
hiddenimports = []
hiddenimports += collect_submodules("siftforge")

block_cipher = None

a = Analysis(
    [str(SRC / "cli.py")],
    pathex=[str(REPO / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Heavy optional ML deps (torch/transformers/presidio) are NOT bundled by
    # default — the package degrades gracefully without them (spec §1). Add them
    # here only for a "batteries-included" build.
    excludes=["torch", "transformers", "presidio_analyzer", "presidio_anonymizer"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # FOLDER-MODE: binaries collected by COLLECT below
    name="siftforge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX raises AV false positives; leave off for signing.
    console=True,
    # codesign_identity / icon set in CI before signing.
)

# COLLECT == folder-mode distribution (the explicit opposite of onefile).
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="siftforge",
)
