"""FastAPI service: clean shared Helm values overrides across chart versions.

Endpoints:
* GET  /health       liveness probe
* POST /api/v1/solve run the cleanup analysis for one override + N chart versions

Swagger UI: /docs  (the two built-in examples are prefilled there).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from .solver import BaselineFailure, MAX_CANDIDATES, solve
from .values_tools import load_values

app = FastAPI(
    title="Shared Helm Values Cleanup API",
    version="1.0.0",
    description=(
        "Renders each chart version with the shared values override, then finds "
        "the largest set (up to 8) of non-contained mapping-key paths that can be "
        "deleted while keeping every version's rendered manifests identical. "
        "Comments, document order and mapping-key order are ignored; list order is kept."
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


# Attach the two tryable examples to the OpenAPI schema after app creation.
def _inject_examples() -> None:
    path_schema = app.openapi()
    op = path_schema["paths"]["/api/v1/solve"]["post"]
    component_ref = "#/components/schemas/SolveRequest"
    op["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": component_ref},
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
    app.openapi_schema = path_schema


_inject_examples()
