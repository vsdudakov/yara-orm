"""Query annotators: attribution comments prepended to every statement.

Covers ``register_query_annotator`` / ``clear_query_annotators`` on all three
Python query paths (model / manual SQL / transaction), sanitisation of
malicious values, multi-annotator composition, the zero-cost executor gate,
and the ``generate_schemas`` extensions wiring.
"""

import pytest

from yara_orm import (
    Model,
    YaraOrm,
    clear_query_annotators,
    clear_query_hooks,
    connections,
    fields,
    in_transaction,
    register_query_annotator,
    register_query_hook,
)
from yara_orm.connection import (
    _EngineProxy,
    _sanitize_comment_value,
    get_dialect,
    get_executor,
)


class QaGadget(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "qa_gadget"


MODELS = [QaGadget]


@pytest.fixture
def seen_sql():
    """Collect the final SQL of every statement via a query hook.

    Hooks observe the statement text after annotation, so asserting on the
    collected strings proves the comment actually reached the engine call.
    Clears hooks and annotators afterwards so tests stay isolated.
    """
    seen: list[str] = []
    register_query_hook(lambda sql, params: seen.append(sql))
    yield seen
    clear_query_hooks()
    clear_query_annotators()


async def test_annotator_comment_reaches_all_query_paths(db, seen_sql):
    """
    GIVEN a registered query annotator returning an attribution string
    WHEN a model query, a manual query and an in-transaction query run
    THEN every executed statement carries the comment prefix and round-trips
    """
    register_query_annotator(lambda: "http_path=/api/calls,caller=list_calls")
    prefix = "/* http_path=/api/calls,caller=list_calls */ "

    await QaGadget.create(name="model-path")
    rows = await QaGadget.all()
    manual = await connections.get().execute_query_dict("SELECT 1 AS n")
    async with in_transaction() as conn:
        await QaGadget.create(name="tx-path")
        tx_rows = await conn.fetch_all("SELECT name FROM qa_gadget ORDER BY name")

    assert [g.name for g in rows] == ["model-path"]
    assert manual == [{"n": 1}]
    assert [r["name"] for r in tx_rows] == ["model-path", "tx-path"]
    assert seen_sql, "the query hook must have observed statements"
    assert all(sql.startswith(prefix) for sql in seen_sql)


async def test_malicious_value_cannot_break_out_of_the_comment(db, seen_sql):
    """
    GIVEN an annotator returning a value containing comment delimiters
    WHEN a statement runs
    THEN the delimiters are stripped, the payload stays inside one comment,
         and the statement still round-trips
    """
    register_query_annotator(lambda: "*/ ; DROP TABLE x; /*")

    rows = await connections.get().execute_query_dict("SELECT 1 AS n")

    assert rows == [{"n": 1}]
    assert seen_sql[-1] == "/* ; DROP TABLE x; */ SELECT 1 AS n"
    # Exactly one comment: the value cannot terminate it early.
    assert seen_sql[-1].count("*/") == 1


def test_sanitize_comment_value_strips_delimiters_and_control_chars():
    """
    GIVEN raw annotator values with delimiters, control characters or both
    WHEN they are sanitised
    THEN no ``*/`` / ``/*`` survives, even when a removal splices one together
    """
    assert _sanitize_comment_value("route=/api,caller=x") == "route=/api,caller=x"
    assert _sanitize_comment_value("a\nb\x00c\td") == "abcd"
    # Removing the control character splices ``*/`` together; the loop catches it.
    assert _sanitize_comment_value("*\x00/") == ""
    assert _sanitize_comment_value("*/*/") == ""
    assert _sanitize_comment_value("keep */ this /* too") == "keep  this  too"


async def test_multiple_annotators_join_non_empty_results_in_order(db, seen_sql):
    """
    GIVEN several annotators, some returning None/empty/delimiter-only values
    WHEN a statement runs
    THEN the non-empty sanitised results join with ``,`` into one comment,
         in registration order
    """
    register_query_annotator(lambda: "a=1")
    register_query_annotator(lambda: None)
    register_query_annotator(lambda: "")
    register_query_annotator(lambda: "*/")  # sanitises to empty: skipped
    register_query_annotator(lambda: "b=2")

    await connections.get().execute_query_dict("SELECT 1 AS n")

    assert seen_sql[-1] == "/* a=1,b=2 */ SELECT 1 AS n"


async def test_all_annotators_returning_nothing_leaves_sql_bare(db, seen_sql):
    """
    GIVEN registered annotators that all return None for this statement
    WHEN a statement runs
    THEN no comment is prepended (the SQL is unchanged)
    """
    register_query_annotator(lambda: None)

    await connections.get().execute_query_dict("SELECT 1 AS n")

    assert seen_sql[-1] == "SELECT 1 AS n"


async def test_annotator_exception_propagates_to_the_caller(db, seen_sql):
    """
    GIVEN an annotator that raises
    WHEN a model query runs
    THEN the exception propagates (matching query-hook behaviour)
    """

    def boom() -> str:
        raise ValueError("annotator failed")

    register_query_annotator(boom)

    with pytest.raises(ValueError, match="annotator failed"):
        await QaGadget.all()


async def test_executor_gate_is_zero_cost_until_an_annotator_registers(db):
    """
    GIVEN no hooks and no annotators
    WHEN the executor for a model is resolved
    THEN it is the raw engine; registering an annotator routes it through the
         proxy, and clearing restores the raw engine
    """
    assert not isinstance(get_executor(QaGadget), _EngineProxy)
    register_query_annotator(lambda: "route=/x")
    try:
        assert isinstance(get_executor(QaGadget), _EngineProxy)
    finally:
        clear_query_annotators()
    assert not isinstance(get_executor(QaGadget), _EngineProxy)


async def test_execute_script_paths_carry_the_comment(db, seen_sql):
    """
    GIVEN a registered annotator
    WHEN scripts run via the pooled proxy and inside a transaction
    THEN the annotated text is observed and the script round-trips; without
         annotators the script text is unchanged
    """
    script = (
        "INSERT INTO qa_gadget (name) VALUES ('s1'); INSERT INTO qa_gadget (name) VALUES ('s2')"
    )
    await connections.get().execute_script(script)
    assert seen_sql[-1] == script

    register_query_annotator(lambda: "job=nightly")
    await connections.get().execute_script(script)
    assert seen_sql[-1] == f"/* job=nightly */ {script}"

    tx_script = (
        "INSERT INTO qa_gadget (name) VALUES ('t1'); INSERT INTO qa_gadget (name) VALUES ('t2')"
    )
    async with in_transaction() as conn:
        await conn.execute_script(tx_script)
    # The transaction path splits and runs each statement via the choke point.
    assert "/* job=nightly */ INSERT INTO qa_gadget (name) VALUES ('t1')" in seen_sql
    assert "/* job=nightly */ INSERT INTO qa_gadget (name) VALUES ('t2')" in seen_sql
    assert await QaGadget.all().count() == 6


async def test_register_query_annotator_works_as_a_decorator(db, seen_sql):
    """
    GIVEN ``register_query_annotator`` used as a decorator
    WHEN the decorated function is defined
    THEN the function object is returned unchanged and is registered
    """

    @register_query_annotator
    def annotator() -> str | None:
        return "caller=decorated"

    assert annotator() == "caller=decorated"
    await connections.get().execute_query_dict("SELECT 1 AS n")
    assert seen_sql[-1] == "/* caller=decorated */ SELECT 1 AS n"


async def test_generate_schemas_runs_extension_statements_first(db, seen_sql, monkeypatch):
    """
    GIVEN a dialect exposing ``extensions_sql`` (stubbed)
    WHEN ``generate_schemas`` runs
    THEN the extension statements execute before any table creation, on the
         models' connection, and receive the module's models
    """
    received: list[list[type[Model]]] = []
    dialect = get_dialect(QaGadget)

    def fake_extensions_sql(self, models):
        received.append(list(models))
        if db == "mssql":
            # SQL Server has no CREATE TABLE IF NOT EXISTS.
            return [
                "IF OBJECT_ID(N'qa_ext_marker', 'U') IS NULL "
                "CREATE TABLE qa_ext_marker (id INTEGER)"
            ]
        return ["CREATE TABLE IF NOT EXISTS qa_ext_marker (id INTEGER)"]

    monkeypatch.setattr(type(dialect), "extensions_sql", fake_extensions_sql, raising=False)

    try:
        await YaraOrm.generate_schemas(models=MODELS)

        assert received == [[QaGadget]]
        assert "qa_ext_marker" in seen_sql[0], "extension statements must run first"
        assert any("qa_gadget" in sql for sql in seen_sql[1:])
    finally:
        await connections.get().execute("DROP TABLE IF EXISTS qa_ext_marker")


async def test_generate_schemas_without_extensions_is_unchanged(db, seen_sql, monkeypatch):
    """
    GIVEN a dialect with no ``extensions_sql`` capability
    WHEN ``generate_schemas`` runs
    THEN schema creation proceeds exactly as before and the table is usable
    """
    dialect = get_dialect(QaGadget)
    # Remove the capability from whichever class in the MRO defines it, so the
    # test forces the "no extensions_sql" branch even after the dialect API
    # grows the method.
    for klass in type(dialect).__mro__:
        if "extensions_sql" in vars(klass):
            monkeypatch.delattr(klass, "extensions_sql")
            break

    await YaraOrm.generate_schemas(models=MODELS)

    assert any("qa_gadget" in sql for sql in seen_sql)
    await QaGadget.create(name="still-works")
    assert await QaGadget.all().count() == 1
