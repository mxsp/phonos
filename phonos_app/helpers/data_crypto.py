"""Field encryption for usage and personal data (AES-256-GCM, ring of versioned keys).

This is NOT ``field_crypto.py`` and does not use ``ENCRYPTION_KEY``: that key protects job data and
2FA secrets and is rotated as a separate job. These values use their own ring in ``PHONOS_DATA_KEYS``
(``kid:base64url(32 bytes)``, comma separated, the first entry encrypts, all entries decrypt), so
rotating either key never touches the other.

* No ``PHONOS_DATA_KEYS`` set: ``seal`` returns the text unchanged. Deploying this code changes nothing.
* Values without the ``pd1:`` prefix are legacy plaintext and are returned as they are.
* A sealed value that cannot be opened (key missing or wrong) raises ``UnreadableField``. It never
  returns blanks silently and never puts the value into the message.
* Token: ``pd1:<kid>:<base64url(nonce | ciphertext | tag)>``; the field label is authenticated
  (AAD), so a value cannot be moved to another column.
* ``blind_indexes`` gives keyed HMACs for equality lookups (one per key in the ring).

Crypto comes from ``cryptography`` (AESGCM, HKDF) and ``hmac``; nothing is implemented here.
Generate a key:  python -c "from phonos_app.helpers.data_crypto import generate_key; print(generate_key('k1'))"
"""
import base64
import hashlib
import hmac
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENV_VAR = "PHONOS_DATA_KEYS"
PREFIX = "pd1:"
ESCAPE = "pd0:"  # plaintext that merely looks like a token, so a typed message cannot block a read
_KID = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_NONCE, _TAG = 12, 16
_cache = {}


class KeyConfigError(Exception):
    pass


class UnreadableField(Exception):
    pass


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b):
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _sub(master, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(master)


def _ring():
    """(active_kid | None, {kid: enc_key}, {kid: index_key}) from the environment, parsed once per value."""
    raw = (os.environ.get(ENV_VAR) or "").strip()
    if raw in _cache:
        return _cache[raw]
    active, enc, idx = None, {}, {}
    for part in (p.strip() for p in raw.split(",") if p.strip()):
        kid, sep, key = part.partition(":")
        if not sep or not _KID.match(kid) or kid in enc:
            raise KeyConfigError(f"{ENV_VAR}: entry without a valid, unique id (expected id:key)")
        try:
            master = _b64d(key)
        except Exception as exc:
            raise KeyConfigError(f"{ENV_VAR}: key '{kid}' is not base64url") from exc
        if len(master) != 32:
            raise KeyConfigError(f"{ENV_VAR}: key '{kid}' must be 32 bytes")
        enc[kid] = _sub(master, b"phonos.datacrypt.enc.v1")
        idx[kid] = _sub(master, b"phonos.datacrypt.idx.v1")
        active = active or kid
    _cache[raw] = (active, enc, idx)
    return _cache[raw]


def validate():
    _ring()


def enabled():
    return _ring()[0] is not None


def active_key_id():
    return _ring()[0]


def generate_key(kid="k1"):
    if not _KID.match(kid):
        raise ValueError("id: 1-16 characters of A-Z a-z 0-9 _ -")
    return f"{kid}:{_b64e(os.urandom(32))}"


def _aad(label):
    return b"phonos.pd1|" + label.encode("utf-8")


def _split(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return None
    kid, sep, body = value[len(PREFIX):].partition(":")
    if not sep or not _KID.match(kid):
        return None
    try:
        blob = _b64d(body)
    except Exception:
        return None
    return (kid, blob) if len(blob) >= _NONCE + _TAG else None


def is_sealed(value):
    return _split(value) is not None


def key_id_of(value):
    p = _split(value)
    return p[0] if p else None


def as_plain(text):
    """Store ``text`` unencrypted, escaped if it could be mistaken for a sealed value."""
    if isinstance(text, str) and text.startswith((PREFIX, ESCAPE)):
        return ESCAPE + text
    return text


def seal(label, text):
    """Encrypt ``text`` for column ``label`` (e.g. 'direct_messages.content'); unchanged without a key."""
    active, enc, _ = _ring()
    if not text or not isinstance(text, str):
        return text
    if active is None:
        return as_plain(text)
    nonce = os.urandom(_NONCE)
    blob = nonce + AESGCM(enc[active]).encrypt(nonce, text.encode("utf-8"), _aad(label))
    return f"{PREFIX}{active}:{_b64e(blob)}"


def unseal(label, value):
    if isinstance(value, str) and value.startswith(ESCAPE):
        return value[len(ESCAPE):]
    p = _split(value)
    if p is None:
        return value
    kid, blob = p
    key = _ring()[1].get(kid)
    if key is None:
        raise UnreadableField(f"{label}: data key '{kid}' is not in {ENV_VAR}")
    try:
        return AESGCM(key).decrypt(blob[:_NONCE], blob[_NONCE:], _aad(label)).decode("utf-8")
    except InvalidTag as exc:
        raise UnreadableField(f"{label}: value (key '{kid}') is damaged or belongs to another column") from exc


def blind_indexes(label, text):
    """Lookup values for ``text`` under every key of the ring (equality search; survives rotation)."""
    norm = (text or "").encode("utf-8")
    return [hmac.new(k, label.encode() + b"\0" + norm, hashlib.sha256).hexdigest() for k in _ring()[2].values()]


def blind_index(label, text):
    """The value to store: under the active key. ``None`` when no key is configured."""
    active, _, idx = _ring()
    if active is None:
        return None
    return hmac.new(idx[active], label.encode() + b"\0" + (text or "").encode("utf-8"), hashlib.sha256).hexdigest()


def unseal_or_placeholder(label, value, placeholder="[message unavailable]"):
    """For screens: an unreadable value must not take the whole page down. Logs label and key id only."""
    try:
        return unseal(label, value)
    except UnreadableField as exc:
        import logging
        logging.getLogger(__name__).error("data_crypto: %s", exc)
        return placeholder
