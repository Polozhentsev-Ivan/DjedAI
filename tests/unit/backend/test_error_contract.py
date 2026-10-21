from fastapi import HTTPException
from fastapi.testclient import TestClient

from djedai.backend.app import create_app
from djedai.backend.config import Settings
from djedai.contracts.common import ErrorResponse


def test_http_and_validation_errors_share_the_wire_contract_without_inputs():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        schema = client.get("/api/openapi.json").json()
        validation = schema["paths"]["/api/auth/register"]["post"]["responses"]["422"]
        assert validation["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ErrorResponse"
        }
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert ErrorResponse.model_validate(response.json()).error.code == "not_found"
        response = client.post(
            "/api/auth/register",
            json={
                "username": "x",
                "password": "password-must-not-be-echoed",
            },
        )
        assert response.status_code == 422
        assert ErrorResponse.model_validate(response.json()).error.code == "invalid_request"
        assert "password-must-not-be-echoed" not in response.text


def test_server_errors_do_not_expose_exception_details():
    app = create_app(Settings(_env_file=None))

    @app.get("/internal-failure")
    def fail():
        raise RuntimeError("database-password-must-not-leak")

    @app.get("/unavailable")
    def unavailable():
        raise HTTPException(503, "provider-api-key-must-not-leak", headers={"Retry-After": "1"})

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/internal-failure")
        assert response.status_code == 500
        assert ErrorResponse.model_validate(response.json()).error.code == "internal_error"
        assert "database-password" not in response.text
        response = client.get("/unavailable")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert ErrorResponse.model_validate(response.json()).error.code == "unavailable"
        assert "provider-api-key" not in response.text
