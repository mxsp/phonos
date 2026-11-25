"""Field encryption for Phonos usage data: round trip, passthrough, wrong key, rotation, lookups, migration."""
import importlib.util
import os
import sqlite3
import sys
from unittest.mock import MagicMock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault('SECRET_KEY', 'unit-test-secret-key-not-used-anywhere-else')
os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')

from phonos_app.helpers import data_crypto as dc  # noqa: E402
from phonos_app.helpers import login_history as lh  # noqa: E402

K1 = "k1:" + "A" * 43
K2 = "k2:" + "B" * 43


@pytest.fixture
def keys(monkeypatch):
    def setter(raw):
        if raw:
            monkeypatch.setenv(dc.ENV_VAR, raw)
        else:
            monkeypatch.delenv(dc.ENV_VAR, raising=False)
    setter("")
    return setter


def test_no_key_changes_nothing(keys):
    assert not dc.enabled()
    assert dc.seal("a.b", "hello") == "hello"
    assert dc.blind_index("a.b", "x") is None


def test_round_trip_and_ciphertext_hides_content(keys):
    keys(K1)
    t = dc.seal("direct_messages.content", "Treffen um 10 Uhr")
    assert t.startswith("pd1:k1:") and "Treffen" not in t
    assert dc.unseal("direct_messages.content", t) == "Treffen um 10 Uhr"
    assert dc.seal("x.y", "same") != dc.seal("x.y", "same")
    assert dc.seal("x.y", "") == "" and dc.seal("x.y", None) is None


def test_legacy_plaintext_passes_through(keys):
    keys(K1)
    assert dc.unseal("a.b", "old text") == "old text"
    assert dc.unseal("a.b", "pd1:not a token") == "pd1:not a token"
    assert dc.unseal("a.b", None) is None


def test_value_is_bound_to_its_column(keys):
    keys(K1)
    t = dc.seal("login_history.ip_address", "203.0.113.5")
    with pytest.raises(dc.UnreadableField) as e:
        dc.unseal("login_history.user_agent", t)
    assert "203.0.113.5" not in str(e.value)


def test_wrong_key_raises_without_leaking(keys):
    keys(K1)
    t = dc.seal("a.b", "geheim")
    keys(K2)
    with pytest.raises(dc.UnreadableField) as e:
        dc.unseal("a.b", t)
    assert "geheim" not in str(e.value) and "k1" in str(e.value)
    assert dc.unseal_or_placeholder("a.b", t) == "[message unavailable]"
    keys("")
    with pytest.raises(dc.UnreadableField):
        dc.unseal("a.b", t)


def test_rotation(keys):
    keys(K1)
    old = dc.seal("a.b", "alt")
    keys(f"{K2},{K1}")
    assert dc.active_key_id() == "k2" and dc.unseal("a.b", old) == "alt"
    new = dc.seal("a.b", "neu")
    assert dc.key_id_of(new) == "k2" and dc.key_id_of(old) == "k1"
    keys(K2)
    assert dc.unseal("a.b", new) == "neu"
    with pytest.raises(dc.UnreadableField):
        dc.unseal("a.b", old)


@pytest.mark.parametrize("raw", ["nokid", "k1:short", f"{K1},k1:{'C' * 43}", "bad id:" + "A" * 43])
def test_bad_config_is_refused(keys, raw):
    keys(raw)
    with pytest.raises(dc.KeyConfigError):
        dc.validate()


def test_encryption_key_is_not_involved(keys, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", "x" * 32)
    keys(K1)
    t = dc.seal("a.b", "v")
    monkeypatch.setenv("ENCRYPTION_KEY", "y" * 32)       # rotating the other key changes nothing here
    assert dc.unseal("a.b", t) == "v"


def test_blind_index_lookup_survives_rotation(keys):
    keys(K1)
    i1 = dc.blind_index(lh.L_DEV, lh._device_text("1.2.3.4", "UA"))
    keys(f"{K2},{K1}")
    assert i1 in dc.blind_indexes(lh.L_DEV, lh._device_text("1.2.3.4", "UA"))
    assert dc.blind_index(lh.L_DEV, lh._device_text("1.2.3.4", "UA")) != i1


def test_login_history_without_key_runs_the_old_sql(keys):
    db = MagicMock()
    lh.record_login(db, 7, "1.2.3.4", "UA")
    sql, params = db.execute.call_args[0]
    assert "device_idx" not in sql and params == (7, "1.2.3.4", "UA")
    lh.is_known_device(db, 7, "1.2.3.4", "UA")
    sql, params = db.execute.call_args[0]
    assert sql == 'SELECT 1 FROM login_history WHERE user_id = %s AND ip_address = %s AND user_agent = %s'
    assert params == (7, "1.2.3.4", "UA")


def test_login_history_with_key_seals_and_looks_up_by_blind_index(keys):
    keys(K1)
    db = MagicMock()
    lh.record_login(db, 7, "1.2.3.4", "UA")
    sql, (uid, ip, ua, idx) = db.execute.call_args[0]
    assert "device_idx" in sql and ip.startswith("pd1:") and ua.startswith("pd1:") and "1.2.3.4" not in ip
    assert idx == dc.blind_index(lh.L_DEV, lh._device_text("1.2.3.4", "UA"))
    lh.is_known_device(db, 7, "1.2.3.4", "UA")
    sql, params = db.execute.call_args[0]
    assert "device_idx IN (%s)" in sql and params[:2] == (7, idx)
    assert params[-2:] == ("1.2.3.4", "UA")                    # legacy plaintext rows still match


def _load_script():
    spec = importlib.util.spec_from_file_location("encrypt_usage_data", os.path.join(ROOT, "scripts", "encrypt_usage_data.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE direct_messages (id INTEGER PRIMARY KEY, content TEXT)")
    c.execute("CREATE TABLE login_history (id INTEGER PRIMARY KEY, ip_address TEXT, user_agent TEXT, device_idx TEXT)")
    c.executemany("INSERT INTO direct_messages (content) VALUES (?)", [("hallo",), ("",), (None,), ("zweite",)])
    c.executemany("INSERT INTO login_history (ip_address, user_agent) VALUES (?, ?)", [("1.1.1.1", "A"), ("2.2.2.2", "B"), (None, None)])
    c.commit()
    return c


def _col(conn, sql):
    return [r[0] for r in conn.execute(sql)]


def test_migration_dry_run_apply_idempotent_rotate_back(keys, conn):
    m = _load_script()
    quiet = lambda *_: None
    keys(K1)
    dry = m.run(conn, ph="?", out=quiet)
    assert dry["encrypt"] == 4 and _col(conn, "SELECT content FROM direct_messages")[0] == "hallo"   # nothing written
    m.run(conn, apply=True, batch=2, ph="?", out=quiet)
    contents = _col(conn, "SELECT content FROM direct_messages ORDER BY id")
    assert dc.key_id_of(contents[0]) == "k1" and contents[1:3] == ["", None]
    ips = _col(conn, "SELECT ip_address FROM login_history ORDER BY id")
    assert dc.key_id_of(ips[0]) == "k1" and ips[2] is None
    assert _col(conn, "SELECT device_idx FROM login_history ORDER BY id")[0] == dc.blind_index(lh.L_DEV, lh._device_text("1.1.1.1", "A"))
    again = m.run(conn, apply=True, ph="?", out=quiet)
    assert again["encrypt"] == 0 and again["reseal"] == 0
    keys(f"{K2},{K1}")
    rot = m.run(conn, apply=True, ph="?", out=quiet)
    assert rot["reseal"] >= 4
    assert dc.key_id_of(_col(conn, "SELECT content FROM direct_messages ORDER BY id")[0]) == "k2"
    keys(K2)
    m.run(conn, verify=True, ph="?", out=quiet)
    m.run(conn, apply=True, decrypt=True, ph="?", out=quiet)
    assert _col(conn, "SELECT content FROM direct_messages ORDER BY id") == ["hallo", "", None, "zweite"]
    assert _col(conn, "SELECT ip_address FROM login_history ORDER BY id") == ["1.1.1.1", "2.2.2.2", None]
    assert set(_col(conn, "SELECT device_idx FROM login_history")) == {None}


def test_migration_refuses_without_key(keys, conn):
    with pytest.raises(SystemExit):
        _load_script().run(conn, apply=True, ph="?", out=lambda *_: None)


SPOOF = "pd1:k9:" + "A" * 60


def test_typed_text_shaped_like_a_token_cannot_block_a_read(keys):
    """A direct message is user input: without this, one message would break the inbox."""
    for typed in (SPOOF, "pd0:" + SPOOF):
        assert dc.unseal("direct_messages.content", dc.seal("direct_messages.content", typed)) == typed


def test_token_shaped_plaintext_survives_a_key_arriving_later(keys):
    stored = dc.seal("direct_messages.content", SPOOF)
    keys("k1:" + "A" * 43)
    assert dc.unseal("direct_messages.content", stored) == SPOOF
    assert dc.unseal("direct_messages.content", dc.seal("direct_messages.content", SPOOF)) == SPOOF
