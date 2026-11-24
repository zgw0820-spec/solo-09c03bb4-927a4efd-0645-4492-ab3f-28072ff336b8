"""Offline rolling-update batch planner.

Scope
-----
The caller supplies the *current* Deployment YAML and the manifests rendered
from the *new* chart.  Nothing here talks to a cluster: pairing, validation and
quota arithmetic are pure functions over those documents.

Pairing and eligibility
-----------------------
Deployments are paired by ``(metadata.namespace, metadata.name)``.  A missing
namespace falls back to the request's release namespace.  A pair is eligible
only when both sides are ``apps/v1`` Deployments, ``spec.replicas`` is
unchanged (missing means 1), and the Pod template changed.  At most
:data:`MAX_ELIGIBLE_PAIRS` pairs may be planned in one request; more is a
validation error.

Pairs that declare ``initContainers`` on either side are out of scope and are
reported as ignored instead of being planned: the quota model below only
accounts for regular containers, so silently dropping an init container's
request could understate the real peak.  Every regular container must declare
``resources.requests.cpu`` and ``resources.requests.memory`` in the canonical
forms documented in :mod:`app.quantities`.

Quota model
-----------
At the start of a batch every not-yet-upgraded Deployment still runs its old
ReplicaSet, every already-upgraded Deployment runs its new ReplicaSet, and the
batch's new Pods are created while the batch's old Pods are still present
(full old/new overlap).  For a batch ``S`` with set ``D`` of already upgraded
Deployments (when ``S`` overlaps the next batch, the previous batch must be
fully settled first, because a Deployment is planned exactly once)::

    usage_before = other_used + sum(new_total for D) + sum(old_total for not D)
    peak         = usage_before + sum(new_total for S)
    usage_after  = usage_before + sum(new_total - old_total for S)

Both CPU and memory peaks must be within the quota.  Once a batch's new Pods
are ready and its old Pods are gone, ``usage_after`` is the starting point for
the next batch.  Replicas never change, so totals are per-replica requests
summed over regular containers, multiplied by the replica count.

The minimum number of batches is found by exact search over subsets (at most
8 deployments, so at most 2**8 states).  Among equally short schedules the
planner prefers the largest first batch, then the earliest Deployments in the
submitted YAML order, so the result is deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

from .quantities import QuantityError, Requests, parse_request_map

MAX_ELIGIBLE_PAIRS = 8


class RolloutValidationError(ValueError):
    """The documents or amounts cannot be planned under the documented rules."""


@dataclass(frozen=True)
class DeploymentPair:
    namespace: str
    name: str
    replicas: int
    old_per_pod: Requests
    new_per_pod: Requests

    @property
    def old_total(self) -> Requests:
        """CPU/memory requested by all old replicas of this Deployment."""
        return self.old_per_pod.scaled(self.replicas)

    @property
    def new_total(self) -> Requests:
        """CPU/memory requested by all new replicas of this Deployment."""
        return self.new_per_pod.scaled(self.replicas)


@dataclass(frozen=True)
class IgnoredPair:
    namespace: str
    name: str
    reason: str


@dataclass(frozen=True)
class PlannedBatch:
    """One batch. ``peak`` includes the batch's new Pods overlapping all old Pods."""

    index: int
    deployments: tuple[DeploymentPair, ...]
    usage_before: Requests
    new_pods_added: Requests
    peak: Requests
    usage_after: Requests


@dataclass
class RolloutPlan:
    feasible: bool
    deployments: list[DeploymentPair]
    batches: list[PlannedBatch]
    ignored: list[IgnoredPair]
    quota: Requests
    other_used: Requests
    initial_usage: Requests
    final_usage: Requests | None
    reason: str | None = None
    min_batches: int | None = None


@dataclass
class _Snapshot:
    namespace: str
    name: str
    api_version: str
    replicas: int
    template: dict[str, Any]
    pod_spec: dict[str, Any]
    init_containers: int = 0


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RolloutValidationError(f"{field_name} must be a mapping")
    return value


def _require_name(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RolloutValidationError(f"{field_name} must be a non-empty string")
    return value


def _snapshot(doc: dict[str, Any], default_namespace: str, side: str) -> _Snapshot:
    metadata = _require_mapping(doc.get("metadata", {}), f"{side} Deployment metadata")
    name = _require_name(metadata.get("name"), f"{side} Deployment metadata.name")
    namespace = metadata.get("namespace") or default_namespace
    namespace = _require_name(namespace, f"{side} Deployment metadata.namespace")

    spec = _require_mapping(doc.get("spec"), f"{side} Deployment {namespace}/{name} spec")
    replicas = spec.get("replicas", 1)
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 0:
        raise RolloutValidationError(
            f"{side} Deployment {namespace}/{name} spec.replicas must be a "
            f"non-negative integer"
        )

    template = _require_mapping(
        spec.get("template"),
        f"{side} Deployment {namespace}/{name} spec.template",
    )
    pod_spec = _require_mapping(
        template.get("spec"),
        f"{side} Deployment {namespace}/{name} spec.template.spec",
    )
    init_containers = pod_spec.get("initContainers", [])
    if init_containers is None:
        init_containers = []
    if not isinstance(init_containers, list):
        raise RolloutValidationError(
            f"{side} Deployment {namespace}/{name} spec.template.spec.initContainers "
            "must be a list"
        )

    api_version = doc.get("apiVersion", "")
    return _Snapshot(
        namespace=namespace,
        name=name,
        api_version=api_version if isinstance(api_version, str) else "",
        replicas=replicas,
        template=template,
        pod_spec=pod_spec,
        init_containers=len(init_containers),
    )


def _regular_container_requests(
    snapshot: _Snapshot, side: str
) -> Requests:
    """Sum requests over regular containers only (init containers excluded)."""
    field_prefix = f"{side} Deployment {snapshot.namespace}/{snapshot.name}"
    containers = snapshot.pod_spec.get("containers")
    if not isinstance(containers, list) or not containers:
        raise RolloutValidationError(
            f"{field_prefix} must have at least one regular container"
        )
    per_pod = Requests(0, 0)
    for index, container in enumerate(containers):
        container = _require_mapping(container, f"{field_prefix} containers[{index}]")
        container_name = container.get("name") or f"#{index}"
        resources = _require_mapping(
            container.get("resources"),
            f"{field_prefix} container {container_name!r} resources",
        )
        requests = resources.get("requests")
        try:
            per_pod += parse_request_map(
                requests,
                f"{field_prefix} container {container_name!r} resources.requests",
            )
        except QuantityError as exc:
            raise RolloutValidationError(str(exc)) from exc
    return per_pod


def _deployment_snapshots(
    docs: Iterable[Any], default_namespace: str, side: str
) -> list[_Snapshot]:
    snapshots: list[_Snapshot] = []
    seen: set[tuple[str, str]] = set()
    for doc in docs:
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        snap = _snapshot(doc, default_namespace, side)
        key = (snap.namespace, snap.name)
        if key in seen:
            raise RolloutValidationError(
                f"{side} YAML contains duplicate Deployment {snap.namespace}/{snap.name}"
            )
        seen.add(key)
        snapshots.append(snap)
    return snapshots


def pair_deployments(
    current_docs: Iterable[Any],
    rendered_docs: Iterable[Any],
    namespace: str,
) -> tuple[list[DeploymentPair], list[IgnoredPair]]:
    """Pair current and rendered Deployments and apply the eligibility rules."""
    current = _deployment_snapshots(current_docs, namespace, "current")
    rendered = _deployment_snapshots(rendered_docs, namespace, "rendered")

    if not current:
        raise RolloutValidationError(
            "current_deployments_yaml contains no Deployment documents"
        )
    if not rendered:
        raise RolloutValidationError("the rendered chart contains no Deployment documents")

    rendered_by_key = {(item.namespace, item.name): item for item in rendered}
    current_by_key = {(item.namespace, item.name): item for item in current}
    pairs: list[DeploymentPair] = []
    ignored: list[IgnoredPair] = []

    for old in current:
        key = (old.namespace, old.name)
        new = rendered_by_key.get(key)
        if new is None:
            ignored.append(
                IgnoredPair(old.namespace, old.name, "no matching rendered Deployment")
            )
            continue

        if old.api_version != "apps/v1" or new.api_version != "apps/v1":
            raise RolloutValidationError(
                f"Deployment {old.namespace}/{old.name} must be apps/v1 on both "
                f"sides (current={old.api_version or 'missing'}, "
                f"rendered={new.api_version or 'missing'})"
            )
        if old.replicas != new.replicas:
            raise RolloutValidationError(
                f"Deployment {old.namespace}/{old.name} changes replicas "
                f"({old.replicas} -> {new.replicas}); only unchanged replicas are supported"
            )
        if old.template == new.template:
            ignored.append(
                IgnoredPair(old.namespace, old.name, "Pod template unchanged")
            )
            continue
        if old.init_containers or new.init_containers:
            ignored.append(
                IgnoredPair(
                    old.namespace,
                    old.name,
                    "init containers are out of scope for this planner",
                )
            )
            continue

        # Only planned pairs need both request amounts; ignored pairs are not
        # inspected for quantities (their Pods are not part of the schedule).
        old_per_pod = _regular_container_requests(old, "current")
        new_per_pod = _regular_container_requests(new, "rendered")
        pairs.append(
            DeploymentPair(
                namespace=old.namespace,
                name=old.name,
                replicas=old.replicas,
                old_per_pod=old_per_pod,
                new_per_pod=new_per_pod,
            )
        )

    for new in rendered:
        if (new.namespace, new.name) not in current_by_key:
            ignored.append(
                IgnoredPair(
                    new.namespace,
                    new.name,
                    "no matching current Deployment",
                )
            )

    if len(pairs) > MAX_ELIGIBLE_PAIRS:
        raise RolloutValidationError(
            f"{len(pairs)} eligible Deployment pairs exceed the maximum of "
            f"{MAX_ELIGIBLE_PAIRS}"
        )
    return pairs, ignored


def _sum_new(pairs: list[DeploymentPair], mask: int) -> Requests:
    total = Requests(0, 0)
    for index, pair in enumerate(pairs):
        if mask & (1 << index):
            total += pair.new_total
    return total


def plan_batches(
    pairs: list[DeploymentPair],
    quota: Requests,
    other_used: Requests,
) -> tuple[list[PlannedBatch], str | None]:
    """Return the minimum-batch schedule, or ``None`` reason when unsolvable."""
    count = len(pairs)
    full = (1 << count) - 1
    initial = other_used
    for pair in pairs:
        initial += pair.old_total
    if not initial.fits_within(quota):
        return [], (
            "usage before the first batch already exceeds the quota "
            f"(cpu {initial.cpu}, memory {initial.memory}); shrink other_used, "
            "quota, or the current Deployment requests"
        )

    def usage_before(done: int) -> Requests:
        total = other_used
        for index, pair in enumerate(pairs):
            total += pair.new_total if done & (1 << index) else pair.old_total
        return total

    def batch_peak(done: int, batch: int) -> Requests:
        return usage_before(done) + _sum_new(pairs, batch)

    @lru_cache(maxsize=None)
    def best_schedule(done: int) -> tuple[tuple[int, ...], ...] | None:
        if done == full:
            return ()
        remaining = full & ~done
        best: tuple[tuple[int, ...], ...] | None = None
        best_key: tuple[Any, ...] | None = None

        batch = remaining
        while batch:
            if batch_peak(done, batch).fits_within(quota):
                rest = best_schedule(done | batch)
                if rest is not None:
                    candidate = (batch,) + rest
                    key = (
                        len(candidate),
                        tuple(-bin(item).count("1") for item in candidate),
                        tuple(
                            tuple(
                                index
                                for index in range(count)
                                if item & (1 << index)
                            )
                            for item in candidate
                        ),
                    )
                    if best_key is None or key < best_key:
                        best = candidate
                        best_key = key
            batch = (batch - 1) & remaining
        return best

    schedule = best_schedule(0)
    if schedule is None:
        return [], (
            "no batching order keeps both CPU and memory peak usage within the "
            "quota; the transition cannot be scheduled without exceeding it"
        )

    batches: list[PlannedBatch] = []
    done = 0
    for index, batch in enumerate(schedule, start=1):
        before = usage_before(done)
        added = _sum_new(pairs, batch)
        peak = before + added
        selected = tuple(
            pair for position, pair in enumerate(pairs) if batch & (1 << position)
        )
        old_of_batch = Requests(0, 0)
        for pair in selected:
            old_of_batch += pair.old_total
        after = before + added - old_of_batch
        batches.append(
            PlannedBatch(
                index=index,
                deployments=selected,
                usage_before=before,
                new_pods_added=added,
                peak=peak,
                usage_after=after,
            )
        )
        done |= batch
    return batches, None


def plan_rollout(
    current_docs: Iterable[Any],
    rendered_docs: Iterable[Any],
    namespace: str,
    quota: Requests,
    other_used: Requests,
) -> RolloutPlan:
    pairs, ignored = pair_deployments(current_docs, rendered_docs, namespace)
    initial = other_used
    for pair in pairs:
        initial += pair.old_total

    batches, reason = plan_batches(pairs, quota, other_used)
    feasible = reason is None
    final = None
    if feasible:
        final = other_used
        for pair in pairs:
            final += pair.new_total

    return RolloutPlan(
        feasible=feasible,
        deployments=pairs,
        batches=batches,
        ignored=ignored,
        quota=quota,
        other_used=other_used,
        initial_usage=initial,
        final_usage=final,
        reason=reason,
        min_batches=len(batches) if feasible else None,
    )
