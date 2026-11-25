"""Seal existing usage/personal columns with PHONOS_DATA_KEYS (phonos_app.helpers.data_crypto).

Columns covered: direct_messages.content, login_history.ip_address, login_history.user_agent
(+ login_history.device_idx, the keyed hash the new-device check searches by).

    python3 scripts/encrypt_usage_data.py                 dry run: counts only, writes nothing
    python3 scripts/encrypt_usage_data.py --apply         plaintext and old-key rows -> active key
    python3 scripts/encrypt_usage_data.py --verify        read every row back, write nothing
    python3 scripts/encrypt_usage_data.py --decrypt --apply   way back: plaintext again

Idempotent (rows already on the active key are skipped), resumable (keyset paging by id, one
transaction per batch). Order: back up the database, deploy the code (it adds login_history.device_idx
on boot), set PHONOS_DATA_KEYS, restart, THEN run this. The app Config is imported as a side effect, but ENCRYPTION_KEY is only read, never changed.
Reads DATABASE_URL from the environment / .env of the host it runs on.
"""
import argparse
import os
import sys
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "production"))
try:  # the app package reads its Config at import, so the host .env must be loaded first
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

from phonos_app.helpers import data_crypto as dc  # noqa: E402
from phonos_app.helpers.login_history import L_DEV, L_IP, L_UA, _device_text  # noqa: E402

DM = "direct_messages.content"


def _state(value, target):
    """empty | current | encrypt | reseal | decrypt for one stored value (target None = decrypt)."""
    if value in (None, ""):
        return "empty"
    kid = dc.key_id_of(value)
    if target is None:
        return "decrypt" if kid else "current"
    if kid is None:
        return "encrypt"
    return "current" if kid == target else "reseal"


def _rows(cur, ph, sql, last, batch):
    cur.execute(sql.format(where=f"WHERE id > {ph}", ph=ph), (last, batch))
    return cur.fetchall()


def run(conn, *, apply=False, decrypt=False, verify=False, batch=500, ph="%s", out=print):
    target = None if decrypt else dc.active_key_id()
    if not decrypt and not verify and target is None:
        raise SystemExit("PHONOS_DATA_KEYS is not set - nothing to seal.")
    total = Counter()
    cur = conn.cursor()

    def paged(table, cols):
        last = 0
        while True:
            cur.execute(f"SELECT id, {', '.join(cols)} FROM {table} WHERE id > {ph} ORDER BY id LIMIT {ph}", (last, batch))
            rows = cur.fetchall()
            if not rows:
                return
            yield rows
            last = rows[-1][0]

    # ---- direct_messages.content
    counts = Counter()
    for rows in paged("direct_messages", ["content"]):
        for rid, content in rows:
            if verify:
                counts["read"] += 1
                if content:
                    dc.unseal(DM, content)
                continue
            st = _state(content, target)
            counts[st] += 1
            if apply and st in ("encrypt", "reseal", "decrypt"):
                plain = dc.unseal(DM, content)
                new = dc.as_plain(plain) if decrypt else dc.seal(DM, plain)
                cur.execute(f"UPDATE direct_messages SET content = {ph} WHERE id = {ph}", (new, rid))
        if apply:
            conn.commit()
    out(f"  {DM}: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))
    total.update(counts)

    # ---- login_history: ip_address, user_agent, device_idx
    counts = Counter()
    for rows in paged("login_history", ["ip_address", "user_agent", "device_idx"]):
        for rid, ip, ua, idx in rows:
            if verify:
                counts["read"] += 1
                dc.unseal(L_IP, ip or ""); dc.unseal(L_UA, ua or "")
                continue
            sts = {_state(ip, target), _state(ua, target)} - {"empty"}
            if decrypt:
                st = "decrypt" if ("decrypt" in sts or idx) else "current"
            else:
                plain_ip, plain_ua = dc.unseal(L_IP, ip), dc.unseal(L_UA, ua)
                want_idx = dc.blind_index(L_DEV, _device_text(plain_ip, plain_ua))
                st = ("encrypt" if "encrypt" in sts else "reseal" if "reseal" in sts
                      else "current" if idx == want_idx else "reseal")
            counts[st] += 1
            if apply and st in ("encrypt", "reseal", "decrypt"):
                plain_ip, plain_ua = dc.unseal(L_IP, ip), dc.unseal(L_UA, ua)
                if decrypt:
                    vals = (dc.as_plain(plain_ip), dc.as_plain(plain_ua), None)
                else:
                    vals = (dc.seal(L_IP, plain_ip), dc.seal(L_UA, plain_ua),
                            dc.blind_index(L_DEV, _device_text(plain_ip, plain_ua)))
                cur.execute(f"UPDATE login_history SET ip_address = {ph}, user_agent = {ph}, device_idx = {ph} WHERE id = {ph}",
                            (*vals, rid))
        if apply:
            conn.commit()
    out("  login_history: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))
    total.update(counts)
    return total


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--decrypt", action="store_true", help="way back to plaintext")
    ap.add_argument("--verify", action="store_true", help="read back only")
    ap.add_argument("--batch", type=int, default=500)
    a = ap.parse_args(argv)
    if a.apply and a.verify:
        ap.error("--verify never writes")
    dc.validate()
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set.")
    conn = psycopg2.connect(url)
    print("encrypt_usage_data:", "VERIFY" if a.verify else "WRITE" if a.apply else "DRY RUN (nothing written)",
          "| active key:", dc.active_key_id() or "-")
    try:
        total = run(conn, apply=a.apply, decrypt=a.decrypt, verify=a.verify, batch=a.batch)
    finally:
        conn.close()
    print("total:", ", ".join(f"{k} {v}" for k, v in sorted(total.items())) or "nothing")
    if not a.apply and not a.verify:
        print("Run again with --apply to write (back up first).")


if __name__ == "__main__":
    main()
