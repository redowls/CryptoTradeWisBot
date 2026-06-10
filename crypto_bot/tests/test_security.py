"""Phase 15 (DB-free): Fernet encryption of secrets in config_store."""

import pytest
from cryptography.fernet import Fernet

from crypto_bot import config_store as cs


@pytest.fixture(autouse=True)
def _clear_cache():
    cs._fernet.cache_clear()
    yield
    cs._fernet.cache_clear()


def _set_key(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv(cs._MASTER_KEY_ENV, key)
    cs._fernet.cache_clear()
    return key


def test_encrypt_decrypt_roundtrip(monkeypatch):
    _set_key(monkeypatch)
    enc = cs._encrypt("super-secret-key", True)
    assert enc.startswith(cs._ENC_PREFIX)
    assert "super-secret-key" not in enc            # ciphertext, not plaintext
    assert cs._decrypt(enc, True) == "super-secret-key"


def test_non_secret_never_encrypted(monkeypatch):
    _set_key(monkeypatch)
    assert cs._encrypt("1H", False) == "1H"


def test_double_encrypt_is_idempotent(monkeypatch):
    _set_key(monkeypatch)
    enc = cs._encrypt("x", True)
    assert cs._encrypt(enc, True) == enc            # already-encrypted passes through


def test_is_encrypted():
    assert cs.is_encrypted("enc::abc") is True
    assert cs.is_encrypted("abc") is False
    assert cs.is_encrypted(None) is False


def test_plaintext_passthrough_on_decrypt(monkeypatch):
    _set_key(monkeypatch)
    assert cs._decrypt("legacy-plaintext", True) == "legacy-plaintext"


def test_no_master_key_stores_plaintext(monkeypatch):
    monkeypatch.delenv(cs._MASTER_KEY_ENV, raising=False)
    cs._fernet.cache_clear()
    assert cs.encryption_enabled() is False
    assert cs._encrypt("z", True) == "z"            # falls back to plaintext


def test_decrypt_without_key_raises(monkeypatch):
    _set_key(monkeypatch)
    enc = cs._encrypt("y", True)
    monkeypatch.delenv(cs._MASTER_KEY_ENV, raising=False)
    cs._fernet.cache_clear()
    with pytest.raises(cs.EncryptionError):
        cs._decrypt(enc, True)


def test_wrong_key_raises(monkeypatch):
    _set_key(monkeypatch)
    enc = cs._encrypt("y", True)
    monkeypatch.setenv(cs._MASTER_KEY_ENV, Fernet.generate_key().decode())  # different key
    cs._fernet.cache_clear()
    with pytest.raises(cs.EncryptionError):
        cs._decrypt(enc, True)


def test_generate_master_key_is_valid():
    key = cs.generate_master_key()
    Fernet(key.encode())  # must not raise
