import os
from cryptography.fernet import Fernet, InvalidToken


def get_cipher() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY", "").strip()

    if not key:
        raise ValueError("Missing ENCRYPTION_KEY environment variable")

    return Fernet(key.encode())


def encrypt_text(value: str) -> str:
    if value is None:
        return ""

    value = str(value).strip()
    if not value:
        return ""

    cipher = get_cipher()
    encrypted = cipher.encrypt(value.encode("utf-8"))
    return encrypted.decode("utf-8")


def decrypt_text(value: str) -> str:
    if value is None:
        return ""

    value = str(value).strip()
    if not value:
        return ""

    cipher = get_cipher()

    try:
        decrypted = cipher.decrypt(value.encode("utf-8"))
        return decrypted.decode("utf-8")
    except InvalidToken:
        raise ValueError("Invalid encrypted value or wrong ENCRYPTION_KEY")


def looks_encrypted(value: str) -> bool:
    if not value:
        return False
    return str(value).startswith("gAAAAA")
