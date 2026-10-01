"""Client-side encryption. Telegram cloud chats are not end-to-end encrypted, so
anything sensitive should be encrypted before it leaves the machine."""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .errors import IntegrityError

NONCE_SIZE = 12
SALT_SIZE = 16
_KEY_CHECK = b"tgfs-key-check-v1"


def new_salt() -> bytes:
    return os.urandom(SALT_SIZE)


def derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


class Cipher:
    """AES-256-GCM. Blob layout: nonce(12) || ciphertext || tag(16)."""

    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    @classmethod
    def from_passphrase(cls, passphrase: str, salt: bytes) -> Cipher:
        return cls(derive_key(passphrase, salt))

    def encrypt(self, data: bytes, aad: bytes = b"") -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        return nonce + self._aead.encrypt(nonce, data, aad)

    def decrypt(self, blob: bytes, aad: bytes = b"") -> bytes:
        try:
            return self._aead.decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], aad)
        except InvalidTag as e:
            raise IntegrityError("decryption failed: wrong passphrase or corrupted data") from e

    def key_check(self) -> bytes:
        return self.encrypt(_KEY_CHECK)

    def verify(self, check: bytes) -> bool:
        try:
            return self.decrypt(check) == _KEY_CHECK
        except IntegrityError:
            return False
