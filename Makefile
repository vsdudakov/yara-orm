.PHONY: build test example clean dev lint format docs docs-serve docs-install

VENV ?= .venv313
PY := $(VENV)/bin/python
DB ?= postgres://localhost/orm_demo
LINT_PATHS := python tests benchmarks examples

dev:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -U pip maturin pytest pytest-asyncio ruff ty

build:
	VIRTUAL_ENV=$(PWD)/$(VENV) $(VENV)/bin/maturin develop --release

# Blocking gate: ruff check + format + ty (matches the AGENTS lint convention).
lint:
	$(VENV)/bin/ruff check $(LINT_PATHS)
	$(VENV)/bin/ruff format --check $(LINT_PATHS)
	$(VENV)/bin/ty check python/yara_orm

format:
	$(VENV)/bin/ruff check --fix $(LINT_PATHS)
	$(VENV)/bin/ruff format $(LINT_PATHS)

test: build
	ORM_TEST_DB=$(DB) $(PY) -m pytest -q

cov: build
	ORM_TEST_DB=$(DB) $(PY) -m pytest -q --cov=yara_orm --cov-branch --cov-report=term-missing

example: build
	ORM_TEST_DB=$(DB) $(PY) examples/basic.py

# Cross-ORM benchmark vs eight other ORMs (Tortoise, SQLAlchemy, Pony, Django,
# Peewee, SQLObject, Ormar, Piccolo). Pony needs Python <= 3.12, so this target
# uses a separate 3.12 venv (.venv312). Run `make bench-setup` once first.
bench-setup:
	python3.12 -m venv .venv312
	.venv312/bin/pip install -U pip maturin tortoise-orm "sqlalchemy[asyncio]" asyncpg aiosqlite pony psycopg2-binary django peewee sqlobject ormar piccolo
	VIRTUAL_ENV=$(PWD)/.venv312 .venv312/bin/maturin develop --release

bench:
	ORM_TEST_DB=$(DB) .venv312/bin/python benchmarks/bench.py

# Same comparison on SQLite (each ORM gets its own file in /tmp).
bench-sqlite:
	.venv312/bin/pip install -q -U aiosqlite
	BENCH_BACKEND=sqlite .venv312/bin/python benchmarks/bench.py

# Same comparison on MySQL (ORM_TEST_MYSQL, default the local yara-mysql
# container). Missing competitor drivers are reported as `-`.
bench-mysql:
	.venv312/bin/pip install -q -U asyncmy aiomysql pymysql cryptography
	BENCH_BACKEND=mysql .venv312/bin/python benchmarks/bench.py

# Same comparison on MariaDB (ORM_TEST_MARIADB, default :3307). Every competitor
# reaches MariaDB through its MySQL driver; Piccolo has no MySQL backend (`-`).
bench-mariadb:
	.venv312/bin/pip install -q -U asyncmy aiomysql pymysql cryptography
	BENCH_BACKEND=mariadb .venv312/bin/python benchmarks/bench.py

# yara-orm-only feature micro-benchmarks (nested-transaction savepoints, eager
# loading vs N+1, projection). Runs on SQLite by default (zero setup); pass
# BENCH_BACKEND=postgres ORM_TEST_DB=... for Postgres.
bench-features: build
	$(PY) benchmarks/bench_features.py

# --- Documentation (MkDocs Material) -----------------------------------------
# Install the docs extra from pyproject.toml ([project.optional-dependencies]).
docs-install:
	$(VENV)/bin/pip install -U "mkdocs-material[imaging]>=9.5"

# Build the static site into ./site (social cards enabled, like CI).
docs:
	CI=true $(PY) -m mkdocs build --strict

# Live-preview at http://127.0.0.1:8000 (social cards off for speed).
docs-serve:
	$(PY) -m mkdocs serve

clean:
	cargo clean
	rm -rf target build *.egg-info site .cache
