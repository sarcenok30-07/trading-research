"""
Encrypted key storage.

Instead of a plaintext key sitting in .env, the key is encrypted with
a passphrase-derived key (PBKDF2 + Fernet/AES) and stored in
wallet.enc. The passphrase is entered interactively at startup — it
is never written to disk or logged — and the decrypted key exists
only in memory for the process lifetime.

This does NOT protect against a compromised running process (malware
with access to process memory can still read it), and it doesn't
help if someone captures your passphrase via keylogger. What it
protects against is the much more common case: someone getting a
copy of your filesystem or a backup of .env without also having your
passphrase in their head.

Usage:
    python3 key_storage.py encrypt   # one-time setup: encrypts a key you paste in
    python3 key_storage.py decrypt   # sanity-check decryption works
"""
import sys
import base64
import getpass
from pathlib import Path

from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.fernet import Fernet, InvalidToken

KEY_FILE = Path("wallet.enc")
SALT_FILE = Path("wallet.salt")
PBKDF2_ITERATIONS = 480_000  # OWASP-recommended minimum as of recent guidance


def _derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_and_store(private_key_b58: str, passphrase: str):
    import os
    salt = os.urandom(16)
    fernet_key = _derive_fernet_key(passphrase, salt)
    token = Fernet(fernet_key).encrypt(private_key_b58.encode("utf-8"))

    SALT_FILE.write_bytes(salt)
    KEY_FILE.write_bytes(token)
    # Restrict permissions where the OS supports it
    try:
        KEY_FILE.chmod(0o600)
        SALT_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"[key_storage] Encrypted key written to {KEY_FILE}. "
          f"You can now remove WALLET_PRIVATE_KEY from .env.")


def load_decrypted_key(passphrase: str) -> str:
    if not KEY_FILE.exists() or not SALT_FILE.exists():
        raise FileNotFoundError(
            "wallet.enc / wallet.salt not found. Run "
            "`python3 key_storage.py encrypt` first."
        )
    salt = SALT_FILE.read_bytes()
    fernet_key = _derive_fernet_key(passphrase, salt)
    token = KEY_FILE.read_bytes()
    try:
        return Fernet(fernet_key).decrypt(token).decode("utf-8")
    except InvalidToken:
        raise ValueError("Wrong passphrase or corrupted key file.")


def prompt_for_key(use_encrypted: bool, plaintext_fallback: str) -> str:
    """
    Called at bot startup. If encrypted storage is set up, prompts for
    a passphrase (input hidden via getpass) and decrypts in memory.
    Otherwise falls back to the plaintext WALLET_PRIVATE_KEY from .env
    (with a warning, since that's the weaker option).
    """
    if use_encrypted and KEY_FILE.exists():
        passphrase = getpass.getpass("Enter wallet passphrase: ")
        return load_decrypted_key(passphrase)

    if plaintext_fallback:
        print("[key_storage] WARNING: using plaintext WALLET_PRIVATE_KEY from "
              ".env. Run `python3 key_storage.py encrypt` to switch to "
              "encrypted storage.")
        return plaintext_fallback

    raise ValueError(
        "No key available: neither encrypted wallet.enc nor "
        "WALLET_PRIVATE_KEY in .env is set."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("encrypt", "decrypt"):
        print("Usage: python3 key_storage.py [encrypt|decrypt]")
        sys.exit(1)

    if sys.argv[1] == "encrypt":
        key = getpass.getpass("Paste your wallet private key (base58, input hidden): ").strip()
        if not key:
            print("No key entered, aborting.")
            sys.exit(1)
        p1 = getpass.getpass("Set a passphrase: ")
        p2 = getpass.getpass("Confirm passphrase: ")
        if p1 != p2:
            print("Passphrases did not match, aborting.")
            sys.exit(1)
        encrypt_and_store(key, p1)
    else:
        p = getpass.getpass("Enter passphrase: ")
        try:
            decrypted = load_decrypted_key(p)
            print(f"[key_storage] Decryption OK. Key starts with: {decrypted[:6]}...")
        except ValueError as e:
            print(f"[key_storage] {e}")
