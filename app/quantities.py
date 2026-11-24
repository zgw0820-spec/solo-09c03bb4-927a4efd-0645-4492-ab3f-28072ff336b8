"""Strict Kubernetes resource-request quantities.

The rollout planner accepts only the two canonical forms the API documents:

* CPU: an integer number of millicores, e.g. ``"250m"``.
* memory: an integer number of mebibytes, e.g. ``"256Mi"``.

Values such as ``"0.25"``, ``"1"``, ``"250u"`` or ``"1Gi"`` are rejected instead
of being converted.  Keeping one representation makes the quota arithmetic
exact and the reported peaks unambiguous.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CPU_PATTERN = re.compile(r"^(0|[1-9][0-9]*)m$")
MEMORY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)Mi$")


class QuantityError(ValueError):
    """Raised for a missing or non-canonical resource quantity."""


@dataclass(frozen=True)
class Requests:
    """A pair of CPU/memory request amounts, used for one pod or a total."""

    cpu_millicores: int
    memory_mebibytes: int

    def __add__(self, other: "Requests") -> "Requests":
        return Requests(
            self.cpu_millicores + other.cpu_millicores,
            self.memory_mebibytes + other.memory_mebibytes,
        )

    def __sub__(self, other: "Requests") -> "Requests":
        return Requests(
            self.cpu_millicores - other.cpu_millicores,
            self.memory_mebibytes - other.memory_mebibytes,
        )

    def scaled(self, factor: int) -> "Requests":
        return Requests(
            self.cpu_millicores * factor,
            self.memory_mebibytes * factor,
        )

    def fits_within(self, quota: "Requests") -> bool:
        return (
            self.cpu_millicores <= quota.cpu_millicores
            and self.memory_mebibytes <= quota.memory_mebibytes
        )

    @property
    def cpu(self) -> str:
        return f"{self.cpu_millicores}m"

    @property
    def memory(self) -> str:
        return f"{self.memory_mebibytes}Mi"


def parse_cpu(value: Any, field: str) -> int:
    if not isinstance(value, str) or not CPU_PATTERN.fullmatch(value):
        raise QuantityError(
            f"{field} must be an integer number of millicores like '250m' "
            f"(got {value!r})"
        )
    return int(value[:-1])


def parse_memory(value: Any, field: str) -> int:
    if not isinstance(value, str) or not MEMORY_PATTERN.fullmatch(value):
        raise QuantityError(
            f"{field} must be an integer number of mebibytes like '256Mi' "
            f"(got {value!r})"
        )
    return int(value[:-2])


def parse_request_map(value: Any, field: str) -> Requests:
    """Parse a mapping with exactly the canonical ``cpu`` and ``memory`` keys."""
    if not isinstance(value, dict):
        raise QuantityError(f"{field} must be a mapping with cpu and memory")
    missing = [key for key in ("cpu", "memory") if key not in value]
    if missing:
        raise QuantityError(f"{field} is missing: {', '.join(missing)}")
    return Requests(
        cpu_millicores=parse_cpu(value["cpu"], f"{field}.cpu"),
        memory_mebibytes=parse_memory(value["memory"], f"{field}.memory"),
    )
