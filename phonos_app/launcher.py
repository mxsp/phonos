"""Unified Playground & Production launcher for Phonos (CLI, Docker, and Executable).

Supports isolated localnet deployments out of the box (modeled on Gimel):

* **playground** — Pre-seeded sandbox lab ("Phonos Sandbox Lab") in ``<base>/playground``
  with SQLite, demo credentials printed on startup, a pre-loaded 3D mini-protein PDB
  structure & starter Python notebook, and ``--reset`` support (default port ``5040``).
* **production** — Clean production database in ``<base>/production`` (or PostgreSQL
  via ``DATABASE_URL``), optional initial admin bootstrap via ``--admin-user`` /
  ``--admin-password`` (or browser First-Run Setup Wizard at ``/setup``), default port ``5041``.
* **both** — Starts Playground (``:5040``) and Production (``:5041``) side by side in
  isolated subprocesses.
* **setup-ui** — Starts the zero-dependency guided Browser GUI Installer.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Literal

Mode = Literal["playground", "production", "both", "setup-ui"]

DEFAULT_HOST = "0.0.0.0"
PLAYGROUND_PORT = 5040
PRODUCTION_PORT = 5041

DEMO_ADMIN_USER = "demo"
DEMO_SCIENTIST_USER = "marie"

# Compact folded alpha-helical mini-protein structure (Trp-cage motif, 20 residues) with realistic pLDDT B-factors
DEMO_PDB_CONTENT = """HEADER    DE NOVO DESIGN / MINI-PROTEIN           25-NOV-26   PHNS
TITLE     PHONOS SANDBOX MINI-PROTEIN (TRP-CAGE FOLD WITH PLDDT CONFIDENCE)
ATOM      1  N   ASN A   1      -8.901   4.127  -0.555  1.00 84.20           N
ATOM      2  CA  ASN A   1      -8.608   3.135  -1.618  1.00 86.50           C
ATOM      3  C   ASN A   1      -7.117   2.964  -1.897  1.00 87.10           C
ATOM      4  O   ASN A   1      -6.632   1.848  -2.092  1.00 86.80           O
ATOM      5  N   LEU A   2      -6.379   4.109  -1.922  1.00 91.40           N
ATOM      6  CA  LEU A   2      -4.923   4.102  -2.164  1.00 92.80           C
ATOM      7  C   LEU A   2      -4.161   3.289  -1.119  1.00 93.10           C
ATOM      8  O   LEU A   2      -3.194   2.593  -1.432  1.00 92.90           O
ATOM      9  N   TYR A   3      -4.588   3.382   0.138  1.00 94.60           N
ATOM     10  CA  TYR A   3      -3.952   2.633   1.224  1.00 95.20           C
ATOM     11  C   TYR A   3      -4.165   1.127   1.076  1.00 95.40           C
ATOM     12  O   TYR A   3      -3.235   0.349   1.281  1.00 95.10           O
ATOM     13  N   ILE A   4      -5.388   0.718   0.728  1.00 96.10           N
ATOM     14  CA  ILE A   4      -5.712  -0.696   0.548  1.00 96.80           C
ATOM     15  C   ILE A   4      -4.812  -1.362  -0.492  1.00 96.40           C
ATOM     16  O   ILE A   4      -4.198  -2.398  -0.228  1.00 96.20           O
ATOM     17  N   GLN A   5      -4.738  -0.761  -1.678  1.00 94.90           N
ATOM     18  CA  GLN A   5      -3.912  -1.298  -2.762  1.00 94.30           C
ATOM     19  C   GLN A   5      -2.442  -1.084  -2.428  1.00 93.80           C
ATOM     20  O   GLN A   5      -1.624  -1.988  -2.612  1.00 93.50           O
ATOM     21  N   TRP A   6      -2.108   0.112  -1.938  1.00 97.20           N
ATOM     22  CA  TRP A   6      -0.732   0.468  -1.578  1.00 97.60           C
ATOM     23  C   TRP A   6      -0.212  -0.412  -0.448  1.00 97.10           C
ATOM     24  O   TRP A   6       0.948  -0.824  -0.478  1.00 96.90           O
ATOM     25  N   LEU A   7      -1.078  -0.708   0.522  1.00 95.30           N
ATOM     26  CA  LEU A   7      -0.712  -1.548   1.662  1.00 94.70           C
ATOM     27  C   LEU A   7       0.128  -2.748   1.232  1.00 92.40           C
ATOM     28  O   LEU A   7       1.112  -3.092   1.888  1.00 91.80           O
ATOM     29  N   LYS A   8      -0.262  -3.382   0.128  1.00 88.50           N
ATOM     30  CA  LYS A   8       0.462  -4.542  -0.382  1.00 86.20           C
ATOM     31  C   LYS A   8       1.942  -4.238  -0.612  1.00 83.40           C
ATOM     32  O   LYS A   8       2.768  -5.142  -0.528  1.00 82.10           O
ATOM     33  N   ASP A   9       2.272  -2.972  -0.892  1.00 76.40           N
ATOM     34  CA  ASP A   9       3.658  -2.568  -1.142  1.00 72.80           C
ATOM     35  C   ASP A   9       4.212  -1.692  -0.018  1.00 68.50           C
ATOM     36  O   ASP A   9       5.418  -1.442   0.042  1.00 66.90           O
ATOM     37  N   GLY A  10       3.328  -1.228   0.862  1.00 63.20           N
ATOM     38  CA  GLY A  10       3.738  -0.382   1.978  1.00 61.50           C
ATOM     39  C   GLY A  10       4.428   0.892   1.522  1.00 64.80           C
ATOM     40  O   GLY A  10       5.318   1.412   2.192  1.00 65.20           O
ATOM     41  N   GLY A  11       4.012   1.392   0.362  1.00 74.10           N
ATOM     42  CA  GLY A  11       4.598   2.612  -0.182  1.00 78.60           C
ATOM     43  C   GLY A  11       3.612   3.762  -0.092  1.00 82.30           C
ATOM     44  O   GLY A  11       2.428   3.588  -0.392  1.00 83.90           O
ATOM     45  N   PRO A  12       4.082   4.952   0.312  1.00 87.40           N
ATOM     46  CA  PRO A  12       3.218   6.128   0.428  1.00 89.10           C
ATOM     47  C   PRO A  12       1.882   5.862   1.118  1.00 90.20           C
ATOM     48  O   PRO A  12       0.852   6.442   0.772  1.00 90.50           O
END
"""


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_sys_path() -> None:
    root = str(_repo_root())
    prod = str(_repo_root() / "production")
    for p in (prod, root):
        if p not in sys.path:
            sys.path.insert(0, p)


def _ensure_valid_stdio(base_dir: Path) -> None:
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir = (
            Path(os.environ.get("LOCALAPPDATA", str(base_dir))) / "Phonos" / "logs"
            if os.name == "nt"
            else base_dir / "logs"
        )
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / "phonos-launcher.log").open("a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file  # type: ignore[assignment]
        if sys.stderr is None:
            sys.stderr = log_file  # type: ignore[assignment]
    except OSError:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull  # type: ignore[assignment]
        if sys.stderr is None:
            sys.stderr = devnull  # type: ignore[assignment]


def _default_base_dir() -> Path:
    env_dir = os.environ.get("PHONOS_BASE_DATA") or os.environ.get("PHONOS_HOME")
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    explicit_data = os.environ.get("PHONOS_DATA_DIR")
    if explicit_data:
        return Path(explicit_data).expanduser().resolve()
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Phonos" / "data"
        return Path.home() / ".local" / "share" / "phonos"
    return (_repo_root() / ".data").resolve()


def _mode_data_dir(base: Path, mode: Literal["playground", "production"]) -> Path:
    if base.name in ("playground", "production") or os.environ.get("PHONOS_DATA_EXACT") == "1":
        target = base
    else:
        target = base / mode
    target.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def _ensure_key_file(data_dir: Path, filename: str, env_var: str) -> str:
    existing = os.environ.get(env_var, "").strip()
    if existing and len(existing) >= 32:
        return existing
    key_file = data_dir / filename
    if key_file.is_file():
        key = key_file.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            os.environ[env_var] = key
            return key
    key = secrets.token_urlsafe(48)
    key_file.write_text(key, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    os.environ[env_var] = key
    return key


def _postgres_url_reachable(db_url: str) -> bool:
    import socket
    from urllib.parse import urlparse
    try:
        parsed = urlparse(db_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _configure_env_before_import(mode: Literal["playground", "production"], data_dir: Path) -> None:
    os.environ["PHONOS_MODE"] = mode
    os.environ["PHONOS_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("LOCAL_LAB_MODE", "true")
    os.environ.setdefault("ENABLE_2FA", "false")
    os.environ.setdefault("REQUIRE_TLS", "false")
    os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
    os.environ.setdefault("GEOBLOCK_ENABLED", "false")
    os.environ.setdefault("DEV", "true" if mode == "playground" else "false")

    job_dir = data_dir / "job_outputs"
    fallback_dir = data_dir / "job_outputs_fallback"
    job_dir.mkdir(parents=True, exist_ok=True)
    fallback_dir.mkdir(parents=True, exist_ok=True)
    os.environ["JOB_OUTPUT_DIR"] = str(job_dir)
    os.environ["JOB_OUTPUT_DIR_FALLBACK"] = str(fallback_dir)

    _ensure_key_file(data_dir, "secret.key", "SECRET_KEY")
    _ensure_key_file(data_dir, "encryption.key", "ENCRYPTION_KEY")

    db_file = (data_dir / "phonos.db").resolve()
    sqlite_url = f"sqlite:///{db_file.as_posix()}"

    existing_db_url = os.environ.get("DATABASE_URL", "").strip()
    use_sqlite = False
    if mode == "playground" or not existing_db_url or existing_db_url.startswith("sqlite:"):
        use_sqlite = True
    elif (
        existing_db_url.startswith(("postgresql://", "postgres://"))
        and os.environ.get("PHONOS_IN_DOCKER") != "1"
        and os.environ.get("PHONOS_STRICT_DB") != "1"
        and not _postgres_url_reachable(existing_db_url)
    ):
        print(
            f"[Phonos] PostgreSQL at DATABASE_URL is not running; using standalone SQLite in {db_file}",
            file=sys.stderr,
        )
        use_sqlite = True

    if use_sqlite:
        os.environ["DATABASE_URL"] = sqlite_url

    # Sync already-imported Config / Flask app if cli.py was invoked via `python -m phonos_app.cli`
    _ensure_sys_path()
    for mod_name in ("production.config", "config"):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "Config"):
            cfg = mod.Config
            cfg.DATABASE_URL = os.environ["DATABASE_URL"]
            if use_sqlite:
                cfg.DATABASE_FILE = str(db_file)
            cfg.JOB_OUTPUT_DIR = str(job_dir)
            cfg.JOB_OUTPUT_DIR_FALLBACK = str(fallback_dir)
            cfg.LOCAL_LAB_MODE = os.environ.get("LOCAL_LAB_MODE", "true").lower() == "true"
            cfg.ENABLE_2FA = os.environ.get("ENABLE_2FA", "false").lower() == "true"

    app_mod = sys.modules.get("phonos_app.app")
    if app_mod and hasattr(app_mod, "app"):
        flask_app = app_mod.app
        flask_app.config["DATABASE_URL"] = os.environ["DATABASE_URL"]
        if use_sqlite:
            flask_app.config["DATABASE_FILE"] = str(db_file)
        flask_app.config["JOB_OUTPUT_DIR"] = str(job_dir)
        flask_app.config["JOB_OUTPUT_DIR_FALLBACK"] = str(fallback_dir)
        flask_app.config["SESSION_COOKIE_NAME"] = f"phonos_{mode}_session"


def _seed_playground_demo_project(db, admin_id: int, scientist_id: int, team_id: int | None, data_dir: Path) -> None:
    existing = db.execute("SELECT id FROM projects WHERE creator_id = %s", (admin_id,)).fetchone()
    if existing:
        return

    cur = db.execute(
        """
        INSERT INTO projects (team_id, creator_id, name, description, is_private)
        VALUES (%s, %s, %s, %s, 1)
        RETURNING id
        """,
        (
            team_id,
            admin_id,
            "Trp-Cage Mini-Protein Sandbox",
            "Pre-seeded Playground project with a 3D mini-protein structure (pLDDT B-factors) and starter Python notebook.",
        ),
    )
    row = cur.fetchone()
    project_id = int((row["id"] if isinstance(row, dict) or hasattr(row, "keys") else row[0]) if row else cur.lastrowid)

    db.execute(
        "INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, 'Project Manager')",
        (project_id, admin_id),
    )
    db.execute(
        "INSERT INTO project_assignments (project_id, user_id, role) VALUES (%s, %s, 'Project Contributor')",
        (project_id, scientist_id),
    )

    proj_folder = data_dir / "job_outputs" / f"project_{project_id}"
    proj_folder.mkdir(parents=True, exist_ok=True)

    pdb_path = proj_folder / "trp_cage_design.pdb"
    pdb_path.write_text(DEMO_PDB_CONTENT, encoding="utf-8")

    nb_payload = {
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": [
                    "# Phonos Playground — Zero-Docker Molecular Notebook\n",
                    "Run Python, RDKit, and 3D structure helpers directly in the built-in local kernel."
                ],
            },
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "# Render a 2D ligand + compute Lipinski descriptors\n",
                    "show_smiles('CC(=O)Oc1ccccc1C(=O)O', width=360, height=220)\n",
                    "\n",
                    "# Render the pre-seeded mini-protein in interactive 3D\n",
                    f"show_structure(r'{pdb_path.as_posix()}', fmt='pdb')\n"
                ],
            },
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3 (Local)", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    nb_path = proj_folder / "sandbox_discovery.ipynb"
    nb_path.write_text(json.dumps(nb_payload, indent=2), encoding="utf-8")

    for fpath, fname, note in (
        (pdb_path, "trp_cage_design.pdb", "Sample folded mini-protein with pLDDT confidence values in B-factor column"),
        (nb_path, "sandbox_discovery.ipynb", "Interactive RDKit + 3D structure starter notebook"),
    ):
        db.execute(
            """
            INSERT INTO files (project_id, creator_id, name, path, size, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (project_id, admin_id, fname, str(fpath), fpath.stat().st_size, note),
        )

    starter_cells = [
        {
            "id": "cell-welcome-md",
            "type": "markdown",
            "source": "# Phonos Playground — Zero-Docker Molecular Notebook\nRun Python, RDKit, and 3D structure helpers directly in the built-in local kernel.",
        },
        {
            "id": "cell-rdkit-3d",
            "type": "code",
            "source": f"# Render a 2D ligand + compute Lipinski descriptors\nshow_smiles('CC(=O)Oc1ccccc1C(=O)O', width=360, height=220)\n\n# Render the pre-seeded mini-protein in interactive 3D\nshow_structure(r'{pdb_path.as_posix()}', fmt='pdb')",
            "outputs": [],
        },
    ]
    try:
        db.execute(
            """
            INSERT INTO notebooks (project_id, user_id, creator_id, name, description, kernel_type, cells)
            VALUES (%s, %s, %s, %s, %s, 'python3', %s)
            """,
            (
                project_id,
                admin_id,
                admin_id,
                "Trp-Cage 3D & RDKit Starter",
                "Pre-seeded interactive molecular discovery notebook",
                json.dumps(starter_cells),
            ),
        )
    except Exception:
        pass
    db.commit()


def _prepare_playground(data_dir: Path, reset: bool) -> tuple[str, str]:
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = data_dir / f"phonos.db{suffix}"
            if p.exists():
                p.unlink()
        cred_old = data_dir / "demo-login.txt"
        if cred_old.exists():
            cred_old.unlink()
        shutil.rmtree(data_dir / "job_outputs", ignore_errors=True)
        (data_dir / "job_outputs").mkdir(parents=True, exist_ok=True)

    _ensure_sys_path()
    from production.main import app, ensure_database
    from phonos_app.helpers.database import get_db
    from phonos_app.helpers.lab_accounts import (
        ROLE_ADMIN,
        ROLE_SCIENTIST,
        create_local_account,
        ensure_default_lab_team,
        ensure_lab_accounts_schema,
        set_lab_setting,
    )

    ensure_database()
    cred_file = data_dir / "demo-login.txt"

    with app.app_context():
        db = get_db()
        ensure_lab_accounts_schema(db)
        cnt_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        has_users = int((cnt_row["cnt"] if isinstance(cnt_row, dict) or hasattr(cnt_row, "keys") else cnt_row[0]) or 0) > 0
        if not has_users:
            demo_pw = f"Phonos-{secrets.token_urlsafe(9)}"
            sci_pw = f"Helix-{secrets.token_urlsafe(9)}"
            admin_acct = create_local_account(
                db,
                username=DEMO_ADMIN_USER,
                password=demo_pw,
                system_role=ROLE_ADMIN,
                legal_name="Demo Lab Admin",
                actor="playground-seed",
            )
            team_id = ensure_default_lab_team(db, admin_acct["id"], lab_name="Phonos Sandbox Lab")
            sci_acct = create_local_account(
                db,
                username=DEMO_SCIENTIST_USER,
                password=sci_pw,
                system_role=ROLE_SCIENTIST,
                legal_name="Dr. Marie Curie (Demo)",
                lab_team_role="contributor",
                actor="playground-seed",
            )
            set_lab_setting(db, "is_playground_demo", "1")
            db.commit()
            _seed_playground_demo_project(db, admin_acct["id"], sci_acct["id"], team_id, data_dir)

            cred_file.write_text(
                f"{DEMO_ADMIN_USER}\n{demo_pw}\n# Secondary scientist login: {DEMO_SCIENTIST_USER} / {sci_pw}\n",
                encoding="utf-8",
            )
            try:
                os.chmod(cred_file, 0o600)
            except OSError:
                pass
            return DEMO_ADMIN_USER, demo_pw

    if cred_file.is_file():
        lines = cred_file.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 2:
            return lines[0].strip(), lines[1].strip()
    return DEMO_ADMIN_USER, f"(see {cred_file})"


def _prepare_production(
    admin_user: str,
    admin_password: str,
    admin_name: str,
    lab_name: str,
) -> int:
    _ensure_sys_path()
    from production.main import app, ensure_database
    from phonos_app.helpers.database import get_db
    from phonos_app.helpers.lab_accounts import (
        ROLE_ADMIN,
        check_password_strength,
        create_local_account,
        ensure_default_lab_team,
        ensure_lab_accounts_schema,
        get_lab_setting,
        normalize_username,
    )
    import bcrypt

    ensure_database()
    with app.app_context():
        db = get_db()
        ensure_lab_accounts_schema(db)
        if get_lab_setting(db, "is_playground_demo", "") == "1":
            print(
                "ERROR: The Production data folder contains a Playground demo database. "
                "Please use a clean data directory or PostgreSQL (DATABASE_URL).",
                file=sys.stderr,
            )
            return 2

        cnt_row = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
        user_count = int((cnt_row["cnt"] if isinstance(cnt_row, dict) or hasattr(cnt_row, "keys") else cnt_row[0]) or 0)

        username = (
            admin_user
            or os.environ.get("PHONOS_ADMIN_USER", "")
            or os.environ.get("PHONOS_ADMIN_EMAIL", "")
        ).strip()
        pw = (admin_password or os.environ.get("PHONOS_ADMIN_PASSWORD", "")).strip()
        display_name = (admin_name or os.environ.get("PHONOS_ADMIN_NAME", "") or username).strip()

        if username and pw:
            try:
                norm_u = normalize_username(username)
                check_password_strength(pw, username=norm_u)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2

            existing = db.execute("SELECT id FROM users WHERE LOWER(username) = %s", (norm_u,)).fetchone()
            if existing:
                uid = int(existing["id"] if isinstance(existing, dict) or hasattr(existing, "keys") else existing[0])
                hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt())
                db.execute("UPDATE user_credentials SET password = %s, email_verified = 1 WHERE user_id = %s", (hashed, uid))
                db.execute("UPDATE users SET is_admin = 1, system_role = 'admin', is_active = 1 WHERE id = %s", (uid,))
                db.commit()
                ensure_default_lab_team(db, uid, lab_name=lab_name or "Phonos Lab")
                db.commit()
                print(f"[Production] Admin account updated & unlocked: {norm_u}")
            else:
                acct = create_local_account(
                    db,
                    username=norm_u,
                    password=pw,
                    system_role=ROLE_ADMIN,
                    legal_name=display_name or norm_u,
                    actor="cli-bootstrap",
                )
                ensure_default_lab_team(db, acct["id"], lab_name=lab_name or "Phonos Lab")
                db.commit()
                print(f"[Production] Admin account created: {norm_u}")
        elif user_count == 0:
            print(
                "[Production] No user accounts yet — create your first Admin account in the browser at /setup\n"
                "  or via CLI: `phonos accounts add admin --role admin`",
                file=sys.stderr,
            )
    return 0


def run_single(
    mode: Literal["playground", "production"],
    *,
    host: str,
    port: int,
    base_dir: Path,
    reset: bool = False,
    no_browser: bool = False,
    init_only: bool = False,
    admin_user: str = "",
    admin_password: str = "",
    admin_name: str = "Administrator",
    lab_name: str = "Phonos Lab",
) -> int:
    _ensure_valid_stdio(base_dir)
    data_dir = _mode_data_dir(base_dir, mode)
    _configure_env_before_import(mode, data_dir)
    _ensure_sys_path()

    from phonos_app.helpers.lab_accounts import local_lan_ips

    local_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    local_url = f"http://{local_host}:{port}"
    lan_urls = [f"http://{ip}:{port}" for ip in local_lan_ips()] if host in ("0.0.0.0", "::") else []

    if mode == "playground":
        user, password = _prepare_playground(data_dir, reset=reset)
        if init_only:
            return 0
        print("=" * 68)
        print("  PHONOS PLAYGROUND (Sandbox Lab · Pre-Seeded 3D & Notebook)")
        print(f"  Local:    {local_url}/login")
        for lan_url in lan_urls:
            print(f"  LAN:      {lan_url}/login")
        print(f"  Data:     {data_dir}")
        print(f"  Username: {user}")
        print(f"  Password: {password}")
        print("=" * 68)
        open_target = f"{local_url}/login"
    else:
        rc = _prepare_production(admin_user, admin_password, admin_name, lab_name)
        if rc != 0 or init_only:
            return rc
        from production.main import app as _app
        from phonos_app.helpers.database import get_db
        with _app.app_context():
            cnt_row = get_db().execute("SELECT COUNT(*) as cnt FROM users").fetchone()
            needs_setup = int((cnt_row["cnt"] if isinstance(cnt_row, dict) or hasattr(cnt_row, "keys") else cnt_row[0]) or 0) == 0
        entry_path = "/setup" if needs_setup else ""
        print("=" * 68)
        print("  PHONOS PRODUCTION (Self-Hosted Lab Instance)")
        print(f"  Local:    {local_url}{entry_path}")
        for lan_url in lan_urls:
            print(f"  LAN:      {lan_url}{entry_path}")
        print(f"  Database: {os.environ.get('DATABASE_URL') or data_dir}")
        if needs_setup:
            print(f"  Setup:    Open {local_url}/setup in your browser to create the first Admin")
        print("=" * 68)
        open_target = f"{local_url}{entry_path}"

    from production.main import app
    from phonos_app import socketio
    from phonos_app.database_updater import start_job_update_listener
    import phonos_app.routes  # noqa: F401

    app.config["SESSION_COOKIE_NAME"] = f"phonos_{mode}_session"
    start_job_update_listener()

    if not no_browser and os.environ.get("PHONOS_NO_BROWSER") != "1":
        threading.Timer(1.3, lambda: webbrowser.open(open_target)).start()

    socketio.run(
        app,
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
    return 0


def run_both(
    *,
    host: str,
    playground_port: int,
    production_port: int,
    base_dir: Path,
    reset: bool = False,
    no_browser: bool = False,
    admin_user: str = "",
    admin_password: str = "",
    admin_name: str = "",
) -> int:
    _ensure_valid_stdio(base_dir)
    # Stop any APScheduler started when phonos_app was imported in this supervisor process
    app_mod = sys.modules.get("phonos_app.app")
    if app_mod and hasattr(app_mod, "scheduler"):
        try:
            app_mod.scheduler.shutdown(wait=False)
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        base_cmd = [sys.executable]
    else:
        base_cmd = [sys.executable, "-m", "phonos_app.launcher"]

    pg_cmd = [
        *base_cmd,
        "--mode", "playground",
        "--host", host,
        "--port", str(playground_port),
        "--data-dir", str(base_dir),
    ]
    if reset:
        pg_cmd.append("--reset")
    if no_browser:
        pg_cmd.append("--no-browser")

    prod_cmd = [
        *base_cmd,
        "--mode", "production",
        "--host", host,
        "--port", str(production_port),
        "--data-dir", str(base_dir),
        "--no-browser",
    ]
    if admin_user and admin_password:
        prod_cmd += ["--admin-user", admin_user, "--admin-password", admin_password]
        if admin_name:
            prod_cmd += ["--admin-name", admin_name]

    print(f"Starting Phonos Playground (:{playground_port}) and Production (:{production_port}) side by side…")
    procs = [subprocess.Popen(pg_cmd), subprocess.Popen(prod_cmd)]

    def _shutdown(*_args):
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        while all(proc.poll() is None for proc in procs):
            for proc in procs:
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
    except KeyboardInterrupt:
        _shutdown()
    finally:
        _shutdown()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phonos-launcher",
        description="Phonos — Unified Playground & Production Launcher",
    )
    p.add_argument(
        "--mode",
        choices=("playground", "production", "both", "setup-ui"),
        default=os.environ.get("PHONOS_MODE", "playground"),
        help="Runtime mode: 'playground' (demo sandbox), 'production' (clean lab), 'both', or 'setup-ui'",
    )
    p.add_argument("--host", default=os.environ.get("PHONOS_HOST", DEFAULT_HOST))
    p.add_argument("--port", type=int, default=int(os.environ.get("PHONOS_PORT", "0") or 0))
    p.add_argument("--playground-port", type=int, default=PLAYGROUND_PORT)
    p.add_argument("--production-port", type=int, default=PRODUCTION_PORT)
    p.add_argument("--data-dir", default="", help="Base data directory containing playground/ and production/")
    p.add_argument("--reset", action="store_true", help="Reset Playground database & demo files before start")
    p.add_argument("--init-only", action="store_true", help="Initialize database/accounts and exit without serving")
    p.add_argument(
        "--no-browser",
        action="store_true",
        default=os.environ.get("PHONOS_NO_BROWSER") == "1",
        help="Do not automatically open the web browser",
    )
    p.add_argument("--admin-user", "--admin-email", dest="admin_user", default="")
    p.add_argument("--admin-password", default="")
    p.add_argument("--admin-name", default="Administrator")
    p.add_argument("--lab-name", default=os.environ.get("PHONOS_LAB_NAME", "Phonos Lab"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_dir = Path(args.data_dir).expanduser().resolve() if args.data_dir else _default_base_dir()

    if args.mode == "setup-ui":
        install_script = _repo_root() / "scripts" / "install-ui.py"
        if install_script.is_file():
            cmd = [sys.executable, str(install_script)]
            if args.port:
                cmd += ["--port", str(args.port)]
            if args.no_browser:
                cmd.append("--no-browser")
            return subprocess.call(cmd)
        print("Error: scripts/install-ui.py not found.", file=sys.stderr)
        return 1

    if args.mode == "both":
        return run_both(
            host=args.host,
            playground_port=args.playground_port,
            production_port=args.production_port,
            base_dir=base_dir,
            reset=args.reset,
            no_browser=args.no_browser,
            admin_user=args.admin_user,
            admin_password=args.admin_password,
            admin_name=args.admin_name,
        )

    port = args.port or (PLAYGROUND_PORT if args.mode == "playground" else PRODUCTION_PORT)
    return run_single(
        args.mode,
        host=args.host,
        port=port,
        base_dir=base_dir,
        reset=args.reset,
        no_browser=args.no_browser,
        init_only=args.init_only,
        admin_user=args.admin_user,
        admin_password=args.admin_password,
        admin_name=args.admin_name,
        lab_name=args.lab_name,
    )


if __name__ == "__main__":
    raise SystemExit(main())
