# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Phonos standalone executables (Windows --onedir & Linux Binary).

Bundles templates/, static/, examples/, phonos_app/, production/, and phonos_lib/
so Playground (:5040) and Production (:5041) run without a separate Python install.
"""
import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = []
binaries = []
for rel in ("templates", "static", "examples"):
    src = ROOT / rel
    if src.is_dir():
        datas.append((str(src), rel))

# Direct-loaded route/socket modules loaded via spec_from_file_location in phonos_app/app.py
for direct_py, dest_dir in (
    ("phonos_app/socket_handlers_notebook.py", "phonos_app"),
    ("phonos_app/helpers/local_kernel_worker.py", "phonos_app/helpers"),
    ("phonos_app/routes/stripe_routes.py", "phonos_app/routes"),
    ("phonos_app/routes/api_databases.py", "phonos_app/routes"),
    ("phonos_app/routes/api_analysis.py", "phonos_app/routes"),
    ("phonos_app/routes/api_keys_management.py", "phonos_app/routes"),
    ("phonos_app/routes/api_webhooks_new.py", "phonos_app/routes"),
    ("phonos_app/routes/api_user_models.py", "phonos_app/routes"),
):
    py_src = ROOT / direct_py
    if py_src.is_file():
        datas.append((str(py_src), dest_dir))

for doc_name in ("LICENSE", "DISCLAIMER.md", "THIRD_PARTY_LICENSES.md"):
    doc_path = ROOT / doc_name
    if doc_path.is_file():
        datas.append((str(doc_path), "."))

for pkg in ("certifi", "rdkit", "Bio"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

hiddenimports = [
    *collect_submodules("phonos_app"),
    *collect_submodules("production"),
    *collect_submodules("phonos_lib"),
    *collect_submodules("flask_socketio"),
    *collect_submodules("engineio"),
    *collect_submodules("socketio"),
    *collect_submodules("eventlet"),
    "engineio.async_drivers.threading",
    "engineio.async_drivers.eventlet",
    "dns",
    "dns.resolver",
    "bcrypt",
    "jwt",
    "cryptography",
    "Crypto",
    "sqlite3",
    "psycopg2",
]

USE_ONEDIR = sys.platform == "win32" or os.environ.get("PHONOS_PYINSTALLER_ONEDIR") == "1"

a = Analysis(
    [str(ROOT / "packaging" / "entrypoint.py")],
    pathex=[str(ROOT), str(ROOT / "production")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if USE_ONEDIR:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Phonos" if sys.platform == "win32" else "phonos",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="phonos",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="phonos",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
    )
