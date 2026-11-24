"""Thin wrapper around the `helm template` CLI."""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class HelmRenderError(RuntimeError):
    """Raised when `helm template` exits non-zero."""


@dataclass(frozen=True)
class HelmResult:
    returncode: int
    stdout: str
    stderr: str


def ensure_helm_available() -> str:
    helm = shutil.which("helm")
    if helm is None:
        raise HelmRenderError(
            "helm executable not found on PATH; install Helm 3 to run this service"
        )
    return helm


def helm_template(
    chart_dir: Path,
    values_file: Path,
    release_name: str,
    namespace: str,
    timeout_seconds: int = 60,
) -> HelmResult:
    """Run `helm template` with a fixed release name and namespace.

    Never raises on helm failure; the caller decides whether a non-zero exit
    is a hard baseline failure or a combination to skip.
    """
    helm = ensure_helm_available()
    cmd = [
        helm,
        "template",
        release_name,
        str(chart_dir),
        "--namespace",
        namespace,
        "--values",
        str(values_file),
        "--include-crds",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return HelmResult(proc.returncode, proc.stdout, proc.stderr)
