"""
auth/credentials.py
====================
Fernet symmetric encryption for BMC passwords stored in PostgreSQL.

A valid Fernet key is exactly 44 URL-safe base64 characters (32 bytes
after decoding). The ENCRYPTION_KEY environment variable must contain
ONE such key — not multiple concatenated keys.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


class CredentialCipher:
    """Thin wrapper around Fernet so callers never handle raw key bytes."""

    def __init__(self, fernet: Fernet):
        self._fernet = fernet

    def encrypt(self, plaintext: str) -> str:
        """Return a Fernet token string."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a Fernet token. Raises ValueError (not InvalidToken) so
        callers get a clear message instead of a cryptography internals trace."""
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise ValueError(
                "Failed to decrypt BMC password. This happens when:\n"
                "  1. ENCRYPTION_KEY changed since the server was added to the database.\n"
                "  2. ENCRYPTION_KEY in .env is corrupted (multiple keys concatenated).\n"
                "  3. The database row was copied from a system with a different key.\n"
                "Fix: set a stable ENCRYPTION_KEY in .env and re-add the server via the UI."
            )


_cipher_instance: CredentialCipher | None = None


def get_cipher(app_config) -> CredentialCipher:
    """Return the process-wide CredentialCipher, initializing on first call.

    app_config may be a Flask app.config (dict-like) or an AppConfig object —
    both are supported.
    """
    global _cipher_instance
    if _cipher_instance is not None:
        return _cipher_instance

    # Support both dict-style and attribute-style config access
    if hasattr(app_config, "__getitem__"):
        key = app_config.get("ENCRYPTION_KEY", "")
    else:
        key = getattr(app_config, "ENCRYPTION_KEY", "")

    key = (key or "").strip()

    if not key:
        # Auto-generate a temporary key for development convenience.
        key = Fernet.generate_key().decode()
        logger.warning(
            "ENCRYPTION_KEY is not set — a temporary key was generated for this process. "
            "Stored passwords will be unreadable after restart. "
            "Set ENCRYPTION_KEY in backend/.env."
        )
    else:
        # Validate it is a proper single Fernet key before first use.
        _assert_valid_fernet_key(key)

    _cipher_instance = CredentialCipher(Fernet(key.encode() if isinstance(key, str) else key))
    return _cipher_instance


def _assert_valid_fernet_key(key: str):
    """Raise ValueError with an actionable message if the key is invalid."""
    import base64
    if len(key) != 44:
        raise ValueError(
            f"ENCRYPTION_KEY must be exactly 44 characters (a Fernet key), "
            f"but got {len(key)} characters. "
            f"This usually means setup.sh ran multiple times and concatenated "
            f"several keys. Edit backend/.env and keep only ONE 44-character key. "
            f"Generate a new one: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        decoded = base64.urlsafe_b64decode(key.encode())
    except Exception:
        raise ValueError(
            f"ENCRYPTION_KEY is not valid URL-safe base64. "
            f"Generate a valid key: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    if len(decoded) != 32:
        raise ValueError(
            f"ENCRYPTION_KEY decodes to {len(decoded)} bytes; Fernet requires 32 bytes."
        )


def reset_cipher():
    """Reset the singleton — used in tests only."""
    global _cipher_instance
    _cipher_instance = None
