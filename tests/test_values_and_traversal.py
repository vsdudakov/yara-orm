"""values()/values_list() corner cases, multi-level relation traversal in
values(), GROUP BY edge cases, and random ordering (order_by("?")).

These exercise the dict/tuple projection paths and the multi-hop forward-FK
join resolution that ``values()`` relies on, across both backends.
"""

import pytest

from yara_orm import Avg, Count, Model, Sum, fields


class VtCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "vt_country"


class VtPublisher(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    country = fields.ForeignKeyField("VtCountry", related_name="publishers", null=True)

    class Meta:
        table = "vt_publisher"


class VtAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    publisher = fields.ForeignKeyField("VtPublisher", related_name="authors", null=True)

    class Meta:
        table = "vt_author"


class VtTag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=20)

    class Meta:
        table = "vt_tag"


class VtBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    price = fields.IntField(default=0)
    author = fields.ForeignKeyField("VtAuthor", related_name="books")
    tags = fields.ManyToManyField("VtTag", related_name="books", through="vt_book_tag")

    class Meta:
        table = "vt_book"


class VtLedger(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    balance = fields.DecimalField(max_digits=12, decimal_places=2)
    # Nullable decoded column: a NULL value must skip the read decoder.
    reserve = fields.DecimalField(max_digits=12, decimal_places=2, null=True)

    class Meta:
        table = "vt_ledger"


class VtEntry(Model):
    id = fields.IntField(pk=True)
    amount = fields.DecimalField(max_digits=12, decimal_places=2)
    ledger = fields.ForeignKeyField("VtLedger", related_name="entries")

    class Meta:
        table = "vt_entry"


class VtDoc(Model):
    # A non-int (uuid) primary key: on MySQL/Oracle/SQL Server it is stored as
    # text and needs a read decoder, so projecting a relation to it exercises
    # the pk decode path.
    ref = fields.UUIDField(pk=True)
    title = fields.CharField(max_length=20)

    class Meta:
        table = "vt_doc"


class VtNote(Model):
    id = fields.IntField(pk=True)
    doc = fields.ForeignKeyField("VtDoc", related_name="notes")

    class Meta:
        table = "vt_note"


MODELS = [VtCountry, VtPublisher, VtTag, VtAuthor, VtBook, VtLedger, VtEntry, VtDoc, VtNote]


async def _seed():
    uk = await VtCountry.create(name="UK")
    us = await VtCountry.create(name="US")
    pub_uk = await VtPublisher.create(name="PubUK", country=uk)
    pub_us = await VtPublisher.create(name="PubUS", country=us)
    ada = await VtAuthor.create(name="Ada", publisher=pub_uk)
    bob = await VtAuthor.create(name="Bob", publisher=pub_us)
    await VtBook.create(title="A1", price=10, author=ada)
    await VtBook.create(title="A2", price=30, author=ada)
    await VtBook.create(title="B1", price=20, author=bob)
    return {"ada": ada, "bob": bob}


@pytest.mark.asyncio
async def test_values_plain_and_subset(db):
    """
    GIVEN seeded books
    WHEN selecting values() with and without an explicit field subset
    THEN each row is a dict keyed by the requested (or all) field names
    """
    await _seed()
    rows = await VtBook.all().order_by("title").values("title", "price")
    assert rows == [
        {"title": "A1", "price": 10},
        {"title": "A2", "price": 30},
        {"title": "B1", "price": 20},
    ]
    # No args -> every model field is present as a key.
    full = await VtBook.filter(title="A1").values()
    assert set(full[0]) == {"id", "title", "price", "author_id"}


@pytest.mark.asyncio
async def test_values_single_level_traversal_dict(db):
    """
    GIVEN books linked to authors
    WHEN values() traverses one forward relation (author__name)
    THEN the dict carries the traversed value under the dotted path key
    """
    await _seed()
    rows = await VtBook.all().order_by("title").values("title", "author__name")
    assert rows == [
        {"title": "A1", "author__name": "Ada"},
        {"title": "A2", "author__name": "Ada"},
        {"title": "B1", "author__name": "Bob"},
    ]


@pytest.mark.asyncio
async def test_values_two_level_traversal_dict(db):
    """
    GIVEN a book -> author -> publisher -> country chain
    WHEN values() traverses two/three forward relations
    THEN the deepest related column is projected into the dict
    """
    await _seed()
    rows = (
        await VtBook.all()
        .order_by("title")
        .values("title", "author__publisher__name", "author__publisher__country__name")
    )
    assert rows == [
        {
            "title": "A1",
            "author__publisher__name": "PubUK",
            "author__publisher__country__name": "UK",
        },
        {
            "title": "A2",
            "author__publisher__name": "PubUK",
            "author__publisher__country__name": "UK",
        },
        {
            "title": "B1",
            "author__publisher__name": "PubUS",
            "author__publisher__country__name": "US",
        },
    ]


@pytest.mark.asyncio
async def test_values_traversal_alias(db):
    """
    GIVEN a multi-hop relation path
    WHEN it is aliased via a keyword in values()
    THEN the dict uses the clean alias key instead of the dotted path
    """
    await _seed()
    rows = await VtBook.filter(title="A1").values(
        "title", country="author__publisher__country__name"
    )
    assert rows == [{"title": "A1", "country": "UK"}]


@pytest.mark.asyncio
async def test_values_list_tuple_and_flat(db):
    """
    GIVEN seeded books
    WHEN reading values_list() with multiple columns and with flat=True
    THEN tuples are returned for multiple columns and scalars for flat
    """
    await _seed()
    pairs = await VtBook.all().order_by("title").values_list("title", "author__name")
    assert pairs == [("A1", "Ada"), ("A2", "Ada"), ("B1", "Bob")]
    flat = await VtBook.all().order_by("title").values_list("title", flat=True)
    assert flat == ["A1", "A2", "B1"]


@pytest.mark.asyncio
async def test_group_by_aggregate_dict(db):
    """
    GIVEN books grouped by author
    WHEN aggregating counts/sums/averages per group
    THEN one dict per group carries the aggregate values
    """
    seed = await _seed()
    rows = (
        await VtBook.annotate(n=Count("id"), total=Sum("price"), avg=Avg("price"))
        .group_by("author_id")
        .order_by("author_id")
        .values("author_id", "n", "total", "avg")
    )
    by_author = {r["author_id"]: r for r in rows}
    assert by_author[seed["ada"].id]["n"] == 2
    assert by_author[seed["ada"].id]["total"] == 40
    assert by_author[seed["bob"].id]["n"] == 1
    assert by_author[seed["bob"].id]["total"] == 20


@pytest.mark.asyncio
async def test_group_by_having_filters_groups(db):
    """
    GIVEN books grouped by author
    WHEN a HAVING filter is applied on the aggregate
    THEN only groups satisfying the aggregate predicate remain
    """
    seed = await _seed()
    rows = (
        await VtBook.annotate(total=Sum("price"))
        .group_by("author_id")
        .filter(total__gte=30)
        .values("author_id", "total")
    )
    assert rows == [{"author_id": seed["ada"].id, "total": 40}]


@pytest.mark.asyncio
async def test_group_by_having_range_on_aggregate(db):
    """
    GIVEN books grouped by author
    WHEN a HAVING uses a special lookup (range) on the aggregate
    THEN the range predicate is honoured (regression: HAVING special ops)
    """
    await _seed()
    rows = (
        await VtBook.annotate(n=Count("id"))
        .group_by("author_id")
        .filter(n__range=(2, 5))
        .values("author_id", "n")
    )
    assert [r["n"] for r in rows] == [2]


@pytest.mark.asyncio
async def test_values_relation_name_returns_pk(db):
    """
    GIVEN books linked to authors
    WHEN values() names a forward relation directly (not a column path)
    THEN the relation's primary key is projected
    """
    seed = await _seed()
    rows = await VtBook.all().order_by("title").values("title", "author")
    assert rows[0] == {"title": "A1", "author": seed["ada"].id}


@pytest.mark.asyncio
async def test_values_path_ending_on_relation(db):
    """
    GIVEN a book -> author -> publisher chain
    WHEN a values() path ends on a relation (not a scalar column)
    THEN that relation's primary key is projected
    """
    await _seed()
    rows = await VtBook.filter(title="A1").values("title", "author__publisher")
    # author__publisher resolves to the publisher's primary key.
    assert rows[0]["title"] == "A1"
    assert isinstance(rows[0]["author__publisher"], int)


@pytest.mark.asyncio
async def test_values_m2m_traversal(db):
    """
    GIVEN books tagged via a many-to-many relation
    WHEN values() traverses the m2m relation (tags__label)
    THEN the related column is projected per join row
    """
    seed = await _seed()
    tag = await VtTag.create(label="sci-fi")
    book = await VtBook.get(title="A1")
    await book.tags.add(tag)
    rows = await VtBook.filter(id=book.id).values("title", "tags__label")
    assert rows == [{"title": "A1", "tags__label": "sci-fi"}]
    assert seed  # silence unused


@pytest.mark.asyncio
async def test_values_invalid_deep_relation_path_raises(db):
    """
    GIVEN an m2m relation segment in the middle of a path
    WHEN values() is asked to traverse it as a multi-hop forward chain
    THEN a FieldError is raised (only forward relations chain)
    """
    from yara_orm.exceptions import FieldError

    await _seed()
    with pytest.raises(FieldError):
        await VtBook.all().values("tags__book__title")


@pytest.mark.asyncio
async def test_values_decode_field_types(db):
    """
    GIVEN a model with a DecimalField
    WHEN reading it through values()/values_list() (tuple and flat)
    THEN each value is decoded to the field's Python type (``Decimal``),
        matching instance-attribute hydration rather than the raw DB string
        SQLite returns (regression: values() skipped read decoders).
    """
    from decimal import Decimal

    await VtLedger.create(name="Main", balance=Decimal("100.50"))

    row = (await VtLedger.all().values("name", "balance"))[0]
    assert row["balance"] == Decimal("100.50")
    assert isinstance(row["balance"], Decimal)

    pair = (await VtLedger.all().values_list("name", "balance"))[0]
    assert pair[1] == Decimal("100.50")
    assert isinstance(pair[1], Decimal)

    flat = await VtLedger.all().values_list("balance", flat=True)
    assert flat == [Decimal("100.50")]
    assert isinstance(flat[0], Decimal)


@pytest.mark.asyncio
async def test_values_traversed_field_decoded(db):
    """
    GIVEN an entry whose forward relation carries a DecimalField
    WHEN values() traverses to that related column (ledger__balance)
    THEN the traversed value is decoded to ``Decimal`` too (the decode path
        resolves the terminal field across the relation hop)
    """
    from decimal import Decimal

    ledger = await VtLedger.create(name="Main", balance=Decimal("100.50"))
    await VtEntry.create(amount=Decimal("12.30"), ledger=ledger)

    row = (await VtEntry.all().values("amount", "ledger__balance"))[0]
    assert row["amount"] == Decimal("12.30")
    assert isinstance(row["amount"], Decimal)
    assert row["ledger__balance"] == Decimal("100.50")
    assert isinstance(row["ledger__balance"], Decimal)


@pytest.mark.asyncio
async def test_order_by_random(db):
    """
    GIVEN a set of rows
    WHEN ordering by the random token "?"
    THEN every row is still returned exactly once (RANDOM() ordering)
    """
    await _seed()
    titles = await VtBook.all().order_by("?").values_list("title", flat=True)
    assert sorted(titles) == ["A1", "A2", "B1"]


@pytest.mark.asyncio
async def test_values_annotate_decodes_field_columns(db):
    """
    GIVEN a ledger with entries and a DecimalField balance
    WHEN annotate()/group_by() + values() projects that field
    THEN the field column is decoded to Decimal, not the raw driver value

    Regression: the grouped/annotated projection path skipped read decoders,
    so annotate(...).values("balance") returned a raw str on SQLite.
    """
    from decimal import Decimal

    ledger = await VtLedger.create(name="Main", balance=Decimal("100.50"))
    await VtEntry.create(amount=Decimal("12.30"), ledger=ledger)
    await VtEntry.create(amount=Decimal("7.70"), ledger=ledger)

    # annotate(...).values(field) — including a NULL nullable decoded column,
    # which must skip the decoder rather than fail.
    row = (await VtLedger.annotate(n=Count("entries")).values("balance", "reserve", "n"))[0]
    assert row["balance"] == Decimal("100.50")
    assert isinstance(row["balance"], Decimal)
    assert row["reserve"] is None
    assert row["n"] == 2  # the aggregate is left untouched

    # group_by(field).annotate(agg).values(...)
    grouped = await (
        VtLedger.all()
        .group_by("balance")
        .annotate(total=Sum("entries__amount"))
        .values("balance", "total")
    )
    assert isinstance(grouped[0]["balance"], Decimal)
    assert grouped[0]["balance"] == Decimal("100.50")


@pytest.mark.asyncio
async def test_values_relation_pk_decoded_consistently(db):
    """
    GIVEN a note whose forward relation has a non-int (uuid) primary key
    WHEN values() projects the bare relation, its ``__pk`` alias, and the id col
    THEN all three return the same decoded uuid.UUID (not a raw str)

    Regression: values("doc__pk") and the bare relation skipped the read decoder
    on backends that store uuid as text (MySQL/Oracle/SQL Server), so they
    diverged from the hydrated attribute type.
    """
    import uuid

    doc = await VtDoc.create(ref=uuid.uuid4(), title="D1")
    await VtNote.create(doc=doc)

    bare = (await VtNote.all().values_list("doc", flat=True))[0]
    via_pk = (await VtNote.all().values_list("doc__pk", flat=True))[0]
    via_col = (await VtNote.all().values_list("doc_id", flat=True))[0]
    assert bare == via_pk == via_col == doc.ref
    assert isinstance(bare, uuid.UUID)
    assert isinstance(via_pk, uuid.UUID)
