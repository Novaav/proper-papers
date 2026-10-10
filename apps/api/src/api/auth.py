"""Access-token verification for the API (RFC 9068 JWT profile, RS256 only).

The API trusts exactly one thing: an access token issued by the configured
issuer, for our audience, to one of our clients, signed with a key from the
issuer's JWKS. A verified token yields a VerifiedIdentity (issuer, subject);
nothing else in a request establishes identity.

Nothing here reads configuration or fetches keys at import or startup; keys are
fetched on first use and cached.
"""

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
from jwt import PyJWK, PyJWKClient

from api.models import ISSUER_MAX_LENGTH, SUBJECT_MAX_LENGTH

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
# RFC 9068 section 2.1; media types compare case-insensitively.
ACCESS_TOKEN_TYPES = {"at+jwt", "application/at+jwt"}
REQUIRED_CLAIMS = ["iss", "aud", "sub", "exp", "iat", "client_id"]
CLOCK_LEEWAY_SECONDS = 30
MAX_TOKEN_LENGTH = 8192
MAX_KID_LENGTH = 256

JWKS_FETCH_TIMEOUT_SECONDS = 2
JWKS_TTL_SECONDS = 600
JWKS_REFETCH_COOLDOWN_SECONDS = 60


class AuthConfigError(Exception):
    """The AUTH_* configuration is missing or invalid."""


class InvalidToken(Exception):
    """The token was rejected. `reason` is a fixed category, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuthSettings:
    issuer: str
    audience: str
    client_ids: frozenset[str]

    @property
    def jwks_url(self) -> str:
        # Auth0 publishes its keys here; the path is fixed, not taken from tokens.
        return f"{self.issuer.rstrip('/')}/.well-known/jwks.json"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> AuthSettings:
        issuer = env.get("AUTH_ISSUER", "").strip()
        audience = env.get("AUTH_AUDIENCE", "").strip()
        client_ids = frozenset(
            part.strip() for part in env.get("AUTH_ALLOWED_CLIENT_IDS", "").split(",")
        ) - {""}
        missing = [
            name
            for name, value in (
                ("AUTH_ISSUER", issuer),
                ("AUTH_AUDIENCE", audience),
                ("AUTH_ALLOWED_CLIENT_IDS", client_ids),
            )
            if not value
        ]
        if missing:
            raise AuthConfigError(f"missing: {', '.join(missing)}")
        parts = urlsplit(issuer)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment:
            raise AuthConfigError("AUTH_ISSUER must be an https URL without query or fragment")
        if len(issuer) > ISSUER_MAX_LENGTH:
            raise AuthConfigError(f"AUTH_ISSUER must be at most {ISSUER_MAX_LENGTH} characters")
        return cls(issuer=issuer, audience=audience, client_ids=client_ids)


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """Who the token says the caller is. Created only by TokenVerifier.verify."""

    issuer: str
    subject: str


JwksFetcher = Callable[[], Mapping[str, Any]]


def https_jwks_fetcher(url: str, timeout: float = JWKS_FETCH_TIMEOUT_SECONDS) -> JwksFetcher:
    """Fetch the JWKS document from `url`; caching is done by JwksCache."""
    client = PyJWKClient(url, cache_jwk_set=False, cache_keys=False, timeout=timeout)

    def fetch() -> Mapping[str, Any]:
        data: Mapping[str, Any] = client.fetch_data()
        return data

    return fetch


class JwksCache:
    """Signing keys by kid, refreshed every `ttl` seconds.

    An unknown kid triggers at most one refetch per `cooldown` seconds, so
    tokens with random kids cannot turn into a stream of requests to the
    issuer. If a refresh fails, the previous keys stay in use.
    """

    def __init__(
        self,
        fetch: JwksFetcher,
        *,
        ttl: float = JWKS_TTL_SECONDS,
        cooldown: float = JWKS_REFETCH_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None

    def get(self, kid: str) -> PyJWK | None:
        with self._lock:
            now = self._clock()
            stale = self._fetched_at is None or now - self._fetched_at >= self._ttl
            if (stale or kid not in self._keys) and self._may_attempt(now):
                self._refresh(now)
            return self._keys.get(kid)

    def _may_attempt(self, now: float) -> bool:
        return self._attempted_at is None or now - self._attempted_at >= self._cooldown

    def _refresh(self, now: float) -> None:
        self._attempted_at = now
        try:
            document = self._fetch()
            entries = document.get("keys") if isinstance(document, Mapping) else None
            if not isinstance(entries, list):
                raise ValueError("JWKS document has no key list")
        except Exception as error:  # network or document errors: keep previous keys
            logger.warning("JWKS refresh failed: %s", type(error).__name__)
            return
        # Each entry is parsed on its own, so one malformed key cannot block the others.
        keys: dict[str, PyJWK] = {}
        skipped = 0
        for entry in entries:
            if not _is_rs256_signing_key(entry):
                continue
            try:
                keys[entry["kid"]] = PyJWK(entry, algorithm=ALGORITHM)
            except Exception:
                skipped += 1
        if skipped:
            logger.warning("JWKS refresh skipped %d malformed signing key(s)", skipped)
        self._keys = keys
        self._fetched_at = now


def _is_rs256_signing_key(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("kty") == "RSA"
        and entry.get("use", "sig") == "sig"
        and entry.get("alg", ALGORITHM) == ALGORITHM
        and isinstance(entry.get("kid"), str)
        and 0 < len(entry["kid"]) <= MAX_KID_LENGTH
    )


class TokenVerifier:
    def __init__(self, settings: AuthSettings, keys: JwksCache) -> None:
        self._settings = settings
        self._keys = keys

    def verify(self, token: str) -> VerifiedIdentity:
        """Return the token's identity or raise InvalidToken. Never logs the token."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise InvalidToken("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise InvalidToken("malformed") from None
        # Header checks come first: they are free and must not trigger key fetches.
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("algorithm")
        typ = header.get("typ")
        if not isinstance(typ, str) or typ.lower() not in ACCESS_TOKEN_TYPES:
            raise InvalidToken("token_type")
        if "crit" in header:
            raise InvalidToken("critical_header")
        kid = header.get("kid")
        if not isinstance(kid, str) or not 0 < len(kid) <= MAX_KID_LENGTH:
            raise InvalidToken("key_id")

        key = self._keys.get(kid)
        if key is None:
            raise InvalidToken("unknown_key")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError:
            raise InvalidToken("expired") from None
        except jwt.ImmatureSignatureError:
            raise InvalidToken("not_yet_valid") from None
        except jwt.InvalidAudienceError:
            raise InvalidToken("audience") from None
        except jwt.InvalidIssuerError:
            raise InvalidToken("issuer") from None
        except jwt.MissingRequiredClaimError:
            raise InvalidToken("missing_claim") from None
        except jwt.InvalidSignatureError:
            raise InvalidToken("signature") from None
        except jwt.PyJWTError:
            raise InvalidToken("invalid") from None

        client_id = claims.get("client_id")
        if not isinstance(client_id, str) or client_id not in self._settings.client_ids:
            raise InvalidToken("client")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not 0 < len(subject) <= SUBJECT_MAX_LENGTH:
            raise InvalidToken("subject")
        return VerifiedIdentity(issuer=self._settings.issuer, subject=subject)
