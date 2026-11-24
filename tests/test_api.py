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


# ---------------------------------------------------------------------------
# /api/v1/rollout/plan
# ---------------------------------------------------------------------------


def _rollout_payload() -> dict:
    from app.examples import rollout_sample_request

    return rollout_sample_request()


def test_rollout_sample_plan_batches_and_pairs():
    resp = client.post("/api/v1/rollout/plan", json=_rollout_payload())
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is True
    assert body["min_batches"] == 2
    assert len(body["batches"]) == 2
    assert [d["name"] for d in body["batches"][0]["deployments"]] == [
        "rollout-demo-api",
        "rollout-demo-search",
    ]
    assert [d["name"] for d in body["batches"][1]["deployments"]] == [
        "rollout-demo-worker"
    ]
    assert body["batches"][0]["peak"] == {"cpu": "500m", "memory": "640Mi"}
    assert body["batches"][1]["peak"] == {"cpu": "500m", "memory": "640Mi"}
    # The init-container Deployment is reported as ignored, never planned.
    assert body["ignored"] == [
        {
            "namespace": "rollout-demo",
            "name": "rollout-demo-report",
            "reason": "init containers are out of scope for this planner",
        }
    ]
    planned = {item["name"] for item in body["deployments"]}
    assert "rollout-demo-report" not in planned


def test_rollout_sequential_order_would_get_stuck():
    """Sanity-check the sample really is an ordering trap."""
    body = _rollout_payload()
    import yaml

    docs = [d for d in yaml.safe_load_all(body["current_deployments_yaml"]) if d]
    old = {d["metadata"]["name"]: d for d in docs}
    new_cpu = {
        "rollout-demo-api": 50,
        "rollout-demo-worker": 150,
        "rollout-demo-search": 200,
    }
    old_cpu = {
        name: int(d["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"][:-1])
        for name, d in old.items()
        if name in new_cpu
    }
    usage = 50 + sum(old_cpu.values())  # other_used + all old Pods
    for name in ["rollout-demo-api", "rollout-demo-worker"]:
        usage += new_cpu[name]
        assert usage <= 500
        usage -= old_cpu[name]
    usage += new_cpu["rollout-demo-search"]
    assert usage > 500  # stuck on the third sequential step


def test_rollout_missing_chart_dir_is_400():
    payload = _rollout_payload()
    payload["chart_dir"] = "/workspace/mounted-charts/does-not-exist"
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 400


def test_rollout_missing_values_is_400():
    payload = _rollout_payload()
    payload["values_path"] = "/workspace/overrides/nope.yaml"
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 400


def test_rollout_bad_yaml_is_400():
    payload = _rollout_payload()
    payload["current_deployments_yaml"] = "a: [unclosed"
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 400


def test_rollout_helm_failure_is_422(tmp_path):
    chart = tmp_path / "broken"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: broken\nversion: 0.1.0\n", encoding="utf-8"
    )
    templates = chart / "templates"
    templates.mkdir()
    (templates / "bad.yaml").write_text('{{- fail "rollout boom" -}}\n', encoding="utf-8")
    values = tmp_path / "values.yaml"
    values.write_text("x: 1\n", encoding="utf-8")

    payload = _rollout_payload()
    payload["chart_dir"] = str(chart)
    payload["values_path"] = str(values)
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 422
    assert "rollout boom" in resp.json()["detail"]


def test_rollout_replica_change_is_422():
    payload = _rollout_payload()
    payload["current_deployments_yaml"] = payload[
        "current_deployments_yaml"
    ].replace("  replicas: 1\n  selector:\n    matchLabels:\n      app.kubernetes.io/name: rollout-demo\n      app.kubernetes.io/component: api", "  replicas: 2\n  selector:\n    matchLabels:\n      app.kubernetes.io/name: rollout-demo\n      app.kubernetes.io/component: api", 1)
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 422
    assert "replicas" in resp.json()["detail"]


def test_rollout_non_canonical_quantity_is_422():
    payload = _rollout_payload()
    payload["quota"] = {"cpu": "0.5", "memory": "640Mi"}
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 422
    assert "millicores" in resp.json()["detail"]


def test_rollout_infeasible_returns_200_without_batches():
    payload = _rollout_payload()
    payload["quota"] = {"cpu": "300m", "memory": "300Mi"}
    resp = client.post("/api/v1/rollout/plan", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["feasible"] is False
    assert body["batches"] == []
    assert body["min_batches"] is None
    assert body["reason"]


def test_rollout_openapi_has_example():
    schema = app.openapi()
    examples = schema["paths"]["/api/v1/rollout/plan"]["post"]["requestBody"][
        "content"
    ]["application/json"]["examples"]
    assert len(examples) == 1
    value = next(iter(examples.values()))["value"]
    assert "current_deployments_yaml" in value
    assert value["quota"] == {"cpu": "500m", "memory": "640Mi"}
