"""
Fernet-based encryption for storing API keys in the database.

SECRET_KEY must be a URL-safe base64-encoded 32-byte key.
Generate once: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Store in .env as ENCRYPTION_KEY=<value>

If ENCRYPTION_KEY is not set, crypto operations raise RuntimeError — this is intentional.
Do NOT silently fall back to plaintext for key storage.
"""
import os

try:
    from cryptography.fernet import Fernet, InvalidToken
    _FERNET_AVAILABLE = True
except ImportError:
    _FERNET_AVAILABLE = False


def _get_fernet() -> "Fernet":
    if not _FERNET_AVAILABLE:
        raise RuntimeError(
            "cryptography package is not installed. "
            "Run: pip install cryptography"
        )
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        # .env загружается в settings, но не экспортируется в os.environ
        from app.core.config import settings
        key = settings.ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set in environment. "
            "Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def encrypt_key(plaintext: str) -> str:
    """Encrypt an API key string, return base64 ciphertext"""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_key(ciphertext: str) -> str:
    """Decrypt a previously encrypted API key"""
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError(f"Failed to decrypt key — wrong ENCRYPTION_KEY or corrupted data: {e}")


def mask_key(key: str) -> str:
    """Return masked key for display: show only last 4 chars"""
    if len(key) <= 4:
        return "****"
    return f"{'*' * (len(key) - 4)}{key[-4:]}"
