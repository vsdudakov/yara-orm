---
title: Contributing
description: Contribute to Yara ORM — set up the Rust + Python dev environment, run the linters and the 100%-coverage test suite, and open a pull request.
---

# Contributing

Contributions are welcome — issues and pull requests alike. Yara ORM is a mixed
**Python + Rust** project, so the dev setup builds the native engine into a local
virtualenv.

## Prerequisites

- A [Rust toolchain](https://rustup.rs/) (`rustup`)
- Python 3.9 – 3.14
- A local **PostgreSQL** for the Postgres tests (the SQLite tests are self-contained)

## Set up

```bash
git clone https://github.com/vsdudakov/yara-orm
cd yara-orm

make dev        # create .venv313 and install dev tools (maturin, ruff, ty, pytest)
make build      # compile the Rust engine into the venv (maturin develop --release)
```

## Everyday commands

```bash
make lint       # ruff check + ruff format --check + ty
make test       # pytest against $DB (default postgres://localhost/orm_demo)
make cov        # tests with the 100% coverage gate
make bench      # 4-way benchmark (see benchmarks/)
```

!!! warning "Both must be green before a PR"
    Pull requests are expected to pass **`make lint`** (ruff + ty clean) and **`make cov`**
    (100% statement *and* branch coverage). CI runs the same checks on Python 3.12–3.14
    against both PostgreSQL and SQLite.

## Project layout

| Path | Contents |
|------|----------|
| `python/yara_orm/` | The Python ORM layer (models, fields, queryset, dialects, migrations). |
| `rust/src/` | The Rust engine (Engine, Backend trait, Pg/SQLite backends, Value). |
| `tests/` | End-to-end tests (mocks kept to a minimum). |
| `benchmarks/` | The 4-way benchmark script and methodology. |
| `docs/` | This documentation site (MkDocs Material). |

## Tests & coverage

Tests are end-to-end against a real database wherever possible. New behavior should come with
tests, and coverage must stay at **100%** (enforced via `fail_under = 100`, branch coverage
on). Prefer e2e tests; reach for mocks only when a path genuinely cannot be exercised
otherwise.

## Adding a backend

The backend abstraction is a two-seam extension point — a Rust `Backend` impl plus a Python
`BaseDialect` subclass. See [Architecture](architecture.md) and [Backends](backends/index.md).

## Working on the docs

```bash
pip install -e ".[docs]"   # the docs extra (MkDocs Material)
mkdocs serve               # live-preview at http://127.0.0.1:8000
```

The site is built and deployed to GitHub Pages automatically on every push to `main`.

## License

By contributing you agree that your contributions are licensed under the project's
[MIT License](https://github.com/vsdudakov/yara-orm/blob/main/LICENSE.md).
