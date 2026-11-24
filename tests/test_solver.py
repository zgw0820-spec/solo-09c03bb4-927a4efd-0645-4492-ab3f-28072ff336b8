"""End-to-end solver tests against the bundled charts."""
from pathlib import Path

import pytest

from app.solver import BaselineFailure, solve
from app.values_tools import load_values

ROOT = Path("/workspace")
MOUNTED = ROOT / "mounted-charts"
OVERRIDES = ROOT / "overrides"
RELEASE = "platform-rel"
NAMESPACE = "platform"

pytestmark = pytest.mark.skipif(
    not MOUNTED.is_dir(), reason="bundled chart dataset not present"
)


def _versions(chart: str) -> list[dict[str, str]]:
    return [
        {
            "label": "production-v1",
            "chart_dir": str(MOUNTED / chart / "v1"),
        },
        {
            "label": "staging-v2",
            "chart_dir": str(MOUNTED / chart / "v2"),
        },
    ]


@pytest.mark.parametrize("chart", ["orders-api", "payment-web"])
def test_removable_sets_verified_on_both_versions(chart):
    values = load_values(OVERRIDES / f"{chart}.values.yaml")
    result = solve(values, _versions(chart), RELEASE, NAMESPACE)

    assert result.verified is True
    assert len(result.removed_paths) <= 8
    assert len(result.candidate_paths) <= 8
    assert all(report["manifests_match_baseline"] for report in result.versions)
    # Every removed path was an advertised candidate.
    assert set(result.removed_paths).issubset(set(result.candidate_paths))


def test_orders_api_finds_all_eight_redundant_keys_and_keeps_traps():
    values = load_values(OVERRIDES / "orders-api.values.yaml")
    result = solve(values, _versions("orders-api"), RELEASE, NAMESPACE)

    assert set(result.removed_paths) == {
        "nameOverride",
        "image.repository",
        "image.pullPolicy",
        "image.pullSecrets",
        "tolerations",
        "service.type",
        "autoscaling.minReplicas",
        "autoscaling.targetCPUUtilizationPercentage",
    }
    # Cross-version default flip: must stay.
    assert "podSecurityContext.runAsNonRoot" in result.kept_paths
    assert "podSecurityContext.runAsUser" in result.kept_paths
    # Effective keys must stay.
    assert "autoscaling.enabled" in result.kept_paths
    assert "config.logLevel" in result.kept_paths


def test_payment_web_caps_candidates_at_eight_and_keeps_traps():
    values = load_values(OVERRIDES / "payment-web.values.yaml")
    result = solve(values, _versions("payment-web"), RELEASE, NAMESPACE)

    assert result.capped is True
    assert len(result.candidate_paths) == 8
    assert len(result.removed_paths) == 8
    # nodeSelector only exists on v2: looks harmless on v1 but must be kept.
    assert "nodeSelector.disktype" in result.kept_paths
    assert "securityContext.runAsNonRoot" in result.kept_paths
    assert "podAnnotations.checksum/legacy" in result.kept_paths


def test_baseline_failure_raises(tmp_path):
    bad_chart = tmp_path / "broken"
    bad_chart.mkdir()
    (bad_chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: broken\nversion: 0.1.0\n", encoding="utf-8"
    )
    templates = bad_chart / "templates"
    templates.mkdir()
    (templates / "broken.yaml").write_text(
        '{{- fail "intentional template failure" -}}\n', encoding="utf-8"
    )

    values = load_values(OVERRIDES / "orders-api.values.yaml")
    versions = [{"label": "broken", "chart_dir": str(bad_chart)}]
    with pytest.raises(BaselineFailure) as exc_info:
        solve(values, versions, RELEASE, NAMESPACE)
    assert "intentional template failure" in str(exc_info.value)


def test_render_failing_combination_is_skipped(tmp_path):
    """A template that only breaks when a specific key is absent.

    The solver must skip the failing deletion and keep that key, while other
    redundant keys are still removed.
    """
    chart = tmp_path / "conditional"
    chart.mkdir()
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: conditional\nversion: 0.1.0\n", encoding="utf-8"
    )
    (chart / "values.yaml").write_text(
        # Chart default for keepMe breaks rendering; only the override value
        # (which the cleanup must keep) makes the template valid.
        "keepMe: broken\nredundant: default-value\n",
        encoding="utf-8",
    )
    templates = chart / "templates"
    templates.mkdir()
    (templates / "cm.yaml").write_text(
        """{{- if eq .Values.keepMe "broken" }}
{{- fail "keepMe override must be present" }}
{{- end }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-conditional
data:
  keepMe: {{ .Values.keepMe | quote }}
  redundant: {{ .Values.redundant | quote }}
""",
        encoding="utf-8",
    )
    values = {"keepMe": "safe", "redundant": "default-value"}
    result = solve(
        values,
        [{"label": "only", "chart_dir": str(chart)}],
        RELEASE,
        NAMESPACE,
    )
    assert result.removed_paths == ["redundant"]
    assert "keepMe" in result.kept_paths
    assert result.verified is True
    assert result.render_failures >= 1
