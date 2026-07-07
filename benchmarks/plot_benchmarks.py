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

Writes ``docs/assets/benchmark-{postgres,mysql,mariadb,sqlite}.png``.
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

# NaN marks an operation an ORM doesn't support (drawn as no bar): Ormar has no
# GROUP BY/annotate API, so its ``group_by`` is NaN on every backend.
NA = float("nan")

#: Backend -> (chart title, ORM -> per-operation latency ms). Nine ORMs, measured
#: together in one run per backend (Piccolo has no MySQL backend, so it is absent
#: from the MySQL and MariaDB sets rather than shown as a zero-height bar).
BACKENDS: dict[str, tuple[str, dict[str, list[float]]]] = {
    "postgres": (
        "Yara ORM vs 8 Python ORMs — PostgreSQL 18, 5000 rows, median of 5",
        {
            "yara-orm": [14.7, 34.4, 3.6, 0.3, 0.7, 2.3, 65.1, 3.3, 0.7],
            "tortoise": [24.2, 80.7, 17.0, 0.6, 1.0, 9.1, 196.3, 3.6, 0.8],
            "sqlalchemy": [78.0, 153.1, 29.4, 1.0, 1.6, 8.1, 292.6, 4.0, 1.1],
            "pony": [222.8, 61.8, 34.5, 0.4, 2.4, 17.9, 85.3, 120.8, 94.3],
            "django": [40.6, 40.5, 9.1, 0.4, 1.0, 5.3, 115.7, 3.4, 0.8],
            "peewee": [51.7, 47.1, 11.9, 0.3, 0.8, 6.7, 114.1, 3.4, 0.7],
            "sqlobject": [526.3, 53.5, 26.6, 0.3, 0.6, 9.1, 23.8, 3.3, 0.6],
            "ormar": [229.8, 167.4, 56.7, 5.4, NA, 42.2, 333.1, 15.0, 2.4],
            "piccolo": [99.2, 89.5, 4.3, 0.4, 1.0, 2.6, 196.1, 3.5, 0.8],
        },
    ),
    "mysql": (
        "Yara ORM vs 7 Python ORMs — MySQL 8.4, 5000 rows, median of 5",
        {
            "yara-orm": [49.8, 605.4, 5.6, 0.5, 1.2, 3.3, 128.3, 7.0, 5.2],
            "tortoise": [50.9, 816.9, 33.4, 0.9, 1.4, 17.4, 226.7, 7.4, 4.8],
            "sqlalchemy": [600.9, 1058.2, 44.2, 1.2, 2.0, 15.8, 524.1, 8.2, 5.4],
            "pony": [443.8, 904.5, 48.4, 0.8, 2.5, 25.3, 312.5, 236.3, 210.0],
            "django": [89.2, 848.3, 29.0, 1.0, 1.5, 15.6, 211.7, 7.2, 6.4],
            "peewee": [88.7, 795.2, 28.0, 1.0, 1.2, 14.8, 206.2, 10.1, 5.0],
            "sqlobject": [1185.8, 875.4, 43.8, 0.8, 1.0, 17.1, 65.8, 6.9, 5.1],
            "ormar": [221.7, 1183.9, 73.3, 4.6, NA, 30.5, 925.0, 8.8, 7.2],
        },
    ),
    "mariadb": (
        "Yara ORM vs 7 Python ORMs — MariaDB 11, 5000 rows, median of 5",
        {
            "yara-orm": [23.3, 264.9, 5.7, 0.5, 1.2, 3.2, 132.9, 4.0, 3.0],
            "tortoise": [37.5, 311.6, 35.0, 0.8, 1.3, 17.5, 240.8, 4.4, 3.0],
            "sqlalchemy": [105.4, 531.7, 43.8, 1.2, 2.3, 16.3, 575.0, 7.8, 3.6],
            "pony": [475.4, 391.5, 48.1, 0.7, 2.2, 24.8, 306.7, 266.3, 249.5],
            "django": [96.4, 388.2, 28.1, 0.7, 1.5, 15.1, 220.7, 5.4, 3.6],
            "peewee": [71.8, 372.2, 37.0, 0.8, 1.4, 14.7, 206.5, 4.2, 3.0],
            "sqlobject": [1266.5, 390.3, 42.6, 0.6, 0.9, 17.1, 64.4, 3.7, 3.0],
            "ormar": [209.9, 660.0, 72.1, 6.7, NA, 32.6, 895.2, 8.2, 4.0],
        },
    ),
    "sqlite": (
        "Yara ORM vs 8 Python ORMs — SQLite (sync_fast_path), 5000 rows, median of 5",
        {
            "yara-orm": [7.5, 15.3, 3.4, 0.04, 0.6, 2.0, 12.5, 0.5, 0.3],
            "tortoise": [13.6, 26.9, 38.6, 0.2, 0.7, 20.2, 79.4, 0.5, 0.4],
            "sqlalchemy": [607.9, 234.5, 27.1, 0.7, 1.3, 7.3, 329.6, 1.7, 1.1],
            "pony": [50.1, 107.0, 51.0, 0.2, 1.4, 25.8, 31.3, 43.0, 35.9],
            "django": [55.8, 120.1, 16.4, 0.3, 0.9, 8.7, 84.6, 1.3, 0.8],
            "peewee": [29.0, 113.5, 12.2, 0.1, 0.6, 6.6, 75.5, 1.2, 0.7],
            "sqlobject": [218.3, 124.8, 44.2, 0.1, 0.5, 17.4, 13.3, 1.1, 0.7],
            "ormar": [143.8, 296.7, 52.0, 1.6, NA, 19.2, 484.1, 1.7, 1.1],
            "piccolo": [73.9, 240.4, 9.2, 0.5, 1.0, 5.0, 357.2, 1.5, 1.1],
        },
    ),
}
# yara-orm in brand deep-purple so the winner pops; competitors in a muted but
# distinguishable categorical palette (greys can't separate eight of them).
COLORS = {
    "yara-orm": "#7c4dff",
    "tortoise": "#ef9a9a",
    "sqlalchemy": "#90caf9",
    "pony": "#a5d6a7",
    "django": "#ffcc80",
    "peewee": "#80deea",
    "sqlobject": "#bcaaa4",
    "ormar": "#f48fb1",
    "piccolo": "#c5e1a5",
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

    fig, ax = plt.subplots(figsize=(13, 6), dpi=140)
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
    ax.legend(frameon=False, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.18))
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
