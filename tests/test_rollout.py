"""Unit tests for the offline rollout batch planner."""
from __future__ import annotations

import pytest

from app.quantities import Requests
from app.rollout import (
    MAX_ELIGIBLE_PAIRS,
    DeploymentPair,
    RolloutValidationError,
    pair_deployments,
    plan_batches,
    plan_rollout,
)


def deployment(
    name: str,
    *,
    namespace: str = "ns",
    replicas: int = 1,
    cpu: str | None = "100m",
    memory: str | None = "128Mi",
    image: str = "img:v1",
    api_version: str = "apps/v1",
    init_container: bool = False,
) -> dict:
    pod_spec: dict = {
        "containers": [
            {
                "name": name,
                "image": image,
                "resources": {"requests": {}},
            }
        ]
    }
    requests = pod_spec["containers"][0]["resources"]["requests"]
    if cpu is not None:
        requests["cpu"] = cpu
    if memory is not None:
        requests["memory"] = memory
    if init_container:
        pod_spec["initContainers"] = [
            {
                "name": "init",
                "image": "init:v1",
                "resources": {"requests": {"cpu": "10m", "memory": "10Mi"}},
            }
        ]
    return {
        "apiVersion": api_version,
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": pod_spec,
            },
        },
    }


def pair(
    name: str,
    old_cpu: int,
    old_mem: int,
    new_cpu: int,
    new_mem: int,
    replicas: int = 1,
) -> DeploymentPair:
    return DeploymentPair(
        namespace="ns",
        name=name,
        replicas=replicas,
        old_per_pod=Requests(old_cpu, old_mem),
        new_per_pod=Requests(new_cpu, new_mem),
    )


# --------------------------------------------------------------------------
# Pairing / eligibility
# --------------------------------------------------------------------------


def test_pairing_uses_namespace_and_name_not_order():
    old = [deployment("b", image="img:v1"), deployment("a", image="img:v1")]
    new = [deployment("a", image="img:v2"), deployment("b", image="img:v2")]
    pairs, ignored = pair_deployments(old, new, "ns")
    assert ignored == []
    assert [(p.namespace, p.name) for p in pairs] == [("ns", "b"), ("ns", "a")]


def test_missing_namespace_falls_back_to_release_namespace():
    old = deployment("api")
    del old["metadata"]["namespace"]
    new = deployment("api", image="img:v2")
    new["metadata"]["namespace"] = "ns"
    pairs, _ = pair_deployments([old], [new], "ns")
    assert pairs[0].namespace == "ns"


def test_unchanged_template_is_ignored():
    pairs, ignored = pair_deployments([deployment("api")], [deployment("api")], "ns")
    assert pairs == []
    assert ignored[0].reason == "Pod template unchanged"


def test_replica_change_is_rejected():
    old = [deployment("api", replicas=2)]
    new = [deployment("api", replicas=3, image="img:v2")]
    with pytest.raises(RolloutValidationError, match="replicas"):
        pair_deployments(old, new, "ns")


def test_non_apps_v1_is_rejected():
    old = [deployment("api", api_version="apps/v1beta2")]
    new = [deployment("api", image="img:v2")]
    with pytest.raises(RolloutValidationError, match="apps/v1"):
        pair_deployments(old, new, "ns")


def test_unmatched_deployments_are_ignored():
    old = [deployment("api"), deployment("gone")]
    new = [deployment("api", image="img:v2"), deployment("brand-new")]
    pairs, ignored = pair_deployments(old, new, "ns")
    assert [p.name for p in pairs] == ["api"]
    reasons = {(i.name, i.reason) for i in ignored}
    assert ("gone", "no matching rendered Deployment") in reasons
    assert ("brand-new", "no matching current Deployment") in reasons


def test_init_container_pairs_are_ignored_not_planned():
    old = [deployment("api", init_container=True)]
    new = [deployment("api", image="img:v2")]
    pairs, ignored = pair_deployments(old, new, "ns")
    assert pairs == []
    assert "init containers" in ignored[0].reason


def test_missing_requests_are_rejected():
    old = [deployment("api", memory=None)]
    new = [deployment("api", image="img:v2")]
    with pytest.raises(RolloutValidationError, match="memory"):
        pair_deployments(old, new, "ns")


def test_non_canonical_quantity_is_rejected():
    old = [deployment("api", cpu="0.1")]
    new = [deployment("api", image="img:v2")]
    with pytest.raises(RolloutValidationError, match="millicores"):
        pair_deployments(old, new, "ns")


def test_replicas_multiply_per_pod_requests():
    old = [deployment("api", replicas=3)]
    new = [deployment("api", replicas=3, image="img:v2", cpu="200m", memory="256Mi")]
    pairs, _ = pair_deployments(old, new, "ns")
    assert pairs[0].old_total == Requests(300, 384)
    assert pairs[0].new_total == Requests(600, 768)


def test_more_than_eight_pairs_is_rejected():
    old = [deployment(f"d{i}") for i in range(MAX_ELIGIBLE_PAIRS + 1)]
    new = [deployment(f"d{i}", image="img:v2") for i in range(MAX_ELIGIBLE_PAIRS + 1)]
    with pytest.raises(RolloutValidationError, match="maximum"):
        pair_deployments(old, new, "ns")


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


def test_everything_fits_in_one_batch():
    pairs = [pair("a", 100, 100, 200, 200), pair("b", 100, 100, 200, 200)]
    # Starting usage 200; one batch adds 400 -> peak 600.
    batches, reason = plan_batches(pairs, Requests(600, 600), Requests(0, 0))
    assert reason is None
    assert len(batches) == 1
    assert batches[0].peak == Requests(600, 600)


def test_grouping_beats_naive_ordering():
    """Sequential api -> worker -> search gets stuck on the third step."""
    pairs = [
        pair("api", 50, 64, 50, 64),
        pair("worker", 50, 64, 150, 192),
        pair("search", 100, 128, 200, 256),
    ]
    quota = Requests(500, 640)
    other = Requests(50, 64)

    # Naive one-at-a-time in YAML order exceeds the quota.
    usage = other + Requests(200, 256)
    naive = True
    for item in pairs:
        usage = usage + item.new_total
        if not usage.fits_within(quota):
            naive = False
            break
        usage = usage - item.old_total
    assert naive is False

    batches, reason = plan_batches(pairs, quota, other)
    assert reason is None
    assert len(batches) == 2
    assert [p.name for p in batches[0].deployments] == ["api", "search"]
    assert [p.name for p in batches[1].deployments] == ["worker"]
    assert batches[0].peak == quota
    assert batches[1].peak == quota
    assert batches[1].usage_after == Requests(450, 576)


def test_peak_accounts_for_full_old_new_overlap():
    pairs = [pair("a", 100, 100, 300, 300)]
    batches, reason = plan_batches(pairs, Requests(400, 400), Requests(0, 0))
    assert reason is None
    assert batches[0].usage_before == Requests(100, 100)
    # New Pod arrives while the old Pod still exists: 100 + 300 = 400.
    assert batches[0].peak == Requests(400, 400)
    assert batches[0].usage_after == Requests(300, 300)


def test_quota_checked_at_full_overlap_not_just_after():
    pairs = [pair("a", 100, 100, 300, 300)]
    # Steady state (300) fits 350, but the overlap peak (400) does not.
    batches, reason = plan_batches(pairs, Requests(350, 350), Requests(0, 0))
    assert batches == []
    assert reason is not None


def test_memory_alone_can_make_a_plan_infeasible():
    pairs = [pair("a", 100, 100, 100, 500)]
    batches, reason = plan_batches(pairs, Requests(1000, 550), Requests(0, 0))
    assert reason is not None
    assert batches == []


def test_infeasible_returns_no_batches_and_reason():
    pairs = [pair("a", 100, 100, 1000, 1000)]
    batches, reason = plan_batches(pairs, Requests(500, 500), Requests(0, 0))
    assert batches == []
    assert "quota" in reason


def test_minimum_batch_count_prefers_larger_first_batch():
    pairs = [
        pair("a", 100, 100, 100, 100),
        pair("b", 100, 100, 100, 100),
        pair("c", 100, 100, 100, 100),
    ]
    # Starting 300; any batch of 3 -> 600; quota 500 forces two batches.
    batches, reason = plan_batches(pairs, Requests(500, 500), Requests(0, 0))
    assert reason is None
    assert len(batches) == 2
    # Largest-first tie-break: two deployments, then one.
    assert [p.name for p in batches[0].deployments] == ["a", "b"]
    assert [p.name for p in batches[1].deployments] == ["c"]


def test_plan_rollout_reports_usage_bookends():
    old = [deployment("api", cpu="100m", memory="128Mi")]
    new = [deployment("api", cpu="200m", memory="256Mi", image="img:v2")]
    plan = plan_rollout(old, new, "ns", Requests(1000, 1000), Requests(50, 64))
    assert plan.initial_usage == Requests(150, 192)
    assert plan.final_usage == Requests(250, 320)
    assert plan.min_batches == 1
    assert plan.feasible is True
    assert plan.reason is None


def test_eight_pairs_are_planned_quickly():
    pairs = [pair(f"d{i}", 100, 100, 150, 150, replicas=2) for i in range(4)]
    pairs += [pair(f"m{i}", 100, 100, 150, 150, replicas=2) for i in range(4)]
    other = Requests(100, 100)
    batches, reason = plan_batches(pairs, Requests(3000, 3000), other)
    assert reason is None
    assert batches[0].deployments == tuple(pairs[:4])
    assert len(batches) == 3
    assert all(batch.peak.fits_within(Requests(3000, 3000)) for batch in batches)


def test_plan_is_deterministic_for_repeated_calls():
    pairs = [pair("a", 100, 100, 100, 100) for _ in range(1)]
    pairs += [pair("b", 100, 100, 100, 100), pair("c", 100, 100, 100, 100)]
    first, _ = plan_batches(pairs, Requests(500, 500), Requests(0, 0))
    second, _ = plan_batches(pairs, Requests(500, 500), Requests(0, 0))
    assert [b.deployments for b in first] == [b.deployments for b in second]


def test_usage_over_quota_before_first_batch_is_infeasible():
    pairs = [pair("a", 400, 400, 400, 400)]
    batches, reason = plan_batches(pairs, Requests(300, 300), Requests(0, 0))
    assert batches == []
    assert "before the first batch" in reason
