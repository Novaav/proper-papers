"""The interactive documentation and the OpenAPI schema need no authentication, so they are
off unless API_DOCS=1 (the local make targets set it). No database is needed."""

import pytest
from fastapi.testclient import TestClient

from api.main import create_app

DOCUMENTATION = ("/docs", "/redoc", "/openapi.json")


@pytest.mark.parametrize("environ", [{}, {"API_DOCS": ""}, {"API_DOCS": "0"}, {"API_DOCS": "true"}])
def test_the_documentation_is_not_served_by_default(environ: dict[str, str]) -> None:
    app = create_app(environ)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert not paths & set(DOCUMENTATION)
    client = TestClient(app)
    for path in DOCUMENTATION:
        assert client.get(path).status_code == 404, path
    # Only the API itself is left: health and /v1.
    assert client.get("/health/live").status_code == 200


def test_api_docs_1_serves_the_documentation_for_local_development() -> None:
    client = TestClient(create_app({"API_DOCS": "1"}))
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/v1/me" in schema.json()["paths"]
    for path in ("/docs", "/redoc"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")


def test_the_flag_is_read_from_the_given_environment_not_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_DOCS", "1")
    assert TestClient(create_app({})).get("/openapi.json").status_code == 404
    assert TestClient(create_app()).get("/openapi.json").status_code == 200
