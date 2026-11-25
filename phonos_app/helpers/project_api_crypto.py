"""
Dual-use crypto for the Project API.

The project API key is used for *two* purposes (hence "dual use"):

  1. Authentication  - we store only ``sha256(plaintext_key)`` in the DB and
     look the key up by that hash, exactly like the other API keys.
  2. Encryption      - we derive a per-key AES-256-GCM data key from the
     *plaintext* key via HKDF-SHA256 and a per-key random salt, and use it to
     encrypt the job inputs/params and results we persist for the project API.

Because the plaintext key is never stored (only its SHA-256 hash, which has
256 bits of entropy behind it and is not reversible), the derived data key
cannot be reconstructed from a database dump alone. An attacker who steals the
database sees only ciphertext for the project-API payload store; decryption
requires the caller to present the API key on each request.

The HKDF derivation uses a *different* salt/info than the auth hash, so the
stored auth hash leaks nothing usable about the data key.

Tokens produced here are self-describing ``pjenc:v1:<urlsafe-b64>`` strings so
they fit in TEXT columns and round-trip across PostgreSQL and SQLite.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from Crypto.Cipher import AES
from Crypto.Protocol.KDF import HKDF
from Crypto.Hash import SHA256
from Crypto.Random import get_random_bytes

logger = logging.getLogger(__name__)

# --- Constants -------------------------------------------------------------
TOKEN_PREFIX = "pjenc:v1:"
_HKDF_INFO = b"phonos-project-api-enc-v1"   # domain separation for the data key
_SALT_LEN = 16
_KEY_LEN = 32                                # AES-256
_NONCE_LEN = 12                             # 96-bit GCM nonce (recommended)
_TAG_LEN = 16
_VERIFIER_PLAINTEXT = b"phonos-project-api-verifier-v1"


# --- Key handling ----------------------------------------------------------
def auth_hash(plaintext_key: str) -> str:
    """Return the SHA-256 hex digest used to look the key up for auth."""
    return hashlib.sha256(plaintext_key.encode("utf-8")).hexdigest()


def new_salt() -> bytes:
    """Generate a fresh random salt for a new key."""
    return get_random_bytes(_SALT_LEN)


def salt_to_text(salt: bytes) -> str:
    """Encode a salt for storage in a TEXT column."""
    return base64.urlsafe_b64encode(salt).decode("ascii")


def salt_from_text(text: str) -> bytes:
    """Decode a salt previously stored via :func:`salt_to_text`."""
    return base64.urlsafe_b64decode(text.encode("ascii"))


def derive_data_key(plaintext_key: str, salt: bytes) -> bytes:
    """
    Derive the AES-256 data key from the plaintext API key and its salt.

    HKDF-SHA256 with a dedicated info string keeps this key independent from
    the stored authentication hash.
    """
    return HKDF(
        master=plaintext_key.encode("utf-8"),
        key_len=_KEY_LEN,
        salt=salt,
        hashmod=SHA256,
        num_keys=1,
        context=_HKDF_INFO,
    )


# --- Symmetric encryption --------------------------------------------------
def encrypt_with_key(data_key: bytes, plaintext: str) -> str:
    """Encrypt a string with the derived data key, returning a pjenc token."""
    if plaintext is None:
        return None
    nonce = get_random_bytes(_NONCE_LEN)
    cipher = AES.new(data_key, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    blob = nonce + tag + ciphertext
    return TOKEN_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_with_key(data_key: bytes, token: str) -> Optional[str]:
    """
    Decrypt a pjenc token back to a string.

    Returns ``None`` if the token is malformed or authentication fails (e.g.
    the wrong key was presented). Authentication failure is the property that
    makes the wrong key unable to read the data.
    """
    if token is None:
        return None
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        # Not one of our tokens - return unchanged (legacy passthrough).
        return token
    try:
        blob = base64.urlsafe_b64decode(token[len(TOKEN_PREFIX):].encode("ascii"))
        nonce = blob[:_NONCE_LEN]
        tag = blob[_NONCE_LEN:_NONCE_LEN + _TAG_LEN]
        ciphertext = blob[_NONCE_LEN + _TAG_LEN:]
        cipher = AES.new(data_key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except (ValueError, KeyError) as e:
        # ValueError = GCM tag mismatch (wrong key / tampered ciphertext).
        logger.warning("project_api_crypto: decrypt failed: %s", e)
        return None


def encrypt_json(data_key: bytes, obj: Any) -> str:
    """Encrypt a JSON-serialisable object."""
    return encrypt_with_key(data_key, json.dumps(obj, separators=(",", ":")))


def decrypt_json(data_key: bytes, token: str) -> Optional[Any]:
    """Decrypt a token produced by :func:`encrypt_json`."""
    plaintext = decrypt_with_key(data_key, token)
    if plaintext is None:
        return None
    try:
        return json.loads(plaintext)
    except (ValueError, TypeError):
        return None


def is_token(value) -> bool:
    """Return True if ``value`` looks like a pjenc token."""
    return isinstance(value, str) and value.startswith(TOKEN_PREFIX)


# --- Key verifier ----------------------------------------------------------
def make_verifier(data_key: bytes) -> str:
    """
    Produce a verifier token from the data key.

    Stored alongside the key, it lets us confirm at auth time that the data key
    we derived from the presented plaintext key matches the one used at
    creation - a cheap integrity check that also detects key/salt corruption.
    """
    return encrypt_with_key(data_key, _VERIFIER_PLAINTEXT.decode("ascii"))


def check_verifier(data_key: bytes, verifier_token: str) -> bool:
    """Return True if ``verifier_token`` decrypts correctly with ``data_key``."""
    if not verifier_token:
        return False
    decrypted = decrypt_with_key(data_key, verifier_token)
    if decrypted is None:
        return False
    return hmac.compare_digest(decrypted.encode("ascii"), _VERIFIER_PLAINTEXT)


# --- Self-test -------------------------------------------------------------
if __name__ == "__main__":
    key = "pj_" + base64.urlsafe_b64encode(get_random_bytes(24)).decode().rstrip("=")
    salt = new_salt()
    dk = derive_data_key(key, salt)

    # Round trip.
    tok = encrypt_with_key(dk, "MKFLILLFNILCLFPVLAADNHGVTETK")
    assert is_token(tok)
    assert decrypt_with_key(dk, tok) == "MKFLILLFNILCLFPVLAADNHGVTETK"

    # JSON round trip.
    jtok = encrypt_json(dk, {"steps": 20, "seed": 42})
    assert decrypt_json(dk, jtok) == {"steps": 20, "seed": 42}

    # Wrong key cannot decrypt.
    wrong = derive_data_key("pj_totally_different_key", salt)
    assert decrypt_with_key(wrong, tok) is None

    # Salt is part of the derivation: same key, different salt => different key.
    other_salt_key = derive_data_key(key, new_salt())
    assert decrypt_with_key(other_salt_key, tok) is None

    # Verifier.
    v = make_verifier(dk)
    assert check_verifier(dk, v)
    assert not check_verifier(wrong, v)

    # Salt text round trip.
    assert salt_from_text(salt_to_text(salt)) == salt

    print("project_api_crypto OK")
