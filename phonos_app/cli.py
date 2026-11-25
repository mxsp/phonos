"""``phonos`` unified CLI — Playground, Production, Account Manager, Setup & GitHub Updater."""
from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from .version import APP_VERSION
from . import launcher


def _init_production_db_context(data_dir: str = ""):
    base_dir = Path(data_dir).expanduser().resolve() if data_dir else launcher._default_base_dir()
    prod_dir = launcher._mode_data_dir(base_dir, "production")
    launcher._configure_env_before_import("production", prod_dir)
    launcher._ensure_sys_path()
    from production.main import app, ensure_database
    ensure_database()
    return app


def _accounts_cmd(args: argparse.Namespace) -> int:
    app = _init_production_db_context(getattr(args, "data_dir", ""))
    import bcrypt
    from .helpers.database import get_db
    from .helpers.lab_accounts import (
        CANONICAL_SYSTEM_ROLES,
        ROLE_ADMIN,
        assert_not_last_admin,
        check_password_strength,
        create_local_account,
        ensure_default_lab_team,
        ensure_lab_accounts_schema,
        normalize_system_role,
        normalize_username,
        record_account_audit,
        set_user_lab_team_role,
    )
    from .helpers.persistent_auth import revoke_all_remember_tokens, revoke_all_trust_devices
    from .helpers.security import AccountLockout

    action = args.account_action
    with app.app_context():
        db = get_db()
        ensure_lab_accounts_schema(db)

        if action == "list":
            rows = db.execute(
                """
                SELECT u.id, u.username, u.system_role, u.is_admin, u.is_active, u.credits, uc.email
                FROM users u
                LEFT JOIN user_credentials uc ON u.user_credential_id = uc.id
                ORDER BY u.username ASC, u.id ASC
                """
            ).fetchall()
            if not rows:
                print("No accounts yet. Create one with: phonos accounts add admin --role admin")
                return 0
            print(f"{'ID':<5} {'USERNAME':<24} {'ROLE':<12} {'STATUS':<10} {'CREDITS':<10} {'EMAIL'}")
            for r in rows:
                d = dict(r) if isinstance(r, dict) else {k: r[k] for k in r.keys()}
                role = "admin" if d.get("is_admin") else (d.get("system_role") or "scientist")
                st = "active" if (d.get("is_active", 1) in (1, True, None)) else "disabled"
                print(
                    f"{d['id']:<5} {str(d.get('username') or ''):<24} {role:<12} "
                    f"{st:<10} {str(d.get('credits') or 0):<10} {str(d.get('email') or '')}"
                )
            return 0

        try:
            username = normalize_username(args.username)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if action == "add":
            pw = args.password or getpass.getpass(f"Password for {username} (min. 8 chars): ")
            try:
                acct = create_local_account(
                    db,
                    username=username,
                    password=pw,
                    system_role=args.role,
                    email=getattr(args, "email", None),
                    legal_name=getattr(args, "name", None) or username,
                    lab_team_role=getattr(args, "lab_team_role", None),
                    actor="cli",
                )
                if acct["system_role"] == ROLE_ADMIN:
                    ensure_default_lab_team(db, acct["id"])
                    db.commit()
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            print(f"Created account '{acct['username']}' (role={acct['system_role']}, id={acct['id']})")
            return 0

        row = db.execute(
            "SELECT id, username, is_admin, system_role, is_active FROM users WHERE LOWER(username) = %s",
            (username,),
        ).fetchone()
        if not row:
            print(f"Error: account '{username}' not found.", file=sys.stderr)
            return 1
        user = dict(row) if isinstance(row, dict) else {k: row[k] for k in row.keys()}
        uid = int(user["id"])

        if action == "passwd":
            pw = args.password or getpass.getpass(f"New password for {username} (min. 8 chars): ")
            try:
                check_password_strength(pw, username=username)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            hashed = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt())
            db.execute("UPDATE user_credentials SET password = %s, email_verified = 1 WHERE user_id = %s", (hashed, uid))
            db.execute("DELETE FROM account_lockouts WHERE LOWER(username) = %s", (username,))
            revoke_all_remember_tokens(db, uid)
            revoke_all_trust_devices(db, uid)
            record_account_audit(db, "cli", "account_password_reset", uid, {"username": username})
            db.commit()
            print(f"Password updated for '{username}' (persistent sessions revoked).")
            return 0

        if action == "role":
            try:
                new_role = normalize_system_role(args.role)
                if new_role != ROLE_ADMIN:
                    assert_not_last_admin(db, uid)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            is_adm = 1 if new_role == ROLE_ADMIN else 0
            db.execute("UPDATE users SET system_role = %s, is_admin = %s WHERE id = %s", (new_role, is_adm, uid))
            record_account_audit(db, "cli", "account_role_change", uid, {"new_role": new_role})
            db.commit()
            print(f"Updated role for '{username}' -> {new_role}")
            return 0

        if action == "disable":
            try:
                assert_not_last_admin(db, uid)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            db.execute("UPDATE users SET is_active = 0 WHERE id = %s", (uid,))
            revoke_all_remember_tokens(db, uid)
            revoke_all_trust_devices(db, uid)
            record_account_audit(db, "cli", "account_disable", uid, {"username": username})
            db.commit()
            print(f"Account '{username}' disabled.")
            return 0

        if action == "enable":
            db.execute("UPDATE users SET is_active = 1 WHERE id = %s", (uid,))
            db.execute("DELETE FROM account_lockouts WHERE LOWER(username) = %s", (username,))
            record_account_audit(db, "cli", "account_enable", uid, {"username": username})
            db.commit()
            print(f"Account '{username}' enabled & unlocked.")
            return 0

        if action == "unlock":
            db.execute("DELETE FROM account_lockouts WHERE LOWER(username) = %s", (username,))
            record_account_audit(db, "cli", "account_unlock", uid, {"username": username})
            db.commit()
            print(f"Account '{username}' unlocked.")
            return 0

        if action == "delete":
            try:
                assert_not_last_admin(db, uid)
            except ValueError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 1
            if not getattr(args, "yes", False):
                ans = input(f"Permanently delete account '{username}'? [y/N]: ").strip().lower()
                if ans not in ("y", "yes"):
                    print("Cancelled.")
                    return 0
            db.execute("DELETE FROM user_credentials WHERE user_id = %s", (uid,))
            db.execute("DELETE FROM users WHERE id = %s", (uid,))
            record_account_audit(db, "cli", "account_delete", uid, {"username": username})
            db.commit()
            print(f"Deleted account '{username}'.")
            return 0

        if action == "team":
            mapped = set_user_lab_team_role(db, uid, args.lab_team_role)
            db.commit()
            print(f"Updated Default Lab Team role for '{username}' -> {mapped or 'assigned_only'}")
            return 0

    return 1


def _update_cmd(args: argparse.Namespace) -> int:
    from .helpers import updater

    st = updater.check_for_update(force=True)
    print(f"Phonos {st.current_version} ({st.install_kind}) — GitHub repo: {st.repo}")
    if st.error and not st.latest_version:
        print(f"Status: {st.error}")
        return 1 if args.apply else 0

    print(f"Latest release: {st.latest_tag or 'none'} ({'UPDATE AVAILABLE' if st.update_available else 'up to date'})")
    if st.release_url:
        print(f"Release page:   {st.release_url}")

    if args.apply:
        res = updater.apply_update(target_tag=args.tag or None, schedule_restart=False)
        print(res.detail)
        if res.command_hint:
            print(f"Hint: {res.command_hint}")
        return 0 if res.ok else 1
    return 0


def build_cli_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phonos",
        description=f"Phonos {APP_VERSION} — Molecular Discovery, 3D Structure & Bio-AI Workbench",
    )
    p.add_argument("--version", action="version", version=f"phonos {APP_VERSION}")
    sub = p.add_subparsers(dest="cmd")

    # playground
    pg = sub.add_parser("playground", help="Start isolated Playground sandbox (port 5040, pre-seeded demo)")
    pg.add_argument("--host", default=launcher.DEFAULT_HOST)
    pg.add_argument("--port", type=int, default=launcher.PLAYGROUND_PORT)
    pg.add_argument("--data-dir", default="")
    pg.add_argument("--reset", action="store_true", help="Reset Playground database & demo files")
    pg.add_argument("--no-browser", action="store_true")

    # production
    pr = sub.add_parser("production", help="Start clean Production lab instance (port 5041)")
    pr.add_argument("--host", default=launcher.DEFAULT_HOST)
    pr.add_argument("--port", type=int, default=launcher.PRODUCTION_PORT)
    pr.add_argument("--data-dir", default="")
    pr.add_argument("--no-browser", action="store_true")
    pr.add_argument("--admin-user", default="")
    pr.add_argument("--admin-password", default="")
    pr.add_argument("--lab-name", default="Phonos Lab")

    # serve (alias for production with custom port)
    sv = sub.add_parser("serve", help="Serve Phonos Production instance")
    sv.add_argument("--host", default=launcher.DEFAULT_HOST)
    sv.add_argument("--port", type=int, default=launcher.PRODUCTION_PORT)
    sv.add_argument("--data-dir", default="")
    sv.add_argument("--no-browser", action="store_true")

    # both
    bt = sub.add_parser("both", help="Run Playground (:5040) and Production (:5041) side by side")
    bt.add_argument("--host", default=launcher.DEFAULT_HOST)
    bt.add_argument("--playground-port", type=int, default=launcher.PLAYGROUND_PORT)
    bt.add_argument("--production-port", type=int, default=launcher.PRODUCTION_PORT)
    bt.add_argument("--data-dir", default="")
    bt.add_argument("--reset", action="store_true")
    bt.add_argument("--no-browser", action="store_true")

    # setup
    st = sub.add_parser("setup", help="Initialize Playground and/or Production databases without starting server")
    st.add_argument("--mode", choices=("playground", "production", "both"), default="both")
    st.add_argument("--data-dir", default="")
    st.add_argument("--reset", action="store_true")
    st.add_argument("--admin-user", default="")
    st.add_argument("--admin-password", default="")
    st.add_argument("--lab-name", default="Phonos Lab")

    # install-ui
    ui = sub.add_parser("install-ui", help="Launch zero-dependency Browser GUI Installer (http://127.0.0.1:8098)")
    ui.add_argument("--port", type=int, default=8098)
    ui.add_argument("--no-browser", action="store_true")

    # accounts
    ac = sub.add_parser("accounts", help="Manage local username + password Lab Accounts")
    ac.add_argument("--data-dir", default="", help="Override base data directory")
    ac_sub = ac.add_subparsers(dest="account_action", required=True)

    ac_sub.add_parser("list", help="List all accounts and Lab Roles")

    ac_add = ac_sub.add_parser("add", help="Create a new local account")
    ac_add.add_argument("username")
    ac_add.add_argument("--role", default="scientist", help="Lab role: admin, scientist, operator, viewer")
    ac_add.add_argument("--password", default="")
    ac_add.add_argument("--email", default="")
    ac_add.add_argument("--name", default="")
    ac_add.add_argument("--lab-team-role", default="", help="Default Lab Team role: none, contributor, viewer, manager")

    ac_pw = ac_sub.add_parser("passwd", help="Reset password for an account")
    ac_pw.add_argument("username")
    ac_pw.add_argument("--password", default="")

    ac_rl = ac_sub.add_parser("role", help="Change Lab Role for an account")
    ac_rl.add_argument("username")
    ac_rl.add_argument("role", help="admin | scientist | operator | viewer")

    ac_tm = ac_sub.add_parser("team", help="Set Default Lab Team inheritance for an account")
    ac_tm.add_argument("username")
    ac_tm.add_argument("lab_team_role", help="none | contributor | viewer | manager")

    for act_name, act_help in (
        ("enable", "Re-enable and unlock a disabled account"),
        ("disable", "Disable an account and revoke persistent sessions"),
        ("unlock", "Clear failed-login lockout for an account"),
    ):
        s_p = ac_sub.add_parser(act_name, help=act_help)
        s_p.add_argument("username")

    ac_del = ac_sub.add_parser("delete", help="Delete an account")
    ac_del.add_argument("username")
    ac_del.add_argument("-y", "--yes", action="store_true")

    # update
    up = sub.add_parser("update", help="Check or apply updates from GitHub Releases (mxsp/phonos)")
    up.add_argument("--check", action="store_true", help="Check GitHub Releases for newer version")
    up.add_argument("--apply", action="store_true", help="Download, verify SHA-256, and apply update")
    up.add_argument("--tag", default="", help="Optional release tag to update to (e.g. v1.1.0)")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "playground":
        base = Path(args.data_dir).expanduser().resolve() if args.data_dir else launcher._default_base_dir()
        return launcher.run_single(
            "playground",
            host=args.host,
            port=args.port,
            base_dir=base,
            reset=args.reset,
            no_browser=args.no_browser,
        )

    if args.cmd in ("production", "serve"):
        base = Path(args.data_dir).expanduser().resolve() if args.data_dir else launcher._default_base_dir()
        return launcher.run_single(
            "production",
            host=args.host,
            port=args.port,
            base_dir=base,
            no_browser=args.no_browser,
            admin_user=getattr(args, "admin_user", ""),
            admin_password=getattr(args, "admin_password", ""),
            lab_name=getattr(args, "lab_name", "Phonos Lab"),
        )

    if args.cmd == "both":
        base = Path(args.data_dir).expanduser().resolve() if args.data_dir else launcher._default_base_dir()
        return launcher.run_both(
            host=args.host,
            playground_port=args.playground_port,
            production_port=args.production_port,
            base_dir=base,
            reset=args.reset,
            no_browser=args.no_browser,
        )

    if args.cmd == "setup":
        base = Path(args.data_dir).expanduser().resolve() if args.data_dir else launcher._default_base_dir()
        rc = 0
        if args.mode in ("playground", "both"):
            rc = launcher.run_single(
                "playground",
                host="127.0.0.1",
                port=launcher.PLAYGROUND_PORT,
                base_dir=base,
                reset=args.reset,
                no_browser=True,
                init_only=True,
            )
            if rc != 0:
                return rc
            print(f"[Setup] Playground sandbox initialized in {base / 'playground'}")
        if args.mode in ("production", "both"):
            rc = launcher.run_single(
                "production",
                host="127.0.0.1",
                port=launcher.PRODUCTION_PORT,
                base_dir=base,
                no_browser=True,
                init_only=True,
                admin_user=args.admin_user,
                admin_password=args.admin_password,
                lab_name=args.lab_name,
            )
            if rc != 0:
                return rc
            print(f"[Setup] Production environment initialized in {base / 'production'}")
        return rc

    if args.cmd == "install-ui":
        script = launcher._repo_root() / "scripts" / "install-ui.py"
        if not script.is_file():
            print("Error: scripts/install-ui.py not found.", file=sys.stderr)
            return 1
        cmd = [sys.executable, str(script), "--port", str(args.port)]
        if args.no_browser:
            cmd.append("--no-browser")
        return subprocess.call(cmd)

    if args.cmd == "accounts":
        return _accounts_cmd(args)

    if args.cmd == "update":
        return _update_cmd(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
