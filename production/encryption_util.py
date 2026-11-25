import base64
import hashlib
import json
import logging
import os
from decimal import Decimal
from typing import Any, Optional
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Hash import SHA256
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from config import Config

logger = logging.getLogger(__name__)

# --- Configuration for AES-GCM ---
try:
    ENCRYPTION_KEY = Config.ENCRYPTION_KEY
    if not ENCRYPTION_KEY or len(ENCRYPTION_KEY) < 16:
        raise ValueError("ENCRYPTION_KEY is missing or too short in config. It must be at least 16 characters.")
except (ImportError, AttributeError, ValueError) as e:
    logger.critical(f"Failed to load a valid ENCRYPTION_KEY: {e}")
    # Exit or raise a critical exception if the key is not available or invalid
    raise SystemExit("Fatal: ENCRYPTION_KEY is not configured properly.") from e

# --- Key Rotation Support ---
# Support for multiple encryption keys with versioning for rotation
ENCRYPTION_KEY_VERSION = int(os.environ.get('ENCRYPTION_KEY_VERSION', '1'))
ENCRYPTION_KEY_OLD = os.environ.get('ENCRYPTION_KEY_OLD')  # Previous key for decryption during rotation

# --- Key Derivation using PBKDF2 ---
# Generate a unique salt from the key version to ensure different derived keys
SALT_BASE = b'phonos-encryption-v'
SALT = SALT_BASE + str(ENCRYPTION_KEY_VERSION).encode()
AES_KEY = hashlib.pbkdf2_hmac('sha256', ENCRYPTION_KEY.encode(), SALT, 100000, dklen=32)

# Derive old key if provided for rotation support
AES_KEY_OLD = None
if ENCRYPTION_KEY_OLD:
    SALT_OLD = SALT_BASE + str(ENCRYPTION_KEY_VERSION - 1).encode()
    AES_KEY_OLD = hashlib.pbkdf2_hmac('sha256', ENCRYPTION_KEY_OLD.encode(), SALT_OLD, 100000, dklen=32)

NONCE_SIZE = 12  # 96 bits is recommended for GCM
TAG_SIZE = 16
VERSION_SIZE = 1  # 1 byte for key version

# --- Encryption/Decryption Functions ---
def encrypt(plaintext_bytes: bytes, aad: Optional[bytes] = None) -> Optional[bytes]:
    """
    Encrypts plaintext bytes using AES-GCM with improved error handling and AAD support.

    Args:
        plaintext_bytes: The data to encrypt
        aad: Optional authenticated associated data (not encrypted but authenticated)

    Returns:
        Encrypted bytes with format: version(1) + nonce(12) + tag(16) + ciphertext
        None if encryption fails
    """
    try:
        if not isinstance(plaintext_bytes, bytes):
            logger.error("Input must be bytes for encryption")
            return None

        if len(plaintext_bytes) == 0:
            logger.warning("Encrypting empty data")

        # Generate random nonce (CRITICAL: must be unique for each encryption with same key)
        nonce = get_random_bytes(NONCE_SIZE)
        cipher = AES.new(AES_KEY, AES.MODE_GCM, nonce=nonce)

        # Add authenticated associated data if provided
        if aad:
            cipher.update(aad)

        # Encrypt and get authentication tag
        ciphertext, tag = cipher.encrypt_and_digest(plaintext_bytes)

        # Combine version + nonce + tag + ciphertext for key rotation support
        version_byte = bytes([ENCRYPTION_KEY_VERSION])
        encrypted_data = version_byte + nonce + tag + ciphertext

        logger.debug(f"Successfully encrypted {len(plaintext_bytes)} bytes to {len(encrypted_data)} bytes (version {ENCRYPTION_KEY_VERSION})")
        return encrypted_data

    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return None


def decrypt(encrypted_bytes: bytes, aad: Optional[bytes] = None) -> Optional[bytes]:
    """
    Decrypts a byte string using AES-GCM with key rotation support.

    Args:
        encrypted_bytes: The encrypted data with format: version + nonce + tag + ciphertext
        aad: Optional authenticated associated data (must match encryption AAD)

    Returns:
        Decrypted bytes if successful, None otherwise
    """
    try:
        if not isinstance(encrypted_bytes, bytes):
            logger.error("Input must be bytes for decryption")
            return None

        # Check minimum length (version + nonce + tag)
        min_length = VERSION_SIZE + NONCE_SIZE + TAG_SIZE
        if len(encrypted_bytes) < min_length:
            # Try legacy format without version byte for backward compatibility
            return _decrypt_legacy(encrypted_bytes, aad)

        # Extract version
        version = encrypted_bytes[0]
        offset = VERSION_SIZE

        # Select appropriate key based on version
        if version == ENCRYPTION_KEY_VERSION:
            key = AES_KEY
        elif AES_KEY_OLD and version == (ENCRYPTION_KEY_VERSION - 1):
            key = AES_KEY_OLD
            logger.info(f"Using old encryption key for version {version}")
        else:
            logger.error(f"Unknown encryption key version: {version}")
            # Try legacy format as fallback
            return _decrypt_legacy(encrypted_bytes, aad)

        # Extract components
        nonce = encrypted_bytes[offset:offset + NONCE_SIZE]
        tag = encrypted_bytes[offset + NONCE_SIZE:offset + NONCE_SIZE + TAG_SIZE]
        ciphertext = encrypted_bytes[offset + NONCE_SIZE + TAG_SIZE:]

        # Create cipher with extracted nonce
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)

        # Add authenticated associated data if provided
        if aad:
            cipher.update(aad)

        # Decrypt and verify authentication
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)

        logger.debug(f"Successfully decrypted {len(encrypted_bytes)} bytes to {len(plaintext)} bytes (version {version})")
        return plaintext

    except ValueError as e:
        # AES-GCM verification failed - data corrupted or tampered with
        logger.error(f"Decryption failed - authentication error: {e}")
        return None
    except KeyError as e:
        # Missing key or cipher parameters
        logger.error(f"Decryption failed - key error: {e}")
        return None
    except Exception as e:
        logger.error(f"Decryption failed - unexpected error: {e}")
        return None


def _decrypt_legacy(encrypted_bytes: bytes, aad: Optional[bytes] = None) -> Optional[bytes]:
    """
    Decrypt data encrypted with legacy format (no version byte).
    For backward compatibility during key rotation.
    """
    try:
        min_length = NONCE_SIZE + TAG_SIZE
        if len(encrypted_bytes) < min_length:
            logger.error(f"Encrypted data too short: {len(encrypted_bytes)} bytes (minimum: {min_length})")
            return None

        # Extract components (legacy format)
        nonce = encrypted_bytes[:NONCE_SIZE]
        tag = encrypted_bytes[NONCE_SIZE:NONCE_SIZE + TAG_SIZE]
        ciphertext = encrypted_bytes[NONCE_SIZE + TAG_SIZE:]

        # Try current key first
        cipher = AES.new(AES_KEY, AES.MODE_GCM, nonce=nonce)
        if aad:
            cipher.update(aad)

        try:
            plaintext = cipher.decrypt_and_verify(ciphertext, tag)
            logger.debug("Successfully decrypted using legacy format with current key")
            return plaintext
        except ValueError:
            # Try old key if available
            if AES_KEY_OLD:
                cipher = AES.new(AES_KEY_OLD, AES.MODE_GCM, nonce=nonce)
                if aad:
                    cipher.update(aad)
                plaintext = cipher.decrypt_and_verify(ciphertext, tag)
                logger.info("Successfully decrypted using legacy format with old key")
                return plaintext
            raise

    except Exception as e:
        logger.error(f"Legacy decryption failed: {e}")
        return None


# --- Utility and Validation Functions ---
def validate_encryption_setup() -> bool:
    """Validate that encryption is properly configured."""
    try:
        # Test encryption/decryption
        test_data = b"test_encryption_setup"
        encrypted = encrypt(test_data)
        if encrypted is None:
            return False
        
        decrypted = decrypt(encrypted)
        if decrypted != test_data:
            return False
        
        logger.info("Encryption system validated successfully")
        return True
        
    except Exception as e:
        logger.error(f"Encryption validation failed: {e}")
        return False


def encrypt_string(text: str) -> Optional[bytes]:
    """
    Encrypt a string and return encrypted bytes.
    Returns encrypted bytes if successful, None otherwise.
    """
    try:
        if not isinstance(text, str):
            logger.error("Input must be a string")
            return None
        
        plaintext_bytes = text.encode('utf-8')
        return encrypt(plaintext_bytes)
        
    except UnicodeEncodeError as e:
        logger.error(f"String encoding failed: {e}")
        return None
    except Exception as e:
        logger.error(f"String encryption failed: {e}")
        return None


def decrypt_to_string(encrypted_bytes: bytes) -> Optional[str]:
    """
    Decrypt encrypted bytes and return as string.
    Returns decrypted string if successful, None otherwise.
    """
    try:
        if not isinstance(encrypted_bytes, bytes):
            logger.error("Input must be bytes")
            return None
        
        decrypted_bytes = decrypt(encrypted_bytes)
        if decrypted_bytes is None:
            return None
        
        return decrypted_bytes.decode('utf-8')
        
    except UnicodeDecodeError as e:
        logger.error(f"String decoding failed: {e}")
        return None
    except Exception as e:
        logger.error(f"String decryption failed: {e}")
        return None


def get_encryption_info() -> dict:
    """Get information about the encryption configuration."""
    return {
        'algorithm': 'AES-GCM',
        'key_size': len(AES_KEY) * 8,  # bits
        'nonce_size': NONCE_SIZE,
        'tag_size': TAG_SIZE,
        'key_derivation': 'SHA-256',
        'authenticated': True
    }


def test_encryption_performance(data_size: int = 1024) -> dict:
    """
    Test encryption/decryption performance.
    Returns performance metrics.
    """
    import time
    
    try:
        # Generate test data
        test_data = get_random_bytes(data_size)
        
        # Test encryption
        start_time = time.time()
        encrypted = encrypt(test_data)
        encrypt_time = time.time() - start_time
        
        if encrypted is None:
            return {'error': 'Encryption failed'}
        
        # Test decryption
        start_time = time.time()
        decrypted = decrypt(encrypted)
        decrypt_time = time.time() - start_time
        
        if decrypted != test_data:
            return {'error': 'Decryption validation failed'}
        
        return {
            'data_size_bytes': data_size,
            'encrypted_size_bytes': len(encrypted),
            'encryption_time_ms': encrypt_time * 1000,
            'decryption_time_ms': decrypt_time * 1000,
            'encryption_speed_mbps': (data_size / (1024 * 1024)) / encrypt_time if encrypt_time > 0 else float('inf'),
            'decryption_speed_mbps': (data_size / (1024 * 1024)) / decrypt_time if decrypt_time > 0 else float('inf'),
            'overhead_bytes': len(encrypted) - data_size,
            'overhead_percent': ((len(encrypted) - data_size) / data_size) * 100
        }
        
    except Exception as e:
        logger.error(f"Performance test failed: {e}")
        return {'error': str(e)}


# Validate encryption setup on module import
if not validate_encryption_setup():
    logger.critical("Encryption system validation failed! System may not work properly.")
else:
    logger.info(f"Encryption system ready: {get_encryption_info()}")


def encrypt_data(data: bytes) -> Optional[bytes]:
    """Alias for encrypt() for backward compatibility."""
    return encrypt(data)

def decrypt_data(encrypted_data: bytes) -> Optional[bytes]:
    """Alias for decrypt() for backward compatibility."""
    return decrypt(encrypted_data)

# --- Asymmetric Key (RSA) Functions ---

def generate_key_pair() -> tuple[str, str]:
    """
    Generates a new RSA key pair and returns them in PEM format.
    :return: A tuple containing the private key and public key as strings.
    """
    key = RSA.generate(2048)
    private_key = key.export_key().decode('utf-8')
    public_key = key.publickey().export_key().decode('utf-8')
    return private_key, public_key


def derive_public_key(private_key_pem: str) -> Optional[str]:
    """Derive the public key PEM from a private key PEM string."""

    try:
        key = RSA.import_key(private_key_pem)
    except (ValueError, TypeError, IndexError) as exc:
        logger.error("Failed to parse private key for public key derivation: %s", exc)
        return None

    try:
        return key.publickey().export_key().decode("utf-8")
    except (ValueError, TypeError, IndexError) as exc:
        logger.error("Failed to derive public key from private key: %s", exc)
        return None

def sign_message(private_key_pem: str, message: bytes) -> Optional[str]:
    """
    Signs a message with a private key.
    :param private_key_pem: The private key in PEM format.
    :param message: The message to sign, as bytes.
    :return: The signature as a base64-encoded string, or None on error.
    """
    try:
        key = RSA.import_key(private_key_pem)
        h = SHA256.new(message)
        signature = pkcs1_15.new(key).sign(h)
        return base64.b64encode(signature).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to sign message: {e}")
        return None

def canonical_dumps(data: Any) -> str:
    """Serialize ``data`` to a canonical JSON string suitable for signing."""

    def _default(obj: Any) -> Any:
        if isinstance(obj, bytes):
            return base64.b64encode(obj).decode("ascii")
        if isinstance(obj, set):
            return sorted(obj)
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_default,
    )


def verify_signature(public_key_pem: str, message: bytes, signature_b64: str) -> bool:
    """
    Verifies a signature with a public key.
    :param public_key_pem: The public key in PEM format.
    :param message: The original message that was signed, as bytes.
    :param signature_b64: The base64-encoded signature.
    :return: True if the signature is valid, False otherwise.
    """
    try:
        key = RSA.import_key(public_key_pem)
        h = SHA256.new(message)
        signature = base64.b64decode(signature_b64)
        pkcs1_15.new(key).verify(h, signature)
        return True
    except (ValueError, TypeError, Exception):
        return False


__all__ = [
    "AES_KEY",
    "canonical_dumps",
    "decrypt",
    "decrypt_data",
    "decrypt_to_string",
    "encrypt",
    "encrypt_data",
    "encrypt_string",
    "generate_key_pair",
    "derive_public_key",
    "get_encryption_info",
    "sign_message",
    "test_encryption_performance",
    "validate_encryption_setup",
    "verify_signature",
]
