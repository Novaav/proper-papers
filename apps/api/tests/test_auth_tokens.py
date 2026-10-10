"""Token verification: configuration, the RFC 9068 / RS256 contract, and the JWKS cache.

No database and no real identity provider: keys come from tests.token_issuer.
"""

import ast
import json
import socket
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api.auth import (
    AuthConfigError,
    AuthSettings,
    InvalidToken,
    JwksCache,
    TokenVerifier,
    VerifiedIdentity,
    https_jwks_fetcher,
)
from tests.token_issuer import (
    AUDIENCE,
    CLIENT_ID,
    ISSUER,
    OMIT,
    LocalIssuer,
    hs256_with_public_key,
    unsigned,
    with_raw_header,
)

SETTINGS = AuthSettings(issuer=ISSUER, audience=AUDIENCE, client_ids=frozenset({CLIENT_ID}))


class CountingFetcher:
    def __init__(self, issuer: LocalIssuer) -> None:
        self.issuer = issuer
        self.calls = 0
        self.fail = False

    def __call__(self) -> Mapping[str, Any]:
        self.calls += 1
        if self.fail:
            raise OSError("JWKS endpoint unreachable")
        return self.issuer.jwks()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def issuer() -> LocalIssuer:
    return LocalIssuer()


@pytest.fixture
def fetcher(issuer: LocalIssuer) -> CountingFetcher:
    return CountingFetcher(issuer)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(fetcher: CountingFetcher, clock: Clock) -> TokenVerifier:
    return TokenVerifier(SETTINGS, JwksCache(fetcher, clock=clock))


def _rejects(verifier: TokenVerifier, token: str) -> str:
    with pytest.raises(InvalidToken) as rejection:
        verifier.verify(token)
    return rejection.value.reason


# --- Configuration ---------------------------------------------------------------------


def _env(**overrides: str) -> dict[str, str]:
    env = {"AUTH_ISSUER": ISSUER, "AUTH_AUDIENCE": AUDIENCE, "AUTH_ALLOWED_CLIENT_IDS": CLIENT_ID}
    env.update(overrides)
    return env


def test_settings_from_env() -> None:
    settings = AuthSettings.from_env(_env(AUTH_ALLOWED_CLIENT_IDS=" web-client , other ,"))

    assert settings == AuthSettings(ISSUER, AUDIENCE, frozenset({"web-client", "other"}))
    assert settings.jwks_url == "https://issuer.test/.well-known/jwks.json"


@pytest.mark.parametrize("missing", ["AUTH_ISSUER", "AUTH_AUDIENCE", "AUTH_ALLOWED_CLIENT_IDS"])
def test_settings_require_every_value(missing: str) -> None:
    with pytest.raises(AuthConfigError, match=missing):
        AuthSettings.from_env(_env(**{missing: " "}))
    with pytest.raises(AuthConfigError, match="AUTH_ALLOWED_CLIENT_IDS"):
        AuthSettings.from_env(_env(AUTH_ALLOWED_CLIENT_IDS=" , "))


@pytest.mark.parametrize(
    "issuer",
    [
        "http://issuer.test/",
        "issuer.test",
        "https:///path",
        "https://issuer.test/?x=1",
        "https://issuer.test/#f",
        "https://" + "i" * 520 + "/",
    ],
)
def test_settings_require_a_plain_https_issuer(issuer: str) -> None:
    with pytest.raises(AuthConfigError):
        AuthSettings.from_env(_env(AUTH_ISSUER=issuer))


# --- Accepted tokens ------------------------------------------------------------------


def test_valid_token_yields_the_identity(verifier: TokenVerifier, issuer: LocalIssuer) -> None:
    assert verifier.verify(issuer.token()) == VerifiedIdentity(ISSUER, "auth0|user-1")


@pytest.mark.parametrize("typ", ["at+jwt", "AT+JWT", "application/at+jwt"])
def test_rfc9068_type_variants_are_accepted(
    verifier: TokenVerifier, issuer: LocalIssuer, typ: str
) -> None:
    assert verifier.verify(issuer.token(headers={"typ": typ})).subject == "auth0|user-1"


def test_audience_array_containing_ours_is_accepted(
    verifier: TokenVerifier, issuer: LocalIssuer
) -> None:
    token = issuer.token(aud=[AUDIENCE, "https://issuer.test/userinfo"])

    assert verifier.verify(token).subject == "auth0|user-1"


def test_clock_skew_within_leeway_is_accepted(verifier: TokenVerifier, issuer: LocalIssuer) -> None:
    now = int(time.time())

    verifier.verify(issuer.token(exp=now - 20))
    verifier.verify(issuer.token(iat=now + 20, nbf=now + 20))


# --- Rejected tokens: header ------------------------------------------------------------


def test_unsigned_token_is_rejected(verifier: TokenVerifier, issuer: LocalIssuer) -> None:
    assert _rejects(verifier, unsigned(issuer)) == "algorithm"


def test_hs256_signed_with_the_public_key_is_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer
) -> None:
    assert _rejects(verifier, hs256_with_public_key(issuer)) == "algorithm"


@pytest.mark.parametrize("algorithm", ["RS384", "RS512", "PS256"])
def test_other_rsa_algorithms_are_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher, algorithm: str
) -> None:
    assert _rejects(verifier, issuer.token(algorithm=algorithm)) == "algorithm"
    assert fetcher.calls == 0  # rejected before any key lookup


@pytest.mark.parametrize("typ", [OMIT, "JWT", "jwt", "id_token", "at+jwt; x", 1])
def test_non_access_token_types_are_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer, typ: object
) -> None:
    # An ID token from the same issuer has typ JWT: it must never pass as an access token.
    assert _rejects(verifier, issuer.token(headers={"typ": typ})) == "token_type"


@pytest.mark.parametrize("kid", [OMIT, "", "k" * 257])
def test_missing_or_invalid_key_ids_are_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher, kid: object
) -> None:
    token = issuer.token(headers={"kid": kid})

    assert _rejects(verifier, token) == "key_id"
    assert fetcher.calls == 0


def test_non_string_key_id_is_rejected_without_a_key_lookup(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher
) -> None:
    # PyJWT refuses to build this, so the header is hand-made; PyJWT's parser
    # rejects it before our own kid check (which remains as defence in depth).
    token = with_raw_header(issuer.token(), {"alg": "RS256", "typ": "at+jwt", "kid": 7})

    assert _rejects(verifier, token) == "malformed"
    assert fetcher.calls == 0


def test_critical_header_extensions_are_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher
) -> None:
    # PyJWT's parser already rejects unsupported critical extensions ("malformed");
    # our explicit check ("critical_header") covers any future parser change.
    token = issuer.token(headers={"crit": ["exp"]})

    assert _rejects(verifier, token) in {"malformed", "critical_header"}
    assert fetcher.calls == 0


def test_embedded_or_remote_keys_in_the_header_are_ignored(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher
) -> None:
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attacker_issuer = LocalIssuer(keys={"key-1": attacker}, published={"key-1"})
    headers = {
        "jwk": attacker_issuer.jwks()["keys"][0],
        "jku": "https://attacker.test/jwks.json",
        "x5u": "https://attacker.test/cert.pem",
    }

    # Signed by the attacker, claiming our kid and pointing at the attacker's key.
    assert _rejects(verifier, issuer.token(signing_key=attacker, headers=headers)) == "signature"
    # Unknown kid with a jku: only our configured JWKS is ever consulted, and the
    # refetch cooldown (started by the first fetch) prevents even that.
    unknown = issuer.token(kid="attacker-kid", signing_key=attacker, headers=headers)
    assert _rejects(verifier, unknown) == "unknown_key"
    assert fetcher.calls == 1


@pytest.mark.parametrize(
    "token",
    ["", "not-a-jwt", "a.b.c", "a.b", "....", "x" * 9000, "eyJhbGciOiJSUzI1NiJ9.e30"],
)
def test_malformed_tokens_are_rejected(verifier: TokenVerifier, token: str) -> None:
    assert _rejects(verifier, token) in {"malformed", "algorithm", "token_type", "key_id"}


# --- Rejected tokens: signature and claims -----------------------------------------------


def test_tampered_payload_is_rejected(verifier: TokenVerifier, issuer: LocalIssuer) -> None:
    header, _payload, signature = issuer.token().split(".")
    forged_payload = issuer.token(sub="auth0|admin").split(".")[1]

    assert _rejects(verifier, f"{header}.{forged_payload}.{signature}") == "signature"


def test_token_signed_by_an_unpublished_key_is_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer
) -> None:
    issuer.add_key("key-unpublished", publish=False)

    assert _rejects(verifier, issuer.token(kid="key-unpublished")) == "unknown_key"


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"iss": "https://issuer.test"}, "issuer"),  # missing trailing slash: exact match only
        ({"iss": "https://other.test/"}, "issuer"),
        ({"aud": "https://other.api"}, "audience"),
        ({"aud": ["https://other.api", "https://issuer.test/userinfo"]}, "audience"),
        ({"aud": CLIENT_ID}, "audience"),  # an ID token's audience is the client
        ({"client_id": "another-client"}, "client"),
        ({"client_id": OMIT, "azp": CLIENT_ID}, "missing_claim"),  # Auth0 profile, not RFC 9068
        ({"exp": int(time.time()) - 40}, "expired"),
        ({"iat": int(time.time()) + 3600}, "not_yet_valid"),
        ({"nbf": int(time.time()) + 3600}, "not_yet_valid"),
        ({"sub": ""}, "subject"),
        ({"sub": "s" * 256}, "subject"),
        ({"sub": 42}, "invalid"),
    ],
)
def test_claim_violations_are_rejected(
    verifier: TokenVerifier, issuer: LocalIssuer, claims: dict[str, Any], reason: str
) -> None:
    assert _rejects(verifier, issuer.token(**claims)) == reason


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp", "iat", "client_id"])
def test_every_required_claim_must_be_present(
    verifier: TokenVerifier, issuer: LocalIssuer, claim: str
) -> None:
    overrides: dict[str, Any] = {claim: OMIT}
    assert _rejects(verifier, issuer.token(**overrides)) in {"missing_claim", "subject"}


# --- JWKS cache ------------------------------------------------------------------------


def test_keys_are_fetched_lazily_and_cached(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher, clock: Clock
) -> None:
    assert fetcher.calls == 0
    for _ in range(5):
        verifier.verify(issuer.token())

    assert fetcher.calls == 1
    clock.now += 601  # past the TTL
    verifier.verify(issuer.token())
    assert fetcher.calls == 2


def test_unknown_kids_refetch_at_most_once_per_cooldown(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher, clock: Clock
) -> None:
    verifier.verify(issuer.token())
    assert fetcher.calls == 1
    # The first fetch starts the cooldown too: an unknown kid right away does not refetch.
    assert _rejects(verifier, issuer.token(kid="random-early", signing_key=issuer.keys["key-1"]))
    assert fetcher.calls == 1

    clock.now += 61
    for i in range(20):
        assert _rejects(verifier, issuer.token(kid=f"random-{i}", signing_key=issuer.keys["key-1"]))
    assert fetcher.calls == 2  # one refetch for the whole burst

    clock.now += 61
    _rejects(verifier, issuer.token(kid="random-after-cooldown", signing_key=issuer.keys["key-1"]))
    assert fetcher.calls == 3


def test_key_rotation_is_picked_up(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher, clock: Clock
) -> None:
    verifier.verify(issuer.token())
    issuer.add_key("key-2")
    clock.now += 61

    assert verifier.verify(issuer.token(kid="key-2")).subject == "auth0|user-1"
    assert fetcher.calls == 2


def test_unreachable_jwks_fails_closed_and_keeps_known_keys(
    verifier: TokenVerifier, issuer: LocalIssuer, fetcher: CountingFetcher, clock: Clock
) -> None:
    fetcher.fail = True
    assert _rejects(verifier, issuer.token()) == "unknown_key"  # nothing cached yet

    fetcher.fail = False
    clock.now += 61
    verifier.verify(issuer.token())
    fetcher.fail = True
    clock.now += 601  # TTL expired and refresh fails: previously fetched keys stay usable
    assert verifier.verify(issuer.token()).subject == "auth0|user-1"


def test_non_rs256_signing_keys_in_the_jwks_are_ignored(
    fetcher: CountingFetcher, issuer: LocalIssuer, clock: Clock
) -> None:
    issuer.add_key("key-enc")
    issuer.add_key("key-rs512")
    document = issuer.jwks()
    for entry in document["keys"]:
        if entry["kid"] == "key-enc":
            entry["use"] = "enc"
        if entry["kid"] == "key-rs512":
            entry["alg"] = "RS512"
    verifier = TokenVerifier(SETTINGS, JwksCache(lambda: document, clock=clock))

    assert verifier.verify(issuer.token()).subject == "auth0|user-1"
    assert _rejects(verifier, issuer.token(kid="key-enc")) == "unknown_key"
    assert _rejects(verifier, issuer.token(kid="key-rs512")) == "unknown_key"


# --- HTTPS fetch path (loopback servers stand in for the issuer) ------------------------


def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/.well-known/jwks.json"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def jwks_server(issuer: LocalIssuer) -> Iterator[str]:
    body = json.dumps(issuer.jwks()).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    yield from _serve(Handler)


def test_fetcher_reads_the_jwks_document(jwks_server: str, issuer: LocalIssuer) -> None:
    verifier = TokenVerifier(SETTINGS, JwksCache(https_jwks_fetcher(jwks_server)))

    assert verifier.verify(issuer.token()).subject == "auth0|user-1"


@pytest.fixture
def silent_port() -> Iterator[int]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield listener.getsockname()[1]


def test_unresponsive_jwks_endpoint_fails_closed_within_the_timeout(
    silent_port: int, issuer: LocalIssuer
) -> None:
    url = f"http://127.0.0.1:{silent_port}/.well-known/jwks.json"
    verifier = TokenVerifier(SETTINGS, JwksCache(https_jwks_fetcher(url, timeout=1)))

    started = time.monotonic()
    assert _rejects(verifier, issuer.token()) == "unknown_key"
    assert time.monotonic() - started < 3


# --- Structure ---------------------------------------------------------------------------


def _constructions(name: str) -> list[str]:
    source_root = Path(__file__).parents[1] / "src" / "api"
    return [
        f"{path.name}:{node.lineno}"
        for path in source_root.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_verified_identity_is_constructed_only_by_the_verifier() -> None:
    assert [site.split(":")[0] for site in _constructions("VerifiedIdentity")] == ["auth.py"]


# --- Regressions from the A2 pre-push review ------------------------------------------------


@pytest.mark.parametrize("client_id", [["web-client"], [], {"id": "web-client"}, 7, True])
def test_non_string_client_id_is_rejected_as_invalid(
    verifier: TokenVerifier, issuer: LocalIssuer, client_id: object
) -> None:
    # D1: a signed token with a non-string client_id must be an InvalidToken (401), never a crash.
    assert _rejects(verifier, issuer.token(client_id=client_id)) == "client"


def test_null_client_id_counts_as_missing(verifier: TokenVerifier, issuer: LocalIssuer) -> None:
    # PyJWT treats a null required claim as absent.
    assert _rejects(verifier, issuer.token(client_id=None)) == "missing_claim"


def _with_malformed_entries(document: Mapping[str, Any]) -> dict[str, Any]:
    malformed: list[Any] = [
        {
            "kty": "RSA",
            "kid": "broken-modulus",
            "use": "sig",
            "alg": "RS256",
            "n": "!!!",
            "e": "AQAB",
        },
        {"kty": "RSA", "kid": "missing-exponent", "use": "sig", "alg": "RS256", "n": "AQAB"},
        "not-an-object",
    ]
    return {"keys": [malformed[0], *document["keys"], *malformed[1:]]}


def test_malformed_jwks_entries_do_not_block_valid_keys(issuer: LocalIssuer, clock: Clock) -> None:
    # D2: entries are parsed one by one; bad ones are skipped, valid ones still work.
    issuer.add_key("key-2")
    document = _with_malformed_entries(issuer.jwks())
    verifier = TokenVerifier(SETTINGS, JwksCache(lambda: document, clock=clock))

    assert verifier.verify(issuer.token(kid="key-1")).subject == "auth0|user-1"
    assert verifier.verify(issuer.token(kid="key-2")).subject == "auth0|user-1"
    claims_broken_kid = issuer.token(kid="broken-modulus", signing_key=issuer.keys["key-1"])
    assert _rejects(verifier, claims_broken_kid) == "unknown_key"


def test_rotation_continues_when_the_jwks_contains_a_malformed_entry(
    issuer: LocalIssuer, clock: Clock
) -> None:
    documents = [issuer.jwks()]
    verifier = TokenVerifier(SETTINGS, JwksCache(lambda: documents[-1], clock=clock))
    verifier.verify(issuer.token(kid="key-1"))

    issuer.add_key("key-2")
    documents.append(_with_malformed_entries(issuer.jwks()))
    clock.now += 61

    assert verifier.verify(issuer.token(kid="key-2")).subject == "auth0|user-1"


def test_unusable_jwks_document_still_keeps_previous_keys(
    issuer: LocalIssuer, clock: Clock
) -> None:
    documents: list[Any] = [issuer.jwks()]
    verifier = TokenVerifier(SETTINGS, JwksCache(lambda: documents[-1], clock=clock))
    verifier.verify(issuer.token())

    documents.append({"keys": "not-a-list"})
    clock.now += 601
    assert verifier.verify(issuer.token()).subject == "auth0|user-1"
