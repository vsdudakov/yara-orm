"""Render the benchmark charts used in the README and docs.

The numbers are the representative PostgreSQL results from ``benchmarks/README.md``
(PostgreSQL 18, Apple Silicon, Python 3.12, N=5000, median of 5). They are
embedded here rather than re-run so the chart is reproducible without a database;
keep them in sync with ``benchmarks/README.md`` and ``docs/performance.md`` if you
re-run ``bench.py``.

Usage::

    pip install matplotlib
    python benchmarks/plot_benchmarks.py

Writes ``docs/assets/benchmark-postgres.png``.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Operation -> latency in ms (lower is better), one entry per ORM.
OPERATIONS = [
    "bulk_insert",
    "single_insert",
    "fetch_all",
    "count",
    "filter",
    "get_by_pk",
    "update",
    "delete",
]
TIMES_MS = {
    "yara-orm": [11.0, 33.4, 3.4, 0.3, 2.2, 62.5, 3.4, 0.7],
    "tortoise": [24.2, 79.5, 16.1, 0.5, 8.4, 193.6, 3.4, 0.8],
    "sqlalchemy": [66.6, 151.3, 11.8, 0.9, 19.9, 294.3, 3.8, 1.0],
    "pony": [218.2, 59.7, 30.4, 0.5, 16.0, 82.5, 119.5, 90.7],
}

# yara-orm in brand deep-purple, competitors in muted greys so the winner pops.
COLORS = {
    "yara-orm": "#7c4dff",
    "tortoise": "#9e9e9e",
    "sqlalchemy": "#bdbdbd",
    "pony": "#e0e0e0",
}
EDGE = "#5e35b1"

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets" / "benchmark-postgres.png"


def main() -> None:
    orms = list(TIMES_MS)
    n_ops = len(OPERATIONS)
    n_orms = len(orms)
    bar_w = 0.8 / n_orms

    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=140)
    x = range(n_ops)

    for i, orm in enumerate(orms):
        offset = (i - (n_orms - 1) / 2) * bar_w
        positions = [xi + offset for xi in x]
        ax.bar(
            positions,
            TIMES_MS[orm],
            width=bar_w,
            label=orm,
            color=COLORS[orm],
            edgecolor=EDGE if orm == "yara-orm" else "none",
            linewidth=1.2 if orm == "yara-orm" else 0,
            zorder=3,
        )

    ax.set_yscale("log")
    ax.set_ylabel("milliseconds (log scale — lower is better)")
    ax.set_title(
        "Yara ORM vs Tortoise, SQLAlchemy & Pony — PostgreSQL 18, 5000 rows, median of 5",
        fontsize=12,
        fontweight="bold",
    )
    ax.set_xticks(list(x))
    ax.set_xticklabels(OPERATIONS, rotation=20, ha="right")
    ax.legend(frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.18))
    ax.grid(axis="y", which="both", color="#eeeeee", zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, bbox_inches="tight")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
