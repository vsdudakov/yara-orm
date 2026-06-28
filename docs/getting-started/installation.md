---
title: Installation
description: Install Yara ORM, the fast async Python ORM with a Rust engine. Prebuilt wheels for Linux, macOS and Windows on CPython 3.9–3.14 — no Rust toolchain needed.
---

# Installation

Yara ORM ships as a Python package with a compiled **Rust** engine. Prebuilt wheels mean a
normal `pip install` needs **no Rust toolchain** on supported platforms.

## Requirements

- **Python 3.9 – 3.14**
- A database: **PostgreSQL** or **SQLite** (SQLite ships with Python — nothing to install)

## Install from PyPI

```bash
pip install yara-orm
```

!!! note "Distribution vs import name"
    The PyPI **distribution** name is `yara-orm` (hyphen), but the **import** package is
    `yara_orm` (underscore):

    ```python
    from yara_orm import Model, YaraOrm, fields
    ```

Prebuilt wheels are published for:

| Platform | Architectures | Python |
|----------|---------------|--------|
| Linux (manylinux) | x86_64 | 3.9 – 3.14 |
| macOS | Apple Silicon (arm64) | 3.9 – 3.14 |
| Windows | x86_64 | 3.9 – 3.14 |

!!! tip "Other platforms"
    On a platform without a prebuilt wheel (for example an Intel Mac or a musl-based Linux),
    pip falls back to the **source distribution**, which compiles the Rust engine on install
    and therefore needs a [Rust toolchain](https://rustup.rs/). Everything else is identical.

## Verify the install

```python
import yara_orm
print(yara_orm.__version__)
```

## Optional: install with dev/test extras

To run the test suite or hack on Yara ORM:

```bash
pip install "yara-orm[dev]"     # pytest, pytest-asyncio, pytest-cov, ruff, ty
```

## Install from source

Building from a checkout needs a Rust toolchain and [maturin](https://www.maturin.rs/):

```bash
git clone https://github.com/vsdudakov/yara-orm
cd yara-orm
python -m venv .venv313 && source .venv313/bin/activate
pip install maturin
maturin develop --release          # compiles the Rust engine into the venv
```

See [Contributing](../contributing.md) for the full developer workflow.

## Next steps

- [Quick start](quickstart.md) — build your first app.
- [Models & fields](../guides/models-and-fields.md) — define your schema.
- [Backends](../backends/index.md) — PostgreSQL and SQLite connection URLs.
