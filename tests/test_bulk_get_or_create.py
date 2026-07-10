"""Batch ``bulk_get_or_create`` / ``bulk_update_or_create``.

Existing rows are matched by a natural key in a single query; missing rows are
inserted with one ``bulk_create`` (and, for update-or-create, existing rows are
written back with one ``bulk_update``). A ``(instance, created)`` tuple is
returned per input record, in order.

Also covers the single-row ``get_or_create`` / ``update_or_create`` honouring
the query set's connection and filter constraints.
"""

import contextlib
import os
import tempfile

import pytest

from yara_orm import Count, F, Model, Q, YaraOrm, connections, fields


class BgItem(Model):
    id = fields.IntField(pk=True)
    sku = fields.CharField(max_length=20)
    name = fields.CharField(max_length=20)
    qty = fields.IntField(default=0)

    class Meta:
        table = "bg_item"


class BgPair(Model):
    id = fields.IntField(pk=True)
    a = fields.IntField()
    b = fields.IntField()
    label = fields.CharField(max_length=20)

    class Meta:
        table = "bg_pair"


class RfxAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "rfx_author"


class RfxBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    qty = fields.IntField(default=0)
    author = fields.ForeignKeyField("RfxAuthor", related_name="books")

    class Meta:
        table = "rfx_book"


class RfxItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    qty = fields.IntField(default=0)

    class Meta:
        table = "rfx_item"


MODELS = [BgItem, BgPair, RfxAuthor, RfxBook, RfxItem]


@pytest.mark.asyncio
async def test_bulk_get_or_create_creates_and_reuses(db):
    """
    GIVEN a mix of new and already-present natural keys
    WHEN bulk_get_or_create runs twice
    THEN new rows are created once, and a second run reuses them (created=False)
    """
    recs = [
        {"sku": "A", "name": "Apple", "qty": 1},
        {"sku": "B", "name": "Banana", "qty": 2},
    ]
    first = await BgItem.bulk_get_or_create(recs, key_fields=["sku"])
    assert [created for _, created in first] == [True, True]
    assert await BgItem.all().count() == 2

    # Second run: same keys plus one new key -> only the new one is created.
    recs2 = recs + [{"sku": "C", "name": "Cherry", "qty": 3}]
    second = await BgItem.bulk_get_or_create(recs2, key_fields=["sku"])
    assert [created for _, created in second] == [False, False, True]
    assert await BgItem.all().count() == 3
    # Reused instances carry the persisted primary keys.
    assert second[0][0].id == first[0][0].id


@pytest.mark.asyncio
async def test_bulk_get_or_create_defaults_and_in_batch_duplicates(db):
    """
    GIVEN defaults and a key repeated within one batch
    WHEN bulk_get_or_create runs
    THEN defaults apply only to created rows, and the repeat reuses one instance
    """
    recs = [
        {"sku": "X", "name": "First"},
        {"sku": "X", "name": "Dup"},  # same key within the batch
    ]
    out = await BgItem.bulk_get_or_create(recs, key_fields=["sku"], defaults={"qty": 9})
    assert [created for _, created in out] == [True, False]
    # Both results reference the single created row (created from the first record).
    assert out[0][0] is out[1][0]
    assert await BgItem.all().count() == 1
    row = await BgItem.get(sku="X")
    assert row.name == "First" and row.qty == 9  # default applied on create


@pytest.mark.asyncio
async def test_bulk_get_or_create_composite_key(db):
    """
    GIVEN a composite natural key spanning two columns
    WHEN bulk_get_or_create matches existing rows
    THEN only genuinely-new (a, b) pairs are created
    """
    await BgPair.bulk_get_or_create(
        [{"a": 1, "b": 1, "label": "one"}, {"a": 1, "b": 2, "label": "two"}],
        key_fields=["a", "b"],
    )
    out = await BgPair.bulk_get_or_create(
        [{"a": 1, "b": 1, "label": "dup"}, {"a": 1, "b": 3, "label": "new"}],
        key_fields=["a", "b"],
    )
    assert [created for _, created in out] == [False, True]
    assert await BgPair.all().count() == 3


@pytest.mark.asyncio
async def test_bulk_update_or_create_updates_existing_and_creates_missing(db):
    """
    GIVEN some pre-existing rows and some new ones
    WHEN bulk_update_or_create runs
    THEN existing rows are updated in place and missing rows are inserted
    """
    await BgItem.create(sku="A", name="old", qty=1)
    recs = [
        {"sku": "A", "name": "new", "qty": 10},  # exists -> update
        {"sku": "B", "name": "fresh", "qty": 20},  # missing -> create
    ]
    out = await BgItem.bulk_update_or_create(recs, key_fields=["sku"])
    assert [created for _, created in out] == [False, True]

    a = await BgItem.get(sku="A")
    b = await BgItem.get(sku="B")
    assert (a.name, a.qty) == ("new", 10)
    assert (b.name, b.qty) == ("fresh", 20)


@pytest.mark.asyncio
async def test_bulk_update_or_create_respects_update_fields(db):
    """
    GIVEN update_fields restricting which columns are written on existing rows
    WHEN bulk_update_or_create runs
    THEN only the listed fields change on the existing row
    """
    await BgItem.create(sku="A", name="keep", qty=1)
    out = await BgItem.bulk_update_or_create(
        [{"sku": "A", "name": "ignored", "qty": 99}],
        key_fields=["sku"],
        update_fields=["qty"],
    )
    assert out[0][1] is False
    a = await BgItem.get(sku="A")
    assert a.qty == 99  # updated
    assert a.name == "keep"  # not in update_fields -> unchanged


@pytest.mark.asyncio
async def test_bulk_get_or_create_all_existing_creates_nothing(db):
    """
    GIVEN a batch whose keys all already exist
    WHEN bulk_get_or_create runs
    THEN no INSERT happens and every result is created=False
    """
    await BgItem.create(sku="A", name="a", qty=1)
    await BgItem.create(sku="B", name="b", qty=2)
    out = await BgItem.bulk_get_or_create(
        [{"sku": "A", "name": "a"}, {"sku": "B", "name": "b"}], key_fields=["sku"]
    )
    assert [created for _, created in out] == [False, False]
    assert await BgItem.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_update_or_create_all_new_with_in_batch_duplicate(db):
    """
    GIVEN only new keys, one of them repeated within the batch
    WHEN bulk_update_or_create runs
    THEN the row is created once (no update pass), and the repeat reuses it
    """
    recs = [
        {"sku": "N", "name": "first", "qty": 1},
        {"sku": "N", "name": "dup", "qty": 2},  # same new key within the batch
        {"sku": "M", "name": "other", "qty": 3},
    ]
    out = await BgItem.bulk_update_or_create(recs, key_fields=["sku"])
    assert [created for _, created in out] == [True, False, True]
    assert out[0][0] is out[1][0]
    assert await BgItem.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_ops_empty_input_and_missing_key(db):
    """
    GIVEN empty input or no key fields
    WHEN the batch upsert helpers are called
    THEN empty input returns [] and a missing key raises ValueError
    """
    assert await BgItem.bulk_get_or_create([], key_fields=["sku"]) == []
    assert await BgItem.bulk_update_or_create([], key_fields=["sku"]) == []
    with pytest.raises(ValueError, match="key field"):
        await BgItem.bulk_get_or_create([{"sku": "A"}], key_fields=[])
    with pytest.raises(ValueError, match="key field"):
        await BgItem.bulk_update_or_create([{"sku": "A"}], key_fields=[])


# -- get_or_create / update_or_create: filter constraints reach the create ----


@pytest.mark.asyncio
async def test_related_manager_get_or_create_sets_fk(db):
    """
    GIVEN an author with no books
    WHEN get_or_create runs through the reverse-FK related manager
    THEN the created book carries the author FK, and a second call finds it
    """
    ada = await RfxAuthor.create(name="Ada")
    other = await RfxAuthor.create(name="Other")

    book, created = await ada.books.get_or_create(title="X")
    assert created is True
    assert book.author_id == ada.id

    again, created = await ada.books.get_or_create(title="X")
    assert created is False
    assert again.id == book.id
    assert await RfxBook.all().count() == 1

    # The row is scoped per author: the same title under another author creates
    # a second row bound to that author.
    theirs, created = await other.books.get_or_create(title="X")
    assert created is True
    assert theirs.author_id == other.id
    assert theirs.id != book.id


@pytest.mark.asyncio
async def test_related_manager_update_or_create_sets_fk(db):
    """
    GIVEN an author with no books
    WHEN update_or_create runs through the reverse-FK related manager
    THEN the created book carries the author FK; a second call updates in place
    """
    ada = await RfxAuthor.create(name="Ada")

    book, created = await ada.books.update_or_create(title="X", defaults={"qty": 1})
    assert created is True
    assert book.author_id == ada.id
    assert book.qty == 1

    updated, created = await ada.books.update_or_create(title="X", defaults={"qty": 5})
    assert created is False
    assert updated.id == book.id
    assert (await RfxBook.get(id=book.id)).qty == 5
    assert await RfxBook.all().count() == 1


@pytest.mark.asyncio
async def test_get_or_create_inherits_filter_values_and_kwargs_win(db):
    """
    GIVEN a query set filtered by simple equalities
    WHEN get_or_create misses and creates the row
    THEN the filter values are applied, with explicit kwargs/defaults winning
    """
    obj, created = await RfxItem.filter(qty=5).get_or_create(name="plain")
    assert created is True
    assert obj.qty == 5  # derived from the filter

    obj, created = await RfxItem.filter(qty=5).get_or_create(name="over", defaults={"qty": 9})
    assert created is True
    assert obj.qty == 9  # defaults beat the filter-derived value

    # A relation filter value (a model instance) reaches the create too.
    ada = await RfxAuthor.create(name="Ada")
    book, created = await RfxBook.filter(author=ada).get_or_create(title="Z")
    assert created is True
    assert book.author_id == ada.id


def test_filter_create_kwargs_skips_ambiguous_constraints():
    """
    GIVEN a filter tree mixing exact matches with lookups, OR and negation
    WHEN the create kwargs are derived from it
    THEN only the unambiguous root-level exact matches are kept
    """
    qs = (
        RfxItem.filter(qty__gte=3)  # lookup operator: skipped
        .filter(Q(name="a") | Q(name="b"))  # OR: skipped
        .exclude(name="c")  # negation: skipped
        .filter(name="x")  # root-level exact: kept
    )
    assert qs._filter_create_kwargs() == {"name": "x"}


def test_filter_create_kwargs_pk_alias_expressions_and_nested_q():
    """
    GIVEN root-level filters using pk, an expression value, an annotation
    name and AND-nested Q objects
    WHEN the create kwargs are derived from the filter tree
    THEN pk maps to the concrete pk field, nested AND branches contribute,
    and expression values / non-column names are skipped
    """
    qs = (
        RfxItem.filter(pk=11)  # pk aliases the concrete pk field: kept as id
        .filter(Q(Q(name="x"), Q(qty=7)))  # AND-nested children are walked
        .filter(name=F("name"))  # expression value: skipped (keeps "x")
        .filter(n=Count("id"))  # aggregate value on a non-column: skipped
        .filter(items=2)  # not a field or relation (lazy validation): skipped
    )
    assert qs._filter_create_kwargs() == {"id": 11, "name": "x", "qty": 7}


@pytest.mark.asyncio
async def test_get_or_create_uses_bound_connection():
    """
    GIVEN a query set bound to a second connection via using_db
    WHEN get_or_create misses and creates the row
    THEN the insert lands on the second connection, not the default one
    """
    paths = []
    for _ in range(2):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)
        paths.append(path)
    await YaraOrm.init(f"sqlite://{paths[0]}")
    await YaraOrm.add_connection("second", f"sqlite://{paths[1]}")
    try:
        await YaraOrm.generate_schemas(models=[RfxItem])
        await connections.get("second").execute(
            'CREATE TABLE "rfx_item" ('
            '"id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"name" VARCHAR(50) NOT NULL, "qty" INT NOT NULL)'
        )

        obj, created = await RfxItem.all().using_db("second").get_or_create(name="s")
        assert created is True
        assert await RfxItem.all().using_db("second").count() == 1
        assert await RfxItem.all().count() == 0  # nothing leaked to default

        again, created = await RfxItem.all().using_db("second").get_or_create(name="s")
        assert created is False
        assert again.id == obj.id

        # update_or_create's update half writes back to the same connection.
        updated, created = await (
            RfxItem.all().using_db("second").update_or_create(name="s", defaults={"qty": 3})
        )
        assert created is False
        assert (await RfxItem.all().using_db("second").get(name="s")).qty == 3
        assert await RfxItem.all().count() == 0
    finally:
        await YaraOrm.close()
        for path in paths:
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(path + suffix)
