from __future__ import annotations

from typing import Dict

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_rsa_key_pair() -> Dict[str, str]:
    """Generate a per-user RSA key pair for Bybit self-generated API keys.

    The public key is pasted into Bybit's AI/OpenAPI screen. The private key
    remains encrypted in the user's workspace and is used only for signing.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return {"private_key": private_pem, "public_key": public_pem}


def public_key_from_private(private_pem: str) -> str:
    private_key = serialization.load_pem_private_key(private_pem.encode("utf-8"), password=None)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
