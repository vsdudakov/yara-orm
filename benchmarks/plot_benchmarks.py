"""Render the benchmark charts used in the README and docs.

The numbers are the representative results from ``benchmarks/README.md``
(Apple Silicon, Python 3.12, N=5000, median of 5; PostgreSQL 18 / MySQL 8.4 /
SQLite). They are embedded here rather than re-run so the charts are
reproducible without a database; keep them in sync with
``benchmarks/README.md`` and ``docs/performance.md`` if you re-run
``bench.py``.

Usage::

    pip install matplotlib
    python benchmarks/plot_benchmarks.py

Writes ``docs/assets/benchmark-{postgres,mysql,sqlite}.png``.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Operation order shared by every chart (latencies in ms, lower is better).
OPERATIONS = [
    "bulk_insert",
    "single_insert",
    "fetch_all",
    "count",
    "group_by",
    "filter",
    "get_by_pk",
    "update",
    "delete",
]

#: Backend -> (chart title, ORM -> per-operation latency ms).
BACKENDS: dict[str, tuple[str, dict[str, list[float]]]] = {
    "postgres": (
        "Yara ORM vs Tortoise, SQLAlchemy & Pony — PostgreSQL 18, 5000 rows, median of 5",
        {
            "yara-orm": [14.7, 34.2, 3.5, 0.3, 0.7, 2.2, 65.0, 3.2, 0.7],
            "tortoise": [24.2, 80.0, 16.7, 0.5, 1.2, 8.5, 194.9, 3.4, 0.8],
            "sqlalchemy": [68.0, 150.7, 21.3, 0.9, 1.4, 7.5, 287.0, 3.8, 1.1],
            "pony": [220.1, 60.9, 34.4, 0.4, 2.3, 17.6, 84.1, 119.8, 92.8],
        },
    ),
    "mysql": (
        "Yara ORM vs Tortoise, SQLAlchemy & Pony — MySQL 8.4, 5000 rows, median of 5",
        {
            "yara-orm": [46.0, 693.7, 5.6, 0.7, 1.2, 3.4, 110.9, 7.2, 4.9],
            "tortoise": [47.3, 753.7, 34.2, 1.1, 1.5, 17.9, 227.7, 7.8, 5.1],
            "sqlalchemy": [799.7, 904.4, 38.9, 1.3, 2.1, 16.4, 544.2, 11.0, 5.6],
            "pony": [432.7, 737.4, 47.5, 0.8, 2.5, 24.9, 312.4, 235.5, 211.5],
        },
    ),
    "sqlite": (
        "Yara ORM vs Tortoise, SQLAlchemy & Pony — SQLite, 5000 rows, median of 5",
        {
            "yara-orm": [7.7, 33.1, 3.3, 0.1, 0.5, 1.9, 47.7, 0.5, 0.4],
            "tortoise": [13.8, 26.6, 39.2, 0.3, 0.7, 20.2, 82.0, 0.5, 0.3],
            "sqlalchemy": [615.4, 245.0, 21.0, 0.7, 1.4, 7.6, 335.7, 1.9, 1.3],
            "pony": [51.5, 109.2, 53.1, 0.2, 1.5, 26.6, 31.2, 43.5, 37.0],
        },
    ),
}

# yara-orm in brand deep-purple, competitors in muted greys so the winner pops.
COLORS = {
    "yara-orm": "#7c4dff",
    "tortoise": "#9e9e9e",
    "sqlalchemy": "#bdbdbd",
    "pony": "#e0e0e0",
}
EDGE = "#5e35b1"

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"


def render(backend: str, title: str, times_ms: dict[str, list[float]]) -> Path:
    """Render one backend's grouped-bar latency chart.

    Args:
        backend: Backend name (used in the output file name).
        title: Chart title.
        times_ms: ORM name -> per-operation latency in milliseconds.

    Returns:
        The written PNG path.
    """
    orms = list(times_ms)
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
            times_ms[orm],
            width=bar_w,
            label=orm,
            color=COLORS[orm],
            edgecolor=EDGE if orm == "yara-orm" else "none",
            linewidth=1.2 if orm == "yara-orm" else 0,
            zorder=3,
        )

    ax.set_yscale("log")
    ax.set_ylabel("milliseconds (log scale — lower is better)")
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xticks(list(x))
    ax.set_xticklabels(OPERATIONS, rotation=20, ha="right")
    ax.legend(frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.18))
    ax.grid(axis="y", which="both", color="#eeeeee", zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    fig.tight_layout()
    out = ASSETS / f"benchmark-{backend}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def main() -> None:
    for backend, (title, times_ms) in BACKENDS.items():
        print(f"wrote {render(backend, title, times_ms)}")


if __name__ == "__main__":
    main()
