.PHONY: build test example clean dev lint format

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

# 3-way benchmark vs Tortoise + Pony. Pony needs Python <= 3.12, so this target
# uses a separate 3.12 venv (.venv312). Run `make bench-setup` once first.
bench-setup:
	python3.12 -m venv .venv312
	.venv312/bin/pip install -U pip maturin tortoise-orm "sqlalchemy[asyncio]" asyncpg aiosqlite pony psycopg2-binary
	VIRTUAL_ENV=$(PWD)/.venv312 .venv312/bin/maturin develop --release

bench:
	ORM_TEST_DB=$(DB) .venv312/bin/python benchmarks/bench.py

# Same 4-way comparison on SQLite (each ORM gets its own file in /tmp).
bench-sqlite:
	.venv312/bin/pip install -q -U aiosqlite
	BENCH_BACKEND=sqlite .venv312/bin/python benchmarks/bench.py

clean:
	cargo clean
	rm -rf target build *.egg-info
