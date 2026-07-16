# Custom fields

Downstream apps can teach yara-orm about column types it does not ship — a
pgvector `VectorField`, a PostGIS geometry, a trigram-indexed text — with one
public call: `register_field_kind()`. It wires the new *kind* into every layer
at once:

- **DDL** — the dialects render the column type from your SQL template (per
  dialect, so the same model works on PostgreSQL, MySQL and SQLite),
- **migrations** — `makemigrations` serialises the field as
  `fields.<ClassName>(...)` and diffs its `type_params` like any built-in
  (a changed parameter becomes an `AlterField`),
- **imports** — `fields.<ClassName>` resolves to your class, so generated
  migration files load without hand-editing,
- **extensions** — an optional `requires_extension` makes `generate_schemas`
  and generated migrations emit `CREATE EXTENSION IF NOT EXISTS ...` on
  PostgreSQL first.

## Declaring and registering a field

A custom field is a `Field` subclass with a unique `field_kind` and its SQL
type parameters in `type_params`:

```python
from yara_orm import Model, fields, register_field_kind


class VectorField(fields.Field):
    field_kind = "vector"

    def __init__(self, dim: int = 3, **kwargs):
        super().__init__(**kwargs)
        self.type_params = {"dim": dim}


register_field_kind(
    "vector",
    field_cls=VectorField,
    sql={"postgres": "vector({dim})", "mysql": "TEXT", "sqlite": "TEXT"},
    requires_extension="vector",  # pgvector; omit when none is needed
)


class Document(Model):
    id = fields.IntField(pk=True)
    embedding = VectorField(dim=1536)
```

`sql` is a `str.format` template filled from the field's `type_params` —
either one template for every dialect (`sql="vector({dim})"`) or a per-dialect
mapping. Rendering fails with a clear `ConfigurationError` when a template
placeholder has no matching type parameter, or when the active dialect has no
entry in the mapping.

!!! tip "Register at import time of your models module"
    Put the `register_field_kind(...)` call right next to the field class, at
    module level. Any process that imports your models — the app, the CLI,
    migration replay — then sees the registration. A migration file that
    additionally imports the field class (e.g. a custom `source` emitting
    `myapp.fields.VectorField(...)`) registers the kind as a side effect of
    that import, so replay works even without importing the models module.

### Rules

- The kind must not shadow a built-in kind (`int`, `varchar`, `json`, ...) —
  that raises `ConfigurationError`.
- `field_cls` must subclass `Field` and declare the matching `field_kind`.
- Re-registering the same kind with the same class is a no-op (import-time
  registration is naturally idempotent); a different class raises. Use
  `unregister_field_kind(kind)` (mainly for tests) to remove a registration.
- Class names must be unique across registrations: generated migrations
  reference the class as `fields.<ClassName>`.

## Migrations round-trip

`makemigrations` renders a custom field with its `type_params` as keyword
arguments plus the usual schema flags:

```python
m.CreateModelIfNotExists(
    "document",
    fields={
        "id": fields.IntField(pk=True),
        "embedding": fields.VectorField(dim=1536),
    },
),
```

The default renderer therefore requires the constructor to accept the
`type_params` keys as keyword arguments (as `VectorField(dim=...)` does). When
that shape doesn't fit, pass `source=` — a callable taking the field and
returning the constructor source string:

```python
register_field_kind(
    "vector",
    field_cls=VectorField,
    sql={"postgres": "vector({dim})", "mysql": "TEXT", "sqlite": "TEXT"},
    source=lambda f: f"fields.VectorField(dim={f.type_params['dim']})",
)
```

Diffing works like for built-ins: changing `dim=3` to `dim=4` produces a
single `m.AlterField(..., fields.VectorField(dim=4), old=fields.VectorField(dim=3))`,
and re-running `makemigrations` with no model changes writes nothing.

## Required PostgreSQL extensions

Declaring `requires_extension="vector"` on the kind makes the extension a
property of the schema:

- `YaraOrm.generate_schemas()` emits
  `CREATE EXTENSION IF NOT EXISTS "vector"` before creating tables (via
  `dialect.extensions_sql(models)`; empty on MySQL and SQLite).
- `makemigrations` prepends an `m.CreateExtension("vector")` operation to any
  migration that creates or retypes a column of the kind. The operation
  renders per dialect — the guarded `CREATE EXTENSION` on PostgreSQL, nothing
  on MySQL or SQLite — so one migration file applies cleanly on every backend. Its
  reverse is deliberately empty: other tables may rely on the extension.

The database role applying the schema needs the privilege to create the
extension (or the extension pre-installed by an administrator).

## Value conversion

DDL and migrations are what `register_field_kind` covers; Python-side value
conversion stays on the field class itself — override `to_db` (Python value →
bindable value), `to_python` (database value → Python value, set
`read_identity = False` so it runs on reads) and `to_python_value`
(assignment coercion), exactly like the built-in fields do.

### PostgreSQL custom column types: `RawText` + `select_as_text`

A custom *column type* (pgvector's `vector`, `inet`, `citext`, ...) needs two
extra declarations on PostgreSQL, because the engine's driver knows neither
the type's parameter encoding nor its binary result format:

- **Writes** — return the value's text rendering wrapped in
  `yara_orm.RawText` from `to_db`. A plain `str` binds with a declared `text`
  type, which PostgreSQL will not implicitly cast to the column's type
  (SQLSTATE 42804); `RawText` binds *untyped*, so the server infers the type
  from the target column and parses the text through the type's own input
  function. On every other backend `RawText` binds exactly like a plain
  string.
- **Reads** — set `select_as_text = True` on the field class. SELECT and
  RETURNING projections then read the column through `CAST(col AS text)` on
  PostgreSQL, and your `to_python` parses the text form (set
  `read_identity = False` alongside). Other backends ignore the flag — there
  the column already stores text.

The complete pgvector field:

```python
from yara_orm import RawText, fields, register_field_kind


class VectorField(fields.Field):
    field_kind = "vector"
    read_identity = False   # to_python runs on reads
    select_as_text = True   # PostgreSQL reads via CAST(col AS text)

    def __init__(self, dim: int = 3, **kwargs):
        super().__init__(**kwargs)
        self.type_params = {"dim": dim}

    def to_db(self, value):
        if isinstance(value, (list, tuple)):
            return RawText("[" + ",".join(map(str, value)) + "]")
        return value

    def to_python(self, value):
        if isinstance(value, str):
            return [float(x) for x in value.strip("[]").split(",") if x]
        return value
```

## See also

- [Models & fields](models-and-fields.md)
- [Migrations](migrations.md)
- [API reference](../api-reference.md)
