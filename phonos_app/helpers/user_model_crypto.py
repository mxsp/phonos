"""
User Model Bridge Cryptography Utilities

Provides encryption/decryption for worker communications using model API keys.
Uses AES-256-GCM for authenticated encryption, similar to IoT device encryption.

Security Features:
- AES-256-GCM authenticated encryption
- PBKDF2-HMAC-SHA256 key derivation (100,000 iterations)
- HMAC-SHA256 request signing with timestamp validation
- Secure random API key generation
"""

import os
import hashlib
import hmac
import secrets
import base64
import json
import time
import logging
from typing import Tuple, Optional, Dict, Any, Union

logger = logging.getLogger(__name__)

# API Key prefixes for different key types
KEY_PREFIX_MODEL = "mk_"      # Model-specific keys
KEY_PREFIX_PROJECT = "pj_"    # Project-wide keys
KEY_PREFIX_WORKER = "wk_"     # External worker keys


def generate_model_api_key(key_type: str = 'model') -> str:
    """
    Generate a secure random API key for user models.

    Args:
        key_type: Type of key ('model', 'project', 'worker')

    Returns:
        API key in format: prefix_<32 random bytes as base64url>
    """
    prefixes = {
        'model': KEY_PREFIX_MODEL,
        'project': KEY_PREFIX_PROJECT,
        'worker': KEY_PREFIX_WORKER
    }
    prefix = prefixes.get(key_type, KEY_PREFIX_MODEL)

    random_bytes = secrets.token_bytes(32)
    key = base64.urlsafe_b64encode(random_bytes).decode('ascii').rstrip('=')
    return f"{prefix}{key}"


def hash_api_key(api_key: str) -> str:
    """
    Create a SHA-256 hash of the API key for secure storage/lookup.

    Args:
        api_key: The API key to hash

    Returns:
        Hexadecimal SHA-256 hash
    """
    return hashlib.sha256(api_key.encode('utf-8')).hexdigest()


def get_key_prefix(api_key: str, length: int = 8) -> str:
    """
    Get the prefix of an API key for display purposes.

    Args:
        api_key: The full API key
        length: Number of characters to include (default 8)

    Returns:
        Key prefix for display (e.g., "mk_abc123...")
    """
    return api_key[:length] + "..." if len(api_key) > length else api_key


def derive_encryption_key(api_key: str, salt: bytes = None) -> Tuple[bytes, bytes]:
    """
    Derive a 256-bit encryption key from the API key using PBKDF2.

    Args:
        api_key: The API key to derive from
        salt: Optional salt bytes (generated if not provided)

    Returns:
        Tuple of (32-byte key, 16-byte salt)
    """
    if salt is None:
        salt = secrets.token_bytes(16)

    key = hashlib.pbkdf2_hmac(
        'sha256',
        api_key.encode('utf-8'),
        salt,
        iterations=100000,  # OWASP recommendation
        dklen=32
    )
    return key, salt


def derive_deterministic_key(api_key: str) -> bytes:
    """
    Derive a deterministic encryption key from the API key.
    Uses the key's own hash as salt for repeatable derivation.

    This is useful when you need the same key to always derive
    the same encryption key (e.g., for stored encrypted data).

    Args:
        api_key: The API key

    Returns:
        32-byte deterministic encryption key
    """
    # Use first 16 bytes of SHA256(api_key) as salt
    salt = hashlib.sha256(api_key.encode('utf-8')).digest()[:16]
    key, _ = derive_encryption_key(api_key, salt)
    return key


def encrypt_data(data: Union[Dict[str, Any], str, bytes], api_key: str) -> str:
    """
    Encrypt data using AES-256-GCM with the API key.

    Packet format:
    <version:1><salt:16><nonce:12><tag:16><ciphertext>

    Args:
        data: Data to encrypt (dict, str, or bytes)
        api_key: The API key for encryption

    Returns:
        Base64-encoded encrypted payload
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        logger.warning("cryptography library not available, using fallback encryption")
        return _encrypt_simple(data, api_key)

    # Serialize data if needed
    if isinstance(data, dict):
        plaintext = json.dumps(data, separators=(',', ':')).encode('utf-8')
    elif isinstance(data, str):
        plaintext = data.encode('utf-8')
    else:
        plaintext = data

    # Derive key with random salt
    salt = secrets.token_bytes(16)
    key, _ = derive_encryption_key(api_key, salt)

    # Encrypt with AES-256-GCM
    nonce = secrets.token_bytes(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)  # Includes 16-byte auth tag

    # Pack: version(1) + salt(16) + nonce(12) + ciphertext(includes tag)
    version = b'\x01'
    packed = version + salt + nonce + ciphertext

    return base64.b64encode(packed).decode('ascii')


def decrypt_data(encrypted: str, api_key: str) -> Union[Dict[str, Any], bytes]:
    """
    Decrypt data using AES-256-GCM with the API key.

    Args:
        encrypted: Base64-encoded encrypted payload
        api_key: The API key for decryption

    Returns:
        Decrypted data (parsed as JSON if possible, otherwise bytes)

    Raises:
        ValueError: If decryption fails
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

        # Try to parse as JSON
        try:
            return json.loads(plaintext.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return plaintext

    except Exception as e:
        raise ValueError(f"Decryption failed: {str(e)}")


def _encrypt_simple(data: Union[Dict[str, Any], str, bytes], api_key: str) -> str:
    """
    Simple fallback encryption using XOR + HMAC (less secure but no dependencies).
    Version 0 format.
    """
    if isinstance(data, dict):
        plaintext = json.dumps(data, separators=(',', ':')).encode('utf-8')
    elif isinstance(data, str):
        plaintext = data.encode('utf-8')
    else:
        plaintext = data

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


def _decrypt_simple(encrypted: str, api_key: str) -> Union[Dict[str, Any], bytes]:
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
        raise ValueError("HMAC verification failed - data may be tampered")

    # Decrypt
    key_stream = (key_bytes * ((len(ciphertext) // 32) + 1))[:len(ciphertext)]
    plaintext = bytes(a ^ b for a, b in zip(ciphertext, key_stream))

    # Try to parse as JSON
    try:
        return json.loads(plaintext.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return plaintext


def sign_request(api_key: str, timestamp: int, payload: str) -> str:
    """
    Create HMAC-SHA256 signature for request authentication.

    Message format: <timestamp>.<payload>

    Args:
        api_key: The API key for signing
        timestamp: Unix timestamp (seconds)
        payload: The request payload as string

    Returns:
        Hexadecimal HMAC signature
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

    Args:
        api_key: The API key for verification
        timestamp: Unix timestamp from request
        payload: The request payload
        signature: The signature to verify
        max_age_seconds: Maximum age for valid requests (default 5 minutes)

    Returns:
        True if valid, False otherwise
    """
    # Check timestamp is within allowed window
    current_time = int(time.time())
    if abs(current_time - timestamp) > max_age_seconds:
        logger.warning(f"Request timestamp {timestamp} is outside allowed window")
        return False

    # Verify signature
    expected_signature = sign_request(api_key, timestamp, payload)
    return hmac.compare_digest(signature, expected_signature)


def encrypt_api_key_for_storage(api_key: str, master_key: Optional[str] = None) -> str:
    """
    Encrypt an API key for database storage.
    Uses a master key from environment or a derived key.

    Args:
        api_key: The API key to encrypt
        master_key: Optional master encryption key (uses env var if not provided)

    Returns:
        Base64-encoded encrypted API key
    """
    if master_key is None:
        master_key = os.environ.get('ENCRYPTION_KEY')
        if not master_key:
            raise EnvironmentError("ENCRYPTION_KEY environment variable is not set. Cannot encrypt API key.")

    return encrypt_data(api_key, master_key)


def decrypt_api_key_from_storage(encrypted_key: str, master_key: Optional[str] = None) -> Optional[str]:
    """
    Decrypt an API key from database storage.

    Args:
        encrypted_key: The encrypted API key
        master_key: Optional master encryption key (uses env var if not provided)

    Returns:
        The decrypted API key, or None if decryption fails
    """
    if master_key is None:
        master_key = os.environ.get('ENCRYPTION_KEY')
        if not master_key:
            logger.error("ENCRYPTION_KEY environment variable is not set. Cannot decrypt API key.")
            return None

    try:
        result = decrypt_data(encrypted_key, master_key)
        if isinstance(result, bytes):
            return result.decode('utf-8')
        return result
    except Exception as e:
        logger.error(f"Failed to decrypt API key: {e}")
        return None


class ModelMessage:
    """
    Helper class for building signed/encrypted messages for model communications.

    Similar to IoTMessage but tailored for user model bridge API.
    """

    def __init__(self, api_key: str, encrypt: bool = True):
        """
        Initialize message handler.

        Args:
            api_key: The API key for signing/encryption
            encrypt: Whether to encrypt payloads (default True)
        """
        self.api_key = api_key
        self.encrypt = encrypt

    def pack(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Pack data for transmission with signature and optional encryption.

        Args:
            data: The data to pack

        Returns:
            Packed message with timestamp, payload, and signature
        """
        timestamp = int(time.time())

        if self.encrypt:
            encrypted_data = encrypt_data(data, self.api_key)
            payload = {"encrypted": True, "data": encrypted_data}
        else:
            payload = data

        payload_str = json.dumps(payload, separators=(',', ':'))
        signature = sign_request(self.api_key, timestamp, payload_str)

        return {
            "timestamp": timestamp,
            "payload": payload,
            "signature": signature
        }

    def unpack(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Unpack and verify a received message.

        Args:
            message: The received message

        Returns:
            Unpacked and verified data

        Raises:
            ValueError: If verification fails
        """
        timestamp = message.get("timestamp")
        payload = message.get("payload")
        signature = message.get("signature")

        if not all([timestamp, payload, signature]):
            raise ValueError("Invalid message format - missing required fields")

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


def create_job_encryption_header(api_key: str) -> Dict[str, str]:
    """
    Create HTTP headers for encrypted job communication.

    Args:
        api_key: The API key

    Returns:
        Dict with required headers
    """
    timestamp = int(time.time())
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Timestamp": str(timestamp),
        "X-Encryption-Enabled": "true"
    }


def verify_job_encryption_headers(headers: Dict[str, str], api_key: str,
                                   body: str = "") -> bool:
    """
    Verify encryption headers on incoming request.

    Args:
        headers: Request headers
        api_key: The expected API key
        body: Request body for signature verification

    Returns:
        True if valid, False otherwise
    """
    auth_header = headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    provided_key = auth_header.replace("Bearer ", "").strip()
    if hash_api_key(provided_key) != hash_api_key(api_key):
        return False

    # Check timestamp if signature verification is enabled
    timestamp_str = headers.get("X-Timestamp")
    signature = headers.get("X-Signature")

    if timestamp_str and signature:
        try:
            timestamp = int(timestamp_str)
            return verify_request(api_key, timestamp, body, signature)
        except ValueError:
            return False

    return True


# Fernet-compatible encryption (for compatibility with existing code)
def get_fernet_key_from_api_key(api_key: str) -> bytes:
    """
    Derive a Fernet-compatible encryption key from an API key.

    This is for compatibility with existing Fernet-based encryption.

    Args:
        api_key: The API key

    Returns:
        32-byte key suitable for Fernet (base64url encoded)
    """
    key = derive_deterministic_key(api_key)
    return base64.urlsafe_b64encode(key)


def encrypt_with_fernet(data: bytes, api_key: str) -> Optional[bytes]:
    """
    Encrypt data using Fernet (AES-128-CBC with HMAC).

    Args:
        data: Data to encrypt
        api_key: API key for key derivation

    Returns:
        Encrypted bytes, or None if encryption fails
    """
    try:
        from cryptography.fernet import Fernet
        fernet_key = get_fernet_key_from_api_key(api_key)
        fernet = Fernet(fernet_key)
        return fernet.encrypt(data)
    except Exception as e:
        logger.error(f"Fernet encryption failed: {e}")
        return None


def decrypt_with_fernet(encrypted_data: bytes, api_key: str) -> Optional[bytes]:
    """
    Decrypt data using Fernet.

    Args:
        encrypted_data: Data to decrypt
        api_key: API key for key derivation

    Returns:
        Decrypted bytes, or None if decryption fails
    """
    try:
        from cryptography.fernet import Fernet
        fernet_key = get_fernet_key_from_api_key(api_key)
        fernet = Fernet(fernet_key)
        return fernet.decrypt(encrypted_data)
    except Exception as e:
        logger.error(f"Fernet decryption failed: {e}")
        return None
