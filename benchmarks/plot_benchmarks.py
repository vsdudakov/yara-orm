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
            "yara-orm": [14.7, 34.5, 3.5, 0.3, 0.8, 2.2, 65.3, 3.3, 0.7],
            "tortoise": [25.7, 81.2, 17.3, 0.6, 1.0, 9.1, 197.7, 3.5, 0.8],
            "sqlalchemy": [81.6, 156.1, 31.1, 1.0, 1.6, 8.7, 297.3, 4.1, 1.1],
            "pony": [222.9, 61.5, 35.3, 0.4, 2.3, 17.8, 85.1, 121.3, 95.1],
            "django": [39.7, 40.8, 9.2, 0.4, 1.1, 5.3, 114.9, 3.4, 0.8],
            "peewee": [53.2, 48.2, 12.3, 0.4, 0.9, 7.1, 115.5, 3.6, 0.7],
            "sqlobject": [523.6, 53.7, 26.5, 0.3, 0.6, 9.0, 23.5, 3.3, 0.6],
            "ormar": [236.4, 163.7, 55.6, 4.9, NA, 21.3, 329.1, 14.4, 2.4],
            "piccolo": [99.2, 88.8, 4.3, 0.4, 1.0, 2.6, 194.2, 3.6, 0.9],
        },
    ),
    "mysql": (
        "Yara ORM vs 7 Python ORMs — MySQL 8.4, 5000 rows, median of 5",
        {
            "yara-orm": [47.3, 627.2, 6.1, 0.5, 1.2, 3.3, 110.3, 6.3, 4.9],
            "tortoise": [53.9, 766.7, 34.0, 0.9, 1.4, 17.8, 212.5, 11.1, 5.5],
            "sqlalchemy": [568.7, 1043.6, 45.7, 1.2, 2.1, 15.8, 484.1, 10.4, 5.9],
            "pony": [403.1, 867.9, 48.8, 0.9, 2.6, 25.2, 275.9, 222.2, 192.4],
            "django": [91.3, 751.7, 29.1, 0.9, 1.5, 15.2, 200.9, 9.9, 5.4],
            "peewee": [83.3, 798.9, 28.0, 0.8, 1.2, 14.9, 195.8, 7.5, 4.8],
            "sqlobject": [974.9, 867.2, 42.9, 0.7, 1.0, 16.7, 58.7, 7.3, 4.9],
            "ormar": [236.3, 1367.5, 75.5, 5.3, NA, 31.0, 804.8, 10.9, 8.4],
        },
    ),
    "mariadb": (
        "Yara ORM vs 7 Python ORMs — MariaDB 11, 5000 rows, median of 5",
        {
            "yara-orm": [23.7, 315.0, 5.5, 0.4, 1.1, 3.5, 107.3, 3.5, 2.8],
            "tortoise": [37.6, 345.9, 34.5, 0.7, 1.3, 17.4, 211.6, 3.6, 2.8],
            "sqlalchemy": [102.2, 494.6, 27.7, 1.1, 2.0, 16.6, 482.3, 7.6, 3.2],
            "pony": [397.7, 388.8, 47.8, 0.7, 2.2, 24.4, 262.8, 232.3, 220.1],
            "django": [88.6, 367.1, 28.6, 0.8, 1.5, 15.0, 195.7, 3.9, 3.3],
            "peewee": [56.7, 284.5, 29.2, 0.9, 1.2, 15.5, 189.8, 3.8, 2.7],
            "sqlobject": [1086.1, 365.5, 42.7, 0.7, 0.9, 16.9, 57.2, 3.7, 2.8],
            "ormar": [209.5, 599.5, 71.3, 5.7, NA, 32.0, 808.3, 7.7, 3.5],
        },
    ),
    "sqlite": (
        "Yara ORM vs 8 Python ORMs — SQLite, 5000 rows, median of 5",
        {
            "yara-orm": [8.0, 32.6, 3.5, 0.1, 0.5, 2.0, 47.5, 0.5, 0.4],
            "tortoise": [14.2, 29.0, 39.6, 0.2, 0.7, 20.0, 85.9, 0.5, 0.3],
            "sqlalchemy": [612.4, 231.1, 28.9, 0.7, 1.4, 7.7, 330.0, 1.8, 1.2],
            "pony": [53.0, 118.6, 52.3, 0.2, 1.6, 26.7, 31.7, 43.5, 36.6],
            "django": [57.9, 130.4, 15.8, 0.2, 0.9, 8.5, 84.1, 1.3, 0.8],
            "peewee": [29.0, 121.0, 12.5, 0.2, 0.7, 6.8, 77.8, 1.2, 0.8],
            "sqlobject": [232.1, 131.3, 48.0, 0.1, 0.5, 17.6, 13.9, 1.0, 0.7],
            "ormar": [140.8, 306.8, 54.3, 1.7, NA, 20.1, 497.0, 1.7, 1.2],
            "piccolo": [77.2, 242.2, 9.1, 0.4, 1.0, 5.0, 357.2, 1.6, 1.1],
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
