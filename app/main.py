"""FastAPI service for Helm work that must be reasoned about before rollout.

Endpoints:
* GET  /health             liveness probe
* POST /api/v1/solve       clean shared values overrides across chart versions
* POST /api/v1/rollout/plan  offline rolling-update batch plan under a quota

Swagger UI: /docs  (prefilled examples are attached to both POST endpoints).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .examples import rollout_sample_request
from .helm import HelmRenderError, helm_template
from .normalize import parse_documents
from .quantities import QuantityError, Requests, parse_request_map
from .rollout import RolloutPlan, RolloutValidationError, plan_rollout
from .solver import BaselineFailure, MAX_CANDIDATES, solve
from .values_tools import load_values

app = FastAPI(
    title="Helm Values Cleanup & Rollout Planning API",
    version="1.1.0",
    description=(
        "Two offline Helm workflows. **/api/v1/solve** renders each chart version "
        "with a shared values override and finds the largest set (up to 8) of "
        "mapping-key paths that can be deleted while every version's rendered "
        "manifests stay identical. **/api/v1/rollout/plan** renders one new chart, "
        "pairs its apps/v1 Deployments with the submitted current YAML by "
        "namespace/name, and computes the minimum-batch rolling update whose CPU "
        "and memory peaks fit the quota during full old/new Pod overlap. Neither "
        "endpoint contacts a cluster."
    ),
)

# Default chart root for the bundled two-chart dataset ("mounted directory").
DEFAULT_CHART_ROOT = Path(
    "/workspace/mounted-charts"
).resolve() if Path("/workspace/mounted-charts").is_dir() else Path("/charts")


class ChartVersion(BaseModel):
    label: str = Field(..., description="Stable label, e.g. 'production-v1'")
    chart_dir: str = Field(..., description="Absolute path to this version's chart directory")

    @field_validator("label")
    @classmethod
    def label_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("label must not be blank")
        return v


class SolveRequest(BaseModel):
    values_override_path: str = Field(
        ..., description="Absolute path to the shared values override YAML"
    )
    versions: list[ChartVersion] = Field(
        ..., min_length=1, description="Chart versions that share this override"
    )
    release_name: str = Field("platform-rel", description="Fixed helm release name")
    namespace: str = Field("platform", description="Fixed helm namespace")

    @field_validator("values_override_path", "release_name", "namespace")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("versions")
    @classmethod
    def unique_labels(cls, v: list[ChartVersion]) -> list[ChartVersion]:
        labels = [item.label for item in v]
        if len(set(labels)) != len(labels):
            raise ValueError("version labels must be unique")
        return v


class YAMLResponse(Response):
    """Minimal YAML response for clients sending Accept: application/yaml."""

    media_type = "application/yaml"

    def render(self, content: Any) -> bytes:
        return yaml.safe_dump(
            content,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        ).encode("utf-8")


class VersionReport(BaseModel):
    label: str
    chart_dir: str
    render_ok: bool
    manifests_match_baseline: bool


class SolveResponse(BaseModel):
    cleaned_yaml: str = Field(..., description="Override YAML with the removable keys deleted")
    removed_paths: list[str] = Field(..., description="Mapping-key paths safe to delete")
    candidate_paths: list[str] = Field(
        ..., description=f"At most {MAX_CANDIDATES} non-contained candidate paths considered"
    )
    kept_paths: list[str] = Field(..., description="All other leaf paths, retained as-is")
    total_leaf_paths: int
    candidate_cap_applied: bool
    verified: bool = Field(..., description="Final cleaned YAML re-rendered and matched on every version")
    versions: list[VersionReport]
    combinations_evaluated: int
    render_failures: int
    release_name: str
    namespace: str


class QuantityPair(BaseModel):
    """A CPU/memory pair in canonical units (millicores / mebibytes)."""

    cpu: str = Field(..., description="CPU in integer millicores, e.g. '300m'")
    memory: str = Field(..., description="Memory in integer mebibytes, e.g. '384Mi'")

    @classmethod
    def from_requests(cls, requests: Requests) -> "QuantityPair":
        return cls(cpu=requests.cpu, memory=requests.memory)


class RolloutRequest(BaseModel):
    current_deployments_yaml: str = Field(
        ...,
        description=(
            "YAML containing the current apps/v1 Deployment documents. "
            "Namespace + name are used to pair them with the rendered new chart."
        ),
    )
    chart_dir: str = Field(
        ..., description="Absolute path to the mounted new chart directory"
    )
    values_path: str = Field(
        ..., description="Absolute path to the values file used to render the new chart"
    )
    release_name: str = Field(..., description="Helm release name used for rendering")
    namespace: str = Field(..., description="Helm namespace used for rendering and pairing")
    quota: QuantityPair = Field(
        ..., description="Namespace quota for CPU and memory requests (both are enforced)"
    )
    other_used: QuantityPair | None = Field(
        None,
        description=(
            "Requests already used in the namespace by workloads outside the "
            "planned pairs (including Deployments returned under 'ignored'). "
            "Omitted means zero."
        ),
    )

    @field_validator(
        "current_deployments_yaml",
        "chart_dir",
        "values_path",
        "release_name",
        "namespace",
    )
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    def quota_requests(self) -> Requests:
        return _requests_from_pair(self.quota, "quota")

    def other_used_requests(self) -> Requests:
        if self.other_used is None:
            return Requests(0, 0)
        return _requests_from_pair(self.other_used, "other_used")


def _requests_from_pair(value: QuantityPair, field_name: str) -> Requests:
    try:
        return parse_request_map(
            {"cpu": value.cpu, "memory": value.memory}, field_name
        )
    except QuantityError as exc:
        raise ValueError(str(exc)) from exc


class RolloutDeploymentReport(BaseModel):
    namespace: str
    name: str
    replicas: int
    old_cpu: str
    old_memory: str
    new_cpu: str
    new_memory: str
    old_total_cpu: str
    old_total_memory: str
    new_total_cpu: str
    new_total_memory: str


class RolloutBatchDeployment(BaseModel):
    namespace: str
    name: str
    replicas: int
    new_cpu_per_pod: str
    new_memory_per_pod: str


class RolloutBatch(BaseModel):
    batch: int
    deployments: list[RolloutBatchDeployment]
    usage_before: QuantityPair
    new_pods_added: QuantityPair
    peak: QuantityPair
    usage_after: QuantityPair


class IgnoredDeployment(BaseModel):
    namespace: str
    name: str
    reason: str


class RolloutResponse(BaseModel):
    feasible: bool = Field(
        ...,
        description=(
            "False when no batching order keeps both peaks within the quota; "
            "batches is then empty and reason explains why."
        ),
    )
    reason: str | None = None
    min_batches: int | None = None
    batches: list[RolloutBatch] = Field(default_factory=list)
    deployments: list[RolloutDeploymentReport] = Field(default_factory=list)
    ignored: list[IgnoredDeployment] = Field(default_factory=list)
    quota: QuantityPair
    other_used: QuantityPair
    initial_usage: QuantityPair
    final_usage: QuantityPair | None = None
    release_name: str
    namespace: str


def _rollout_response(
    plan: RolloutPlan, request: RolloutRequest
) -> RolloutResponse:
    deployment_reports = [
        RolloutDeploymentReport(
            namespace=pair.namespace,
            name=pair.name,
            replicas=pair.replicas,
            old_cpu=pair.old_per_pod.cpu,
            old_memory=pair.old_per_pod.memory,
            new_cpu=pair.new_per_pod.cpu,
            new_memory=pair.new_per_pod.memory,
            old_total_cpu=pair.old_total.cpu,
            old_total_memory=pair.old_total.memory,
            new_total_cpu=pair.new_total.cpu,
            new_total_memory=pair.new_total.memory,
        )
        for pair in plan.deployments
    ]
    batch_reports = [
        RolloutBatch(
            batch=batch.index,
            deployments=[
                RolloutBatchDeployment(
                    namespace=pair.namespace,
                    name=pair.name,
                    replicas=pair.replicas,
                    new_cpu_per_pod=pair.new_per_pod.cpu,
                    new_memory_per_pod=pair.new_per_pod.memory,
                )
                for pair in batch.deployments
            ],
            usage_before=QuantityPair.from_requests(batch.usage_before),
            new_pods_added=QuantityPair.from_requests(batch.new_pods_added),
            peak=QuantityPair.from_requests(batch.peak),
            usage_after=QuantityPair.from_requests(batch.usage_after),
        )
        for batch in plan.batches
    ]
    return RolloutResponse(
        feasible=plan.feasible,
        reason=plan.reason,
        min_batches=plan.min_batches,
        batches=batch_reports,
        deployments=deployment_reports,
        ignored=[
            IgnoredDeployment(
                namespace=item.namespace, name=item.name, reason=item.reason
            )
            for item in plan.ignored
        ],
        quota=QuantityPair.from_requests(plan.quota),
        other_used=QuantityPair.from_requests(plan.other_used),
        initial_usage=QuantityPair.from_requests(plan.initial_usage),
        final_usage=(
            QuantityPair.from_requests(plan.final_usage)
            if plan.final_usage is not None
            else None
        ),
        release_name=request.release_name,
        namespace=request.namespace,
    )


def _example_request(chart: str, v1: str, v2: str) -> dict[str, Any]:
    return {
        "values_override_path": f"/workspace/overrides/{chart}.values.yaml",
        "release_name": "platform-rel",
        "namespace": "platform",
        "versions": [
            {
                "label": "production-v1",
                "chart_dir": f"/workspace/mounted-charts/{chart}/{v1}",
            },
            {
                "label": "staging-v2",
                "chart_dir": f"/workspace/mounted-charts/{chart}/{v2}",
            },
        ],
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/api/v1/solve",
    response_model=SolveResponse,
    responses={
        200: {
            "description": "Cleanup result (JSON by default, YAML with Accept: application/yaml)",
            "content": {
                "application/json": {},
                "application/yaml": {},
            },
        },
        400: {"description": "Invalid request / unreadable values file"},
        422: {"description": "Baseline helm template failed for a version"},
    },
)
def solve_endpoint(
    req: SolveRequest, accept: str = Header(default="")
) -> SolveResponse | YAMLResponse:
    values_path = Path(req.values_override_path)
    if not values_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"values override not found or not a file: {values_path}",
        )
    try:
        values = load_values(values_path)
    except (yaml.YAMLError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"failed to parse values YAML: {exc}"
        ) from exc

    versions = [item.model_dump() for item in req.versions]
    try:
        result = solve(
            values=values,
            versions=versions,
            release_name=req.release_name,
            namespace=req.namespace,
        )
    except BaselineFailure as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = SolveResponse(
        cleaned_yaml=result.cleaned_yaml,
        removed_paths=result.removed_paths,
        candidate_paths=result.candidate_paths,
        kept_paths=result.kept_paths,
        total_leaf_paths=result.total_leaf_paths,
        candidate_cap_applied=result.capped,
        verified=result.verified,
        versions=[VersionReport(**report) for report in result.versions],
        combinations_evaluated=result.combinations_evaluated,
        render_failures=result.render_failures,
        release_name=req.release_name,
        namespace=req.namespace,
    )
    # YAML response for clients sending Accept: application/yaml.
    if "yaml" in accept.lower():
        return YAMLResponse(payload.model_dump())
    return payload


@app.post(
    "/api/v1/rollout/plan",
    response_model=RolloutResponse,
    responses={
        200: {
            "description": (
                "Batch plan. feasible=false with an empty batches list means no "
                "schedule keeps both peaks within the quota."
            )
        },
        400: {"description": "Unreadable files or malformed YAML"},
        422: {
            "description": (
                "helm render failure, or a document/quantity that violates the rules"
            )
        },
    },
)
def rollout_plan_endpoint(req: RolloutRequest) -> RolloutResponse:
    chart_dir = Path(req.chart_dir)
    values_path = Path(req.values_path)
    if not chart_dir.is_dir():
        raise HTTPException(
            status_code=400, detail=f"chart directory not found: {chart_dir}"
        )
    if not values_path.is_file():
        raise HTTPException(
            status_code=400, detail=f"values file not found: {values_path}"
        )

    try:
        current_docs = parse_documents(req.current_deployments_yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=400, detail=f"failed to parse current_deployments_yaml: {exc}"
        ) from exc
    if not current_docs:
        raise HTTPException(
            status_code=400, detail="current_deployments_yaml contains no documents"
        )

    try:
        rendered = helm_template(
            chart_dir,
            values_path,
            req.release_name,
            req.namespace,
        )
    except HelmRenderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if rendered.returncode != 0:
        raise HTTPException(
            status_code=422,
            detail=f"helm template failed: {rendered.stderr.strip()[:2000]}",
        )

    rendered_docs = parse_documents(rendered.stdout)
    try:
        plan = plan_rollout(
            current_docs=current_docs,
            rendered_docs=rendered_docs,
            namespace=req.namespace,
            quota=req.quota_requests(),
            other_used=req.other_used_requests(),
        )
    except RolloutValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _rollout_response(plan, req)


# Attach the tryable examples to the OpenAPI schema after app creation.
def _inject_examples() -> None:
    path_schema = app.openapi()

    solve_op = path_schema["paths"]["/api/v1/solve"]["post"]
    solve_op["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/SolveRequest"},
                "examples": {
                    "orders-api (v1 production + v2 staging)": {
                        "summary": "orders-api shared override cleanup",
                        "value": _example_request("orders-api", "v1", "v2"),
                    },
                    "payment-web (v1 production + v2 staging)": {
                        "summary": "payment-web shared override cleanup",
                        "value": _example_request("payment-web", "v1", "v2"),
                    },
                },
            }
        },
    }

    rollout_op = path_schema["paths"]["/api/v1/rollout/plan"]["post"]
    rollout_op["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/RolloutRequest"},
                "examples": {
                    "rollout-demo (sequential order gets stuck)": {
                        "summary": (
                            "one-by-one in YAML order exceeds the quota on the "
                            "third step; the plan batches api+search, then worker"
                        ),
                        "value": rollout_sample_request(),
                    }
                },
            }
        },
    }
    app.openapi_schema = path_schema


_inject_examples()
