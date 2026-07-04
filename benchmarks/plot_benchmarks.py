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

# NaN marks an operation an ORM doesn't support (drawn as no bar): Ormar has no
# GROUP BY/annotate API, so its ``group_by`` is NaN on every backend.
NA = float("nan")

#: Backend -> (chart title, ORM -> per-operation latency ms). Nine ORMs, measured
#: together in one run per backend (Piccolo has no MySQL backend, so it is absent
#: from the MySQL set rather than shown as a zero-height bar).
BACKENDS: dict[str, tuple[str, dict[str, list[float]]]] = {
    "postgres": (
        "Yara ORM vs 8 Python ORMs — PostgreSQL 18, 5000 rows, median of 5",
        {
            "yara-orm": [15.5, 35.5, 3.7, 0.3, 0.8, 2.3, 64.0, 3.7, 0.9],
            "tortoise": [26.4, 79.5, 17.5, 0.6, 1.2, 9.4, 198.1, 3.7, 0.9],
            "sqlalchemy": [100.5, 299.3, 36.0, 1.9, 3.2, 12.0, 589.1, 7.8, 1.9],
            "pony": [411.6, 109.6, 41.5, 0.7, 3.7, 20.5, 136.5, 204.1, 148.3],
            "django": [56.6, 67.2, 12.4, 0.9, 2.4, 6.8, 189.2, 9.1, 1.8],
            "peewee": [83.9, 75.5, 14.4, 0.8, 1.5, 9.9, 175.8, 7.9, 1.4],
            "sqlobject": [1045.8, 171.0, 70.3, 0.8, 1.3, 14.2, 54.7, 8.4, 1.6],
            "ormar": [260.7, 273.8, 65.8, 3.7, NA, 51.1, 512.5, 9.1, 2.2],
            "piccolo": [119.9, 186.2, 5.9, 1.0, 2.2, 2.8, 347.8, 8.0, 1.6],
        },
    ),
    "mysql": (
        "Yara ORM vs 7 Python ORMs — MySQL 8.4, 5000 rows, median of 5",
        {
            "yara-orm": [53.8, 715.8, 11.5, 0.8, 1.4, 3.6, 106.6, 7.0, 5.0],
            "tortoise": [51.5, 795.4, 33.8, 0.8, 1.4, 17.7, 212.9, 9.8, 5.6],
            "sqlalchemy": [640.5, 1214.8, 45.5, 1.2, 2.1, 16.1, 479.6, 10.1, 5.5],
            "pony": [402.5, 883.9, 49.2, 0.8, 2.5, 25.0, 275.5, 221.9, 192.0],
            "django": [93.3, 834.8, 30.0, 0.9, 1.4, 15.8, 209.6, 7.0, 6.1],
            "peewee": [85.6, 840.3, 28.0, 0.8, 1.1, 15.0, 194.9, 7.4, 5.7],
            "sqlobject": [1058.7, 879.0, 43.8, 0.7, 1.1, 17.0, 59.8, 7.0, 5.3],
            "ormar": [193.7, 1091.1, 74.7, 3.9, NA, 30.6, 855.5, 9.1, 6.0],
        },
    ),
    "sqlite": (
        "Yara ORM vs 8 Python ORMs — SQLite, 5000 rows, median of 5",
        {
            "yara-orm": [8.0, 36.1, 3.5, 0.1, 0.6, 2.0, 57.2, 0.6, 0.5],
            "tortoise": [15.5, 44.1, 43.3, 0.4, 0.8, 21.6, 103.8, 0.8, 0.5],
            "sqlalchemy": [660.8, 397.7, 32.0, 0.8, 1.5, 8.2, 373.8, 2.0, 1.4],
            "pony": [56.1, 155.1, 56.4, 0.2, 1.6, 29.7, 35.5, 50.0, 39.7],
            "django": [66.9, 162.8, 16.9, 0.2, 0.9, 8.9, 90.9, 1.5, 1.0],
            "peewee": [34.0, 144.0, 13.4, 0.2, 0.7, 7.1, 92.3, 1.2, 0.8],
            "sqlobject": [234.8, 139.4, 46.5, 0.1, 0.5, 17.9, 13.9, 1.0, 0.7],
            "ormar": [158.9, 339.0, 53.1, 1.6, NA, 19.1, 506.9, 1.7, 1.1],
            "piccolo": [75.9, 264.7, 9.2, 0.4, 1.0, 5.0, 365.9, 1.5, 1.2],
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
