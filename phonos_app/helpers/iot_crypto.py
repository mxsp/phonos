"""
IoT Bridge Cryptography Utilities

Provides encryption/decryption for device communications using device API keys.
Uses AES-256-GCM for authenticated encryption.
"""

import os
import hashlib
import hmac
import secrets
import base64
import json
import time
from typing import Tuple, Optional, Dict, Any


def generate_device_api_key() -> str:
    """Generate a secure random API key for a device."""
    # Format: dk_<32 random bytes as base64url>
    random_bytes = secrets.token_bytes(32)
    key = base64.urlsafe_b64encode(random_bytes).decode('ascii').rstrip('=')
    return f"dk_{key}"


def hash_api_key(api_key: str) -> str:
    """Create a SHA-256 hash of the API key for secure storage/lookup."""
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()


def derive_encryption_key(api_key: str, salt: bytes = None) -> Tuple[bytes, bytes]:
    """
    Derive a 256-bit encryption key from the API key using PBKDF2.

    Returns: (key, salt)
    """
    if salt is None:
        salt = secrets.token_bytes(16)

    key = hashlib.pbkdf2_hmac(
        'sha256',
        api_key.encode('utf-8'),
        salt,
        iterations=100000,
        dklen=32
    )
    return key, salt


def encrypt_data(data: Dict[str, Any], api_key: str) -> str:
    """
    Encrypt data using AES-256-GCM with the device API key.

    Returns base64-encoded encrypted payload with format:
    <version:1><salt:16><nonce:12><tag:16><ciphertext>
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        # Fallback if cryptography not installed - use simple XOR + HMAC
        return _encrypt_simple(data, api_key)

    # Serialize data
    plaintext = json.dumps(data, separators=(',', ':')).encode('utf-8')

    # Derive key
    salt = secrets.token_bytes(16)
    key, _ = derive_encryption_key(api_key, salt)

    # Encrypt
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    # Pack: version(1) + salt(16) + nonce(12) + ciphertext(includes 16-byte tag)
    version = b'\x01'
    packed = version + salt + nonce + ciphertext

    return base64.b64encode(packed).decode('ascii')


def decrypt_data(encrypted: str, api_key: str) -> Dict[str, Any]:
    """
    Decrypt data using AES-256-GCM with the device API key.

    Raises ValueError if decryption fails.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return _decrypt_simple(encrypted, api_key)

    try:
        packed = base64.b64decode(encrypted)

        # Unpack
        version = packed[0]
        if version != 1:
            raise ValueError(f"Unsupported encryption version: {version}")

        salt = packed[1:17]
        nonce = packed[17:29]
        ciphertext = packed[29:]

        # Derive key
        key, _ = derive_encryption_key(api_key, salt)

        # Decrypt
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

        return json.loads(plaintext.decode('utf-8'))

    except Exception as e:
        raise ValueError(f"Decryption failed: {str(e)}")


def _encrypt_simple(data: Dict[str, Any], api_key: str) -> str:
    """Simple fallback encryption using XOR + HMAC (less secure but no dependencies)."""
    plaintext = json.dumps(data, separators=(',', ':')).encode('utf-8')

    # Create key stream from API key
    key_bytes = hashlib.sha256(api_key.encode('utf-8')).digest()

    # XOR encryption (extend key stream as needed)
    key_stream = (key_bytes * ((len(plaintext) // 32) + 1))[:len(plaintext)]
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, key_stream))

    # Add HMAC for integrity
    mac = hmac.new(key_bytes, ciphertext, hashlib.sha256).digest()

    # Pack: version(1) + mac(32) + ciphertext
    packed = b'\x00' + mac + ciphertext
    return base64.b64encode(packed).decode('ascii')


def _decrypt_simple(encrypted: str, api_key: str) -> Dict[str, Any]:
    """Simple fallback decryption."""
    packed = base64.b64decode(encrypted)

    version = packed[0]
    if version != 0:
        raise ValueError(f"Unsupported encryption version: {version}")

    mac = packed[1:33]
    ciphertext = packed[33:]

    # Verify HMAC
    key_bytes = hashlib.sha256(api_key.encode('utf-8')).digest()
    expected_mac = hmac.new(key_bytes, ciphertext, hashlib.sha256).digest()

    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("HMAC verification failed")

    # Decrypt
    key_stream = (key_bytes * ((len(ciphertext) // 32) + 1))[:len(ciphertext)]
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, key_stream))

    return json.loads(plaintext.decode('utf-8'))


def sign_request(api_key: str, timestamp: int, payload: str) -> str:
    """
    Create HMAC-SHA256 signature for request authentication.

    Message format: <timestamp>.<payload>
    """
    message = f"{timestamp}.{payload}"
    signature = hmac.new(
        api_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return signature


def verify_request(api_key: str, timestamp: int, payload: str, signature: str,
                   max_age_seconds: int = 300) -> bool:
    """
    Verify request signature and timestamp.

    Returns True if valid, False otherwise.
    """
    # Check timestamp is within allowed window
    current_time = int(time.time())
    if abs(current_time - timestamp) > max_age_seconds:
        return False

    # Verify signature
    expected_signature = sign_request(api_key, timestamp, payload)
    return hmac.compare_digest(signature, expected_signature)


def generate_access_token() -> str:
    """Generate a secure access token for web client access."""
    # Format: at_<32 random bytes as base64url>
    random_bytes = secrets.token_bytes(32)
    token = base64.urlsafe_b64encode(random_bytes).decode('ascii').rstrip('=')
    return f"at_{token}"


class IoTMessage:
    """Helper class for building signed/encrypted messages."""

    def __init__(self, api_key: str, encrypt: bool = True):
        self.api_key = api_key
        self.encrypt = encrypt

    def pack(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Pack data for transmission with signature and optional encryption."""
        timestamp = int(time.time())

        if self.encrypt:
            encrypted_data = encrypt_data(data, self.api_key)
            payload = json.dumps({"encrypted": True, "data": encrypted_data})
        else:
            payload = json.dumps(data, separators=(',', ':'))

        signature = sign_request(self.api_key, timestamp, payload)

        return {
            "timestamp": timestamp,
            "payload": payload if not self.encrypt else {"encrypted": True, "data": encrypted_data},
            "signature": signature
        }

    def unpack(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Unpack and verify a received message."""
        timestamp = message.get("timestamp")
        payload = message.get("payload")
        signature = message.get("signature")

        if not all([timestamp, payload, signature]):
            raise ValueError("Invalid message format")

        # Serialize payload for signature verification
        if isinstance(payload, dict):
            payload_str = json.dumps(payload, separators=(',', ':'))
        else:
            payload_str = payload

        if not verify_request(self.api_key, timestamp, payload_str, signature):
            raise ValueError("Invalid signature or expired message")

        # Decrypt if needed
        if isinstance(payload, dict) and payload.get("encrypted"):
            return decrypt_data(payload["data"], self.api_key)
        elif isinstance(payload, str):
            return json.loads(payload)
        else:
            return payload
