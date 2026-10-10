"""An in-process token issuer for tests: RSA keys generated at test time, never stored."""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://issuer.test/"
AUDIENCE = "https://api.test"
CLIENT_ID = "web-client"

_OMIT = object()


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class LocalIssuer:
    """Holds signing keys by kid and publishes the public ones as a JWKS document."""

    keys: dict[str, rsa.RSAPrivateKey] = field(default_factory=lambda: {"key-1": _new_key()})
    published: set[str] = field(default_factory=lambda: {"key-1"})

    def add_key(self, kid: str, *, publish: bool = True) -> None:
        self.keys[kid] = _new_key()
        if publish:
            self.published.add(kid)

    def jwks(self) -> dict[str, Any]:
        entries = []
        for kid in sorted(self.published):
            entry = json.loads(RSAAlgorithm.to_jwk(self.keys[kid].public_key()))
            entry.update(kid=kid, use="sig", alg="RS256")
            entries.append(entry)
        return {"keys": entries}

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "auth0|user-1",
            "client_id": CLIENT_ID,
            "iat": now,
            "exp": now + 600,
            "scope": "openid",
            "jti": "jti-1",
        }
        for name, value in overrides.items():
            if value is _OMIT:
                claims.pop(name, None)
            else:
                claims[name] = value
        return claims

    def token(
        self,
        *,
        kid: str = "key-1",
        signing_key: rsa.RSAPrivateKey | None = None,
        algorithm: str = "RS256",
        headers: dict[str, Any] | None = None,
        **claim_overrides: Any,
    ) -> str:
        header = {"kid": kid, "typ": "at+jwt", **(headers or {})}
        header = {name: value for name, value in header.items() if value is not _OMIT}
        return jwt.encode(
            self.claims(**claim_overrides),
            signing_key or self.keys[kid],
            algorithm=algorithm,
            headers=header,
        )

    def public_pem(self, kid: str = "key-1") -> bytes:
        return (
            self.keys[kid]
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )


OMIT = _OMIT


def hs256_with_public_key(issuer: LocalIssuer, kid: str = "key-1") -> str:
    """Algorithm-confusion forgery: HS256 using the RSA public key as the HMAC secret.

    Built by hand because PyJWT refuses to create it.
    """
    header = _b64(json.dumps({"alg": "HS256", "typ": "at+jwt", "kid": kid}).encode())
    payload = _b64(json.dumps(issuer.claims()).encode())
    signing_input = f"{header}.{payload}".encode()
    signature = hmac.new(issuer.public_pem(kid), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def with_raw_header(token: str, header: dict[str, Any]) -> str:
    """Replace a token's header with one PyJWT would refuse to encode (signature kept)."""
    _header, payload, signature = token.split(".")
    return f"{_b64(json.dumps(header).encode())}.{payload}.{signature}"


def unsigned(issuer: LocalIssuer, kid: str = "key-1") -> str:
    header = _b64(json.dumps({"alg": "none", "typ": "at+jwt", "kid": kid}).encode())
    payload = _b64(json.dumps(issuer.claims()).encode())
    return f"{header}.{payload}."
