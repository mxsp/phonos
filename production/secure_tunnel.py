"""Shared helpers for the encrypted kicker tunnel."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any, Optional

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from config import Config

_TUNNEL_SALT = b"phonos-secure-tunnel"
_PBKDF2_ITERATIONS = 200_000
_AES_KEY_LENGTH = 16
_NONCE_LENGTH = 12
_TAG_LENGTH = 16


def derive_tunnel_key(secret: str | bytes | None = None) -> bytes:
    """Derive the symmetric key used for tunnel encryption."""

    if secret is None:
        secret = Config.ENCRYPTION_KEY
    if isinstance(secret, str):
        secret_bytes = secret.encode("utf-8")
    else:
        secret_bytes = bytes(secret)
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret_bytes,
        _TUNNEL_SALT,
        _PBKDF2_ITERATIONS,
        dklen=_AES_KEY_LENGTH,
    )


def encrypt_payload(
    data: dict[str, Any],
    *,
    key: bytes = None,
    logger: logging.Logger | None = None,
) -> str:
    """Encrypt a JSON-serialisable payload using AES-GCM."""

    try:
        serialized = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive guard
        if logger:
            logger.warning("Failed to serialize secure tunnel payload: %s", exc)
        raise

    key_bytes = key or derive_tunnel_key()
    nonce = get_random_bytes(_NONCE_LENGTH)
    cipher = AES.new(key_bytes, AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(serialized)
    token = nonce + tag + ciphertext
    return base64.b64encode(token).decode("ascii")


def decrypt_payload(
    token: str,
    *,
    key: bytes = None,
    logger: logging.Logger | None = None,
) -> Optional[dict[str, Any]]:
    """Decrypt an AES-GCM payload and return the parsed JSON body."""

    try:
        raw = base64.b64decode(token)
    except (ValueError, TypeError) as exc:
        if logger:
            logger.warning("Failed to base64 decode tunnel payload: %s", exc)
        return None

    if len(raw) < (_NONCE_LENGTH + _TAG_LENGTH):
        if logger:
            logger.warning("Secure tunnel payload too short: %d bytes", len(raw))
        return None

    nonce = raw[:_NONCE_LENGTH]
    tag = raw[_NONCE_LENGTH : _NONCE_LENGTH + _TAG_LENGTH]
    ciphertext = raw[_NONCE_LENGTH + _TAG_LENGTH :]

    key_bytes = key or derive_tunnel_key()
    cipher = AES.new(key_bytes, AES.MODE_GCM, nonce=nonce)
    try:
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as exc:
        if logger:
            logger.warning("Secure tunnel authentication failed: %s", exc)
        return None

    try:
        return json.loads(plaintext.decode("utf-8"))
    except json.JSONDecodeError as exc:
        if logger:
            logger.warning("Secure tunnel returned invalid JSON: %s", exc)
        return None
