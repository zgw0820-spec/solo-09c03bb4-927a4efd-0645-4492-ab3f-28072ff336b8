"""Core cleanup solver.

Pipeline:
1. Render every chart version with the original override. Any baseline failure
   aborts the whole request with an error (per spec).
2. Enumerate leaf mapping-key paths from the override (all non-contained).
3. Screen each leaf individually: it is a candidate only when deleting it alone
   still renders successfully on every version AND keeps the manifests
   identical to baseline. This avoids wasting the 8-key candidate budget on
   keys that can never be removed.
4. Cap the individually-removable candidates to 8, then exhaustively search
   removal subsets, largest first. A subset is accepted only when every version
   still renders AND its manifests are semantically identical to the baseline.
   Combinations that fail to render are skipped and other combinations are
   tried (per spec).
5. Return the cleaned YAML plus the removable key paths; everything else stays.
"""
from __future__ import annotations

import itertools
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .helm import HelmRenderError, HelmResult, helm_template
from .normalize import manifests_equal
from .values_tools import (
    leaf_paths,
    remove_paths,
    render_path,
    dump_values,
)

MAX_CANDIDATES = 8


@dataclass
class VersionBaseline:
    label: str
    chart_dir: str
    ok: bool
    stderr: str = ""
    rendered: str = ""


@dataclass
class SolveResult:
    cleaned_yaml: str
    removed_paths: list[str]
    candidate_paths: list[str]
    kept_paths: list[str]
    total_leaf_paths: int
    capped: bool
    versions: list[dict[str, Any]] = field(default_factory=list)
    combinations_evaluated: int = 0
    render_failures: int = 0
    verified: bool = False


class BaselineFailure(Exception):
    def __init__(self, failures: list[VersionBaseline]):
        self.failures = failures
        detail = "; ".join(f"{f.label}: {f.stderr.strip()[:300]}" for f in failures)
        super().__init__(f"baseline helm template failed -> {detail}")


def render_versions(
    versions: list[dict[str, str]],
    values_file: Path,
    release_name: str,
    namespace: str,
) -> list[VersionBaseline]:
    baselines: list[VersionBaseline] = []
    for version in versions:
        chart_dir = Path(version["chart_dir"])
        if not chart_dir.is_dir():
            baselines.append(
                VersionBaseline(
                    label=version["label"],
                    chart_dir=str(chart_dir),
                    ok=False,
                    stderr=f"chart directory not found: {chart_dir}",
                )
            )
            continue
        try:
            result: HelmResult = helm_template(
                chart_dir, values_file, release_name, namespace
            )
        except (HelmRenderError, FileNotFoundError) as exc:
            baselines.append(
                VersionBaseline(
                    label=version["label"],
                    chart_dir=str(chart_dir),
                    ok=False,
                    stderr=str(exc),
                )
            )
            continue
        baselines.append(
            VersionBaseline(
                label=version["label"],
                chart_dir=str(chart_dir),
                ok=result.returncode == 0,
                stderr=result.stderr,
                rendered=result.stdout,
            )
        )
    return baselines


def select_candidates(
    removable_leaves: list[tuple[str, ...]], max_candidates: int = MAX_CANDIDATES
) -> tuple[list[tuple[str, ...]], bool]:
    """Cap individually-removable leaves deterministically (override order).

    Leaf paths are non-contained by construction.
    """
    capped = len(removable_leaves) > max_candidates
    return removable_leaves[:max_candidates], capped


def _trial_render_matches(
    versions: list[dict[str, str]],
    trial_file: Path,
    baseline_rendered: dict[str, str],
    release_name: str,
    namespace: str,
) -> tuple[bool, bool]:
    """Render all versions with a trial values file.

    Returns (render_ok_all, manifests_match_baseline_all).
    """
    render_ok = True
    matches = True
    for version in versions:
        result = helm_template(
            Path(version["chart_dir"]),
            trial_file,
            release_name,
            namespace,
        )
        if result.returncode != 0:
            return False, False
        render_ok = render_ok and result.returncode == 0
        matches = matches and manifests_equal(
            baseline_rendered[version["label"]], result.stdout
        )
    return render_ok, matches


def solve(
    values: dict[str, Any],
    versions: list[dict[str, str]],
    release_name: str,
    namespace: str,
) -> SolveResult:
    all_leaves = leaf_paths(values)

    with tempfile.TemporaryDirectory(prefix="helm-cleanup-") as tmp:
        tmp_dir = Path(tmp)
        baseline_file = tmp_dir / "baseline.values.yaml"
        baseline_file.write_text(dump_values(values), encoding="utf-8")

        # Step 1: baselines. Any failure here is fatal for the request.
        baselines = render_versions(
            versions, baseline_file, release_name, namespace
        )
        failures = [b for b in baselines if not b.ok]
        if failures:
            raise BaselineFailure(failures)
        baseline_rendered = {b.label: b.rendered for b in baselines}

        # Step 2/3: screen every leaf individually. Only keys that are safe to
        # remove on their own deserve a slot in the 8-key candidate budget.
        individually_removable: list[tuple[str, ...]] = []
        render_failures = 0
        for leaf in all_leaves:
            trial_values = remove_paths(values, [leaf])
            trial_file = tmp_dir / "screen.values.yaml"
            trial_file.write_text(dump_values(trial_values), encoding="utf-8")
            render_ok, matches = _trial_render_matches(
                versions,
                trial_file,
                baseline_rendered,
                release_name,
                namespace,
            )
            if not render_ok:
                render_failures += 1
            elif matches:
                individually_removable.append(leaf)

        candidates, capped = select_candidates(individually_removable)

        # Step 4: exhaustive subset search over the capped candidates,
        # largest subset first.
        best: list[tuple[str, ...]] = []
        evaluated = 0
        n = len(candidates)
        for size in range(n, 0, -1):
            found_at_size = False
            for subset in itertools.combinations(candidates, size):
                subset_list = list(subset)
                cleaned = remove_paths(values, subset_list)
                trial_file = tmp_dir / "trial.values.yaml"
                trial_file.write_text(dump_values(cleaned), encoding="utf-8")

                evaluated += 1
                render_ok, matches = _trial_render_matches(
                    versions,
                    trial_file,
                    baseline_rendered,
                    release_name,
                    namespace,
                )
                if not render_ok:
                    # Combination fails to render: skip and try others.
                    render_failures += 1
                    continue
                if matches:
                    best = subset_list
                    found_at_size = True
                    break
            if found_at_size:
                break

        # Step 4: build cleaned output and re-verify it once against baseline.
        cleaned_values = remove_paths(values, best)
        final_file = tmp_dir / "final.values.yaml"
        final_file.write_text(dump_values(cleaned_values), encoding="utf-8")
        verified = True
        version_reports: list[dict[str, Any]] = []
        for version in versions:
            result = helm_template(
                Path(version["chart_dir"]),
                final_file,
                release_name,
                namespace,
            )
            same = (
                result.returncode == 0
                and manifests_equal(
                    baseline_rendered[version["label"]], result.stdout
                )
            )
            verified = verified and same
            version_reports.append(
                {
                    "label": version["label"],
                    "chart_dir": version["chart_dir"],
                    "render_ok": result.returncode == 0,
                    "manifests_match_baseline": same,
                }
            )

    removed = [render_path(p) for p in best]
    candidate_text = [render_path(p) for p in candidates]
    kept = [
        render_path(p)
        for p in all_leaves
        if p not in set(best)
    ]

    return SolveResult(
        cleaned_yaml=dump_values(cleaned_values),
        removed_paths=removed,
        candidate_paths=candidate_text,
        kept_paths=kept,
        total_leaf_paths=len(all_leaves),
        capped=capped,
        versions=version_reports,
        combinations_evaluated=evaluated,
        render_failures=render_failures,
        verified=verified,
    )
