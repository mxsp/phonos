"""
Text-safe field encryption for database columns.

Thin wrapper over production.encryption_util (AES-256-GCM) that encodes
ciphertext as a self-describing ``enc:v1:<urlsafe-base64>`` token so
encrypted values fit in TEXT columns, with transparent legacy-plaintext
passthrough on read (values without the prefix are returned unchanged).
"""

import base64
import logging

from production.encryption_util import encrypt_string, decrypt_to_string

logger = logging.getLogger(__name__)

# Token prefix identifying values encrypted by this module.
ENC_PREFIX = "enc:v1:"


def is_encrypted(value) -> bool:
    """Return True if value looks like an enc:v1: token produced here."""
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypt_field(plaintext):
    """
    Encrypt a string field for at-rest storage in a TEXT column.

    Returns None for None. Raises RuntimeError if encryption fails so we
    never silently store plaintext when encryption was intended.
    """
    if plaintext is None:
        return None

    encrypted = encrypt_string(plaintext)
    if encrypted is None:
        raise RuntimeError("field_crypto: encryption failed; refusing to store plaintext")

    return ENC_PREFIX + base64.urlsafe_b64encode(encrypted).decode("ascii")


def decrypt_field(value):
    """
    Decrypt an enc:v1: token back to plaintext.

    Returns None for None. Values without the enc:v1: prefix are returned
    unchanged (legacy plaintext passthrough). If decryption fails, the raw
    value is returned so reads never hard-fail.
    """
    if value is None:
        return None

    if not is_encrypted(value):
        # Legacy plaintext row - pass through unchanged.
        return value

    try:
        encrypted = base64.urlsafe_b64decode(value[len(ENC_PREFIX):].encode("ascii"))
        plaintext = decrypt_to_string(encrypted)
        if plaintext is None:
            logger.error("field_crypto: decryption returned None; returning raw value")
            return value
        return plaintext
    except Exception as e:
        logger.error(f"field_crypto: failed to decrypt field: {e}")
        return value


if __name__ == "__main__":
    # Self-test: round-trip, legacy passthrough, None handling.
    token = encrypt_field("s3cret")
    assert token is not None and token.startswith(ENC_PREFIX), token
    assert is_encrypted(token)
    assert decrypt_field(token) == "s3cret"
    assert decrypt_field("legacyplain") == "legacyplain"
    assert not is_encrypted("legacyplain")
    assert encrypt_field(None) is None
    assert decrypt_field(None) is None
    empty_token = encrypt_field("")
    assert is_encrypted(empty_token) and decrypt_field(empty_token) == ""
    print("field_crypto OK")
