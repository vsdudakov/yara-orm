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
            "yara-orm": [15.2, 34.4, 3.6, 0.3, 0.8, 2.2, 64.1, 3.5, 0.7],
            "tortoise": [25.2, 84.1, 17.3, 0.5, 1.1, 9.1, 205.9, 3.9, 0.9],
            "sqlalchemy": [80.8, 158.7, 31.6, 1.0, 1.6, 8.4, 310.4, 4.1, 1.1],
            "pony": [223.8, 60.8, 34.4, 0.4, 2.3, 17.5, 86.0, 121.1, 95.5],
            "django": [40.5, 42.9, 9.1, 0.5, 1.0, 5.3, 121.1, 3.5, 0.8],
            "peewee": [51.1, 46.6, 11.9, 0.3, 0.8, 6.8, 115.6, 3.4, 0.7],
            "sqlobject": [513.3, 53.3, 27.8, 0.3, 0.6, 9.3, 23.1, 3.3, 0.6],
            "ormar": [227.5, 169.1, 56.6, 6.0, NA, 21.2, 336.9, 12.8, 2.2],
            "piccolo": [100.7, 92.3, 4.4, 0.4, 1.0, 2.6, 201.6, 3.6, 0.8],
        },
    ),
    "mysql": (
        "Yara ORM vs 7 Python ORMs — MySQL 8.4, 5000 rows, median of 5",
        {
            "yara-orm": [46.8, 638.7, 7.5, 0.5, 1.3, 3.2, 122.0, 6.8, 4.9],
            "tortoise": [48.2, 660.6, 33.8, 0.8, 1.4, 17.5, 226.1, 7.4, 4.8],
            "sqlalchemy": [596.1, 985.1, 44.3, 1.2, 2.0, 15.5, 524.2, 9.9, 5.6],
            "pony": [443.5, 800.6, 47.7, 0.9, 2.5, 24.6, 315.4, 232.4, 207.0],
            "django": [100.0, 783.4, 28.7, 0.9, 1.5, 15.1, 214.0, 7.7, 5.8],
            "peewee": [82.1, 743.5, 28.5, 0.8, 1.2, 14.9, 208.0, 9.5, 4.8],
            "sqlobject": [1076.6, 773.8, 42.0, 0.7, 1.0, 16.8, 64.6, 7.4, 6.6],
            "ormar": [212.3, 1062.2, 73.0, 4.2, NA, 31.3, 924.3, 11.7, 6.1],
        },
    ),
    "mariadb": (
        "Yara ORM vs 7 Python ORMs — MariaDB 11, 5000 rows, median of 5",
        {
            "yara-orm": [25.6, 311.9, 5.8, 0.4, 1.3, 3.3, 120.8, 3.8, 2.7],
            "tortoise": [38.4, 345.2, 36.1, 0.8, 1.3, 17.7, 224.8, 3.3, 2.8],
            "sqlalchemy": [101.2, 476.6, 43.2, 1.3, 2.1, 16.2, 541.4, 6.6, 3.2],
            "pony": [473.5, 403.7, 48.1, 0.8, 2.2, 24.8, 310.9, 264.7, 252.1],
            "django": [100.8, 296.3, 32.1, 0.9, 1.8, 15.4, 217.8, 4.5, 3.1],
            "peewee": [59.6, 299.0, 31.0, 0.7, 1.3, 14.6, 210.7, 4.3, 2.8],
            "sqlobject": [1247.3, 345.6, 42.7, 0.6, 0.9, 17.3, 64.8, 4.2, 3.0],
            "ormar": [208.4, 573.5, 71.6, 5.8, NA, 50.0, 914.8, 7.3, 3.7],
        },
    ),
    "sqlite": (
        "Yara ORM vs 8 Python ORMs — SQLite (sync_fast_path), 5000 rows, median of 5",
        {
            "yara-orm": [7.5, 14.6, 3.5, 0.03, 0.5, 2.0, 11.9, 0.5, 0.3],
            "tortoise": [14.1, 30.1, 39.7, 0.3, 0.7, 20.1, 86.0, 0.6, 0.4],
            "sqlalchemy": [612.6, 234.4, 29.4, 0.7, 1.4, 7.6, 332.9, 1.9, 1.2],
            "pony": [50.8, 111.7, 52.2, 0.2, 1.6, 25.7, 31.0, 42.8, 35.3],
            "django": [57.3, 124.8, 15.8, 0.2, 0.9, 8.6, 82.9, 1.2, 0.8],
            "peewee": [28.2, 106.9, 12.6, 0.1, 0.6, 7.0, 76.4, 1.2, 0.7],
            "sqlobject": [221.3, 132.1, 60.5, 0.1, 0.5, 17.7, 13.3, 1.2, 0.8],
            "ormar": [160.7, 315.5, 54.9, 1.6, NA, 42.7, 510.4, 1.7, 1.2],
            "piccolo": [75.7, 247.3, 9.1, 0.5, 1.0, 5.0, 368.3, 1.6, 1.1],
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
