"""Latency measurement and percentiles for the run report."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Percentiles:
    count: int
    p50: float
    p95: float
    p99: float
    max: float
    mean: float


def _pct(sorted_ms: list[float], q: float) -> float:
    """Standard nearest-rank percentile: value at rank ceil(q/100 * n).

    Deliberately not round((n-1)*q): rounding down would under-report the
    very number (p95) the task's hard constraint is about."""
    if not sorted_ms:
        return 0.0
    k = max(1, min(len(sorted_ms), math.ceil(q / 100 * len(sorted_ms))))
    return sorted_ms[k - 1]


def summarize(times_ms: list[float]) -> Percentiles:
    if not times_ms:
        return Percentiles(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    s = sorted(times_ms)
    return Percentiles(
        count=len(s),
        p50=_pct(s, 50),
        p95=_pct(s, 95),
        p99=_pct(s, 99),
        max=s[-1],
        mean=sum(s) / len(s),
    )


def format_by_branch(branch_times: dict[str, list[float]]) -> str:
    """Per-branch p50/p95 table, so the latency doc shows why paths differ."""
    lines = ["branch                        n     p50      p95      max"]
    for branch in sorted(branch_times):
        p = summarize(branch_times[branch])
        lines.append(
            f"{branch:<28} {p.count:>3}  {p.p50:>6.2f}  {p.p95:>6.2f}  {p.max:>6.2f}"
        )
    return "\n".join(lines)