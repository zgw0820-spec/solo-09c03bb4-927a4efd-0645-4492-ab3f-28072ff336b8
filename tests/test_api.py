"""API-level tests using FastAPI's TestClient."""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _payload(chart: str) -> dict:
    return {
        "values_override_path": f"/workspace/overrides/{chart}.values.yaml",
        "release_name": "platform-rel",
        "namespace": "platform",
        "versions": [
            {"label": "production-v1", "chart_dir": f"/workspace/mounted-charts/{chart}/v1"},
            {"label": "staging-v2", "chart_dir": f"/workspace/mounted-charts/{chart}/v2"},
        ],
    }


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_contains_two_examples():
    schema = app.openapi()
    examples = schema["paths"]["/api/v1/solve"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert len(examples) == 2
    assert any("orders-api" in name for name in examples)
    assert any("payment-web" in name for name in examples)


def test_solve_orders_api_example():
    resp = client.post("/api/v1/solve", json=_payload("orders-api"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert len(body["removed_paths"]) == 8
    assert body["release_name"] == "platform-rel"
    assert body["namespace"] == "platform"


def test_solve_payment_web_example():
    resp = client.post("/api/v1/solve", json=_payload("payment-web"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["candidate_cap_applied"] is True


def test_solve_supports_yaml_accept():
    resp = client.post(
        "/api/v1/solve",
        json=_payload("orders-api"),
        headers={"Accept": "application/yaml"},
    )
    assert resp.status_code == 200
    assert "application/yaml" in resp.headers["content-type"]
    assert "cleaned_yaml:" in resp.text


def test_missing_values_file_is_400():
    payload = _payload("orders-api")
    payload["values_override_path"] = "/workspace/overrides/nope.yaml"
    resp = client.post("/api/v1/solve", json=payload)
    assert resp.status_code == 400


def test_baseline_failure_is_422(tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "Chart.yaml").write_text(
        "apiVersion: v2\nname: broken\nversion: 0.1.0\n", encoding="utf-8"
    )
    templates = broken / "templates"
    templates.mkdir()
    (templates / "broken.yaml").write_text(
        '{{- fail "boom" -}}\n', encoding="utf-8"
    )
    payload = _payload("orders-api")
    payload["versions"] = [{"label": "broken", "chart_dir": str(broken)}]
    resp = client.post("/api/v1/solve", json=payload)
    assert resp.status_code == 422
    assert "boom" in resp.json()["detail"]


def test_duplicate_labels_rejected():
    payload = _payload("orders-api")
    payload["versions"][1]["label"] = "production-v1"
    resp = client.post("/api/v1/solve", json=payload)
    assert resp.status_code == 422
