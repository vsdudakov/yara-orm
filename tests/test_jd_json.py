"""Corner-case coverage for :class:`JSONField`.

Focuses on gaps around nested key-path lookups, ``__contains`` structural
containment (PostgreSQL only), the ``__filter`` dict, ``isnull`` on JSON paths,
arrays, unicode / special-character keys and values, empty / null containers,
encoder / decoder hooks and round-trips of scalar types plus in-place updates.

PostgreSQL-only containment (``@>``) is guarded with a SQLite skip so the whole
module stays green on SQLite.
"""

import json

import pytest

from yara_orm import Model, fields
from yara_orm.exceptions import UnSupportedError


class JdDoc(Model):
    id = fields.IntField(pk=True)
    data = fields.JSONField(null=True)

    class Meta:
        table = "jd_doc"


class JdEnc(Model):
    id = fields.IntField(pk=True)
    # Encoder returns a serialised JSON string; decoder is an identity value
    # transform (so ``read_identity`` is turned off and ``to_python`` runs).
    data = fields.JSONField(
        null=True,
        encoder=lambda v: json.dumps({"wrapped": v}),
        decoder=lambda v: v,
    )

    class Meta:
        table = "jd_enc"


MODELS = [JdDoc, JdEnc]


# ---------------------------------------------------------------------------
# Nested key-path lookups
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deep_nested_path_lookup(db):
    """
    GIVEN a JSON column with three levels of nesting
    WHEN filtered by a deep ``a__b__c`` key path
    THEN only the row whose addressed value matches returns
    """
    await JdDoc.create(data={"a": {"b": {"c": "deep"}}})
    await JdDoc.create(data={"a": {"b": {"c": "shallow"}}})

    rows = await JdDoc.filter(data__a__b__c="deep")
    assert len(rows) == 1
    assert rows[0].data["a"]["b"]["c"] == "deep"


@pytest.mark.asyncio
async def test_path_lookup_contains_operator(db):
    """
    GIVEN a JSON column holding a string leaf
    WHEN filtered by a key path with the text ``__contains`` (LIKE) operator
    THEN substring matching applies to the extracted text
    """
    await JdDoc.create(data={"status": "reopened"})
    await JdDoc.create(data={"status": "closed"})

    assert len(await JdDoc.filter(data__status__contains="open")) == 1


@pytest.mark.asyncio
async def test_path_lookup_missing_key_is_null(db):
    """
    GIVEN rows whose JSON lacks a given key
    WHEN filtered with ``path__isnull=True`` / ``False``
    THEN a missing key extracts as NULL (present -> False, absent -> True)
    """
    await JdDoc.create(data={"present": "yes"})
    await JdDoc.create(data={"other": "no"})

    assert await JdDoc.filter(data__present__isnull=True).count() == 1
    assert await JdDoc.filter(data__present__isnull=False).count() == 1
    # A key that no row has extracts NULL everywhere.
    assert await JdDoc.filter(data__nope__isnull=True).count() == 2


@pytest.mark.asyncio
async def test_path_lookup_in_operator(db):
    """
    GIVEN JSON rows with distinct string leaves
    WHEN filtered by a key path with ``__in``
    THEN rows whose extracted text is in the set return
    """
    await JdDoc.create(data={"kind": "a"})
    await JdDoc.create(data={"kind": "b"})
    await JdDoc.create(data={"kind": "c"})

    rows = await JdDoc.filter(data__kind__in=["a", "c"])
    assert sorted(r.data["kind"] for r in rows) == ["a", "c"]


@pytest.mark.asyncio
async def test_unicode_key_and_value_path_lookup(db):
    """
    GIVEN JSON with a non-ASCII key and value
    WHEN stored and filtered by that unicode key path
    THEN the unicode round-trips and the lookup matches
    """
    await JdDoc.create(data={"ключ": "значение", "emoji": "🎉"})

    stored = (await JdDoc.filter(data__ключ="значение"))[0]
    assert stored.data["ключ"] == "значение"
    assert stored.data["emoji"] == "🎉"


@pytest.mark.asyncio
async def test_special_char_value_roundtrip(db):
    """
    GIVEN JSON string values with quotes, backslashes and newlines
    WHEN stored and read back
    THEN the special characters survive serialisation intact
    """
    tricky = 'has "quotes", back\\slash and\nnewline'
    row = await JdDoc.create(data={"s": tricky})
    assert (await JdDoc.get(id=row.id)).data["s"] == tricky


# ---------------------------------------------------------------------------
# Scalar / container round-trips
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scalar_types_roundtrip_preserving_python_type(db):
    """
    GIVEN a JSON object mixing int, float, bool, None and nested containers
    WHEN saved and read back
    THEN each value keeps its Python type (int stays int, bool stays bool)
    """
    payload = {
        "i": 42,
        "f": 3.14,
        "t": True,
        "f2": False,
        "n": None,
        "nested": {"list": [1, 2, {"deep": True}]},
    }
    row = await JdDoc.create(data=payload)
    stored = (await JdDoc.get(id=row.id)).data

    assert stored["i"] == 42 and isinstance(stored["i"], int)
    assert stored["f"] == 3.14 and isinstance(stored["f"], float)
    assert stored["t"] is True and stored["f2"] is False
    assert stored["n"] is None
    assert stored["nested"]["list"][2]["deep"] is True


@pytest.mark.asyncio
async def test_non_finite_float_rejected_with_clear_error(db):
    """
    GIVEN a JSON document containing a non-finite float (NaN / Infinity), which
        has no JSON representation
    WHEN saved
    THEN the write is rejected with a clear ValueError instead of silently
         corrupting the stored shape (null before 1.14.2, a text "inf"/"NaN"
         in 1.14.2 — both change the member's type under readers and filters)
    """
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError, match="no JSON representation"):
            await JdDoc.create(data={"score": bad})
    # A finite float in the same shape still stores as a JSON number.
    row = await JdDoc.create(data={"score": 1.5})
    assert (await JdDoc.get(id=row.id)).data == {"score": 1.5}


@pytest.mark.asyncio
async def test_bool_not_confused_with_int(db):
    """
    GIVEN JSON booleans and integers side by side
    WHEN read back
    THEN booleans do not decay to 1/0 integers (type is preserved)
    """
    row = await JdDoc.create(data={"b": True, "i": 1})
    stored = (await JdDoc.get(id=row.id)).data
    assert stored["b"] is True
    assert isinstance(stored["i"], int) and not isinstance(stored["i"], bool)


@pytest.mark.asyncio
async def test_top_level_array_roundtrip(db):
    """
    GIVEN a JSON column whose top-level value is a list (not an object)
    WHEN saved and read back
    THEN the array round-trips with element order and types intact
    """
    row = await JdDoc.create(data=[1, "two", {"three": 3}, [4]])
    stored = (await JdDoc.get(id=row.id)).data
    assert stored == [1, "two", {"three": 3}, [4]]


@pytest.mark.asyncio
async def test_empty_dict_and_empty_list(db):
    """
    GIVEN empty container values
    WHEN an empty dict and an empty list are stored
    THEN each round-trips as an empty container (and stays distinct from NULL)
    """
    d = await JdDoc.create(data={})
    lst = await JdDoc.create(data=[])
    assert (await JdDoc.get(id=d.id)).data == {}
    assert (await JdDoc.get(id=lst.id)).data == []


@pytest.mark.asyncio
async def test_null_json_column(db):
    """
    GIVEN a nullable JSON column left as NULL
    WHEN the row is read back and filtered by isnull
    THEN the attribute is None and column-level isnull matches it
    """
    row = await JdDoc.create(data=None)
    assert (await JdDoc.get(id=row.id)).data is None
    assert await JdDoc.filter(data__isnull=True).count() == 1
    assert await JdDoc.filter(data__isnull=False).count() == 0


@pytest.mark.asyncio
async def test_json_null_literal_vs_sql_null(db):
    """
    GIVEN a stored JSON literal ``null`` inside an object versus a NULL column
    WHEN both are read back
    THEN the nested JSON null is preserved as Python None
    """
    row = await JdDoc.create(data={"maybe": None})
    assert (await JdDoc.get(id=row.id)).data == {"maybe": None}


# ---------------------------------------------------------------------------
# Updating a JSON column
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_update_json_via_save(db):
    """
    GIVEN a persisted JSON row
    WHEN the attribute is reassigned and ``save`` is called
    THEN the new document replaces the old one on disk
    """
    row = await JdDoc.create(data={"v": 1})
    row.data = {"v": 2, "extra": [1, 2]}
    await row.save()
    assert (await JdDoc.get(id=row.id)).data == {"v": 2, "extra": [1, 2]}


@pytest.mark.asyncio
async def test_update_json_via_queryset(db):
    """
    GIVEN a persisted JSON row
    WHEN ``QuerySet.update`` sets a new JSON value
    THEN the column is rewritten
    """
    row = await JdDoc.create(data={"v": 1})
    await JdDoc.filter(id=row.id).update(data={"v": 99})
    assert (await JdDoc.get(id=row.id)).data == {"v": 99}


@pytest.mark.asyncio
async def test_update_json_to_null(db):
    """
    GIVEN a persisted JSON row holding an object
    WHEN it is updated to NULL
    THEN the column becomes NULL
    """
    row = await JdDoc.create(data={"v": 1})
    await JdDoc.filter(id=row.id).update(data=None)
    assert (await JdDoc.get(id=row.id)).data is None


# ---------------------------------------------------------------------------
# Encoder / decoder hooks
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_encoder_string_result_parsed_and_decoder_runs(db):
    """
    GIVEN a JSONField whose encoder returns a JSON string
    WHEN a value is written and read back through the decoder
    THEN the string is parsed to native JSON (not stored verbatim) and wrapped
    """
    row = await JdEnc.create(data={"x": [1, 2]})
    assert (await JdEnc.get(id=row.id)).data == {"wrapped": {"x": [1, 2]}}


@pytest.mark.asyncio
async def test_encoder_decoder_pass_through_null(db):
    """
    GIVEN a JSONField with encoder/decoder hooks and a NULL value
    WHEN the row is saved and read back
    THEN None bypasses both hooks and stays None
    """
    row = await JdEnc.create(data=None)
    assert (await JdEnc.get(id=row.id)).data is None


# ---------------------------------------------------------------------------
# __filter dict (Tortoise-style ANDed JSON path conditions)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_json_filter_dict_ands_conditions(db):
    """
    GIVEN JSON rows with several keys
    WHEN filtered with a ``__filter`` dict of path->op conditions
    THEN only rows satisfying every condition return (ANDed)
    """
    await JdDoc.create(data={"status": "open", "name": "alpha"})
    await JdDoc.create(data={"status": "open", "name": "beta"})
    await JdDoc.create(data={"status": "done", "name": "alpha"})

    rows = await JdDoc.filter(data__filter={"status": "open", "name__contains": "alph"})
    assert len(rows) == 1
    assert rows[0].data["name"] == "alpha"


@pytest.mark.asyncio
async def test_json_filter_empty_dict_matches_all(db):
    """
    GIVEN JSON rows
    WHEN filtered with an empty ``__filter`` dict
    THEN every row matches (an empty filter is a no-op)
    """
    await JdDoc.create(data={"a": 1})
    await JdDoc.create(data={"a": 2})
    assert await JdDoc.filter(data__filter={}).count() == 2


@pytest.mark.asyncio
async def test_json_filter_with_not_op(db):
    """
    GIVEN JSON rows with a status leaf
    WHEN a ``__filter`` uses a ``__not`` inner op
    THEN rows whose extracted value differs return
    """
    await JdDoc.create(data={"status": "open"})
    await JdDoc.create(data={"status": "resolved"})

    rows = await JdDoc.filter(data__filter={"status__not": "resolved"})
    assert len(rows) == 1
    assert rows[0].data["status"] == "open"


# ---------------------------------------------------------------------------
# __contains structural containment (PostgreSQL only)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_contains_object_subset(db):
    """
    GIVEN JSON objects
    WHEN filtered with ``__contains`` for an object subset
    THEN rows whose object contains the given key/value pairs match
    """
    if db in ("sqlite", "mssql"):
        pytest.skip("SQLite/SQL Server have no JSON containment operator (@>)")
    await JdDoc.create(data={"a": 1, "b": 2, "c": 3})
    await JdDoc.create(data={"a": 1, "b": 9})

    rows = await JdDoc.filter(data__contains={"a": 1, "b": 2})
    assert len(rows) == 1
    assert rows[0].data["c"] == 3


@pytest.mark.asyncio
async def test_contains_array_element(db):
    """
    GIVEN JSON arrays
    WHEN filtered with ``__contains`` for an element subset
    THEN rows whose array contains all listed elements match
    """
    if db in ("sqlite", "mssql"):
        pytest.skip("SQLite/SQL Server have no JSON containment operator (@>)")
    await JdDoc.create(data=["x", "y", "z"])
    await JdDoc.create(data=["x"])

    rows = await JdDoc.filter(data__contains=["x", "y"])
    assert len(rows) == 1
    assert set(rows[0].data) == {"x", "y", "z"}


@pytest.mark.asyncio
async def test_contains_array_of_objects(db):
    """
    GIVEN a JSON array of objects
    WHEN filtered with ``__contains`` for a matching object element
    THEN rows containing that object (as a subset) match
    """
    if db in ("sqlite", "mssql"):
        pytest.skip("SQLite/SQL Server have no JSON containment operator (@>)")
    await JdDoc.create(data=[{"id": 1, "t": "a"}, {"id": 2, "t": "b"}])
    await JdDoc.create(data=[{"id": 3, "t": "c"}])

    rows = await JdDoc.filter(data__contains=[{"id": 2}])
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_contains_raises_on_sqlite(db):
    """
    GIVEN SQLite (no ``@>`` operator)
    WHEN a JSON ``__contains`` filter is compiled
    THEN it raises UnSupportedError rather than silently mis-filtering
    """
    if db != "sqlite":
        pytest.skip("PostgreSQL supports JSON containment")
    await JdDoc.create(data={"a": 1})
    with pytest.raises(UnSupportedError):
        await JdDoc.filter(data__contains={"a": 1})
