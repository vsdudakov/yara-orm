"""Reference-API parity fixes: transaction routing and keyword-only
aggregate ``distinct``; ``use_tz`` init, ``F`` in annotate, ``Subquery``/``RawSQL``
in filter, multi-level ``select_related``/``prefetch_related``; modern field
parameter aliases, extra lookups, multi-sender signals, per-model exceptions,
recorded ``Meta`` options, and ``construct``/``fetch_for_list``."""

import datetime as dt

import pytest

from yara_orm import (
    DoesNotExist,
    F,
    Model,
    MultipleObjectsReturned,
    RawSQL,
    Subquery,
    Sum,
    YaraOrm,
    fields,
    in_transaction,
    post_save,
)
from yara_orm.connection import _CONNECTIONS
from yara_orm.timezone import get_use_tz


class A3Country(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "a3_country"


class A3Author(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    country = fields.ForeignKeyField("A3Country", related_name="authors", null=True)

    class Meta:
        table = "a3_author"


class A3Tag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=20)

    class Meta:
        table = "a3_tag"


class A3Book(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    qty = fields.IntField(default=0)
    created = fields.DatetimeField(null=True)
    author = fields.ForeignKeyField("A3Author", related_name="books")
    editor = fields.ForeignKeyField("A3Author", related_name="edited", null=True)
    tags = fields.ManyToManyField(to="A3Tag", related_name="books", through="a3_book_tag")

    class Meta:
        table = "a3_book"


# Modern parameter spellings.
class A3Modern(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, db_index=True)
    alias = fields.CharField(max_length=20, source_field="alias_col", null=True)
    count = fields.IntField(db_default=0)
    owner = fields.ForeignKeyField(to="A3Country", related_name="moderns", null=True)

    class Meta:
        table = "a3_modern"


MODELS = [A3Country, A3Author, A3Tag, A3Book, A3Modern]


# Used standalone; its default_connection is set at runtime in the test so it
# does not poison `generate_schemas()` (all-models) in other test modules.
class A3Pinned(Model):
    id = fields.IntField(pk=True)
    v = fields.CharField(max_length=20)

    class Meta:
        table = "a3_pinned"


async def _seed():
    uk = await A3Country.create(name="UK")
    ada = await A3Author.create(name="Ada", country=uk)
    b1 = await A3Book.create(
        title="B1", qty=5, author=ada, created=dt.datetime(2024, 2, 1, tzinfo=dt.timezone.utc)
    )
    tag = await A3Tag.create(label="py")
    await b1.tags.add(tag)
    return uk, ada, b1, tag


# -- P0.2 aggregate distinct keyword-only -------------------------------------


def test_aggregate_distinct_is_keyword_only():
    """
    GIVEN an aggregate
    WHEN a second positional argument is passed
    THEN it raises rather than silently becoming ``distinct``
    """
    with pytest.raises(TypeError):
        Sum("qty", 0)
    assert Sum("qty", distinct=True).distinct is True


# -- P1.5 F in annotate -------------------------------------------------------


@pytest.mark.asyncio
async def test_f_in_annotate(db):
    """
    GIVEN rows
    WHEN F (and F arithmetic) is projected via annotate
    THEN the column expression is evaluated
    """
    await _seed()
    rows = await A3Book.all().annotate(x=F("qty"), y=F("qty") + 1).values("x", "y")
    assert rows[0] == {"x": 5, "y": 6}


# -- P1.6 Subquery / RawSQL in filter -----------------------------------------


@pytest.mark.asyncio
async def test_subquery_and_rawsql_in_filter(db):
    """
    GIVEN a subquery and a raw-SQL fragment
    WHEN used as filter values
    THEN they compile into the WHERE clause
    """
    _, ada, b1, _ = await _seed()
    sub = Subquery(A3Author.filter(name="Ada").only("id"))
    assert [b.title for b in await A3Book.filter(author=sub)] == ["B1"]
    assert [b.title for b in await A3Book.filter(id=RawSQL(str(b1.id)))] == ["B1"]


# -- P1.4 multi-level select_related / prefetch_related -----------------------


@pytest.mark.asyncio
async def test_multi_level_select_related(db):
    """
    GIVEN book -> author -> country
    WHEN select_related chains two hops
    THEN both related instances hydrate synchronously
    """
    await _seed()
    book = (await A3Book.all().select_related("author__country"))[0]
    assert book.author.name == "Ada"
    assert book.author.country.name == "UK"


@pytest.mark.asyncio
async def test_multi_level_prefetch_related(db):
    """
    GIVEN country -> authors -> books
    WHEN prefetch_related chains two hops
    THEN the deep relation is cached
    """
    await _seed()
    country = (await A3Country.all().prefetch_related("authors__books"))[0]
    authors = await country.authors
    titles = [b.title for a in authors for b in (await a.books)]
    assert titles == ["B1"]


# -- P2.7 modern field parameters ---------------------------------------------


def test_modern_field_parameters():
    """
    GIVEN modern parameter spellings
    WHEN a model is defined with them
    THEN they map onto the canonical field options
    """
    m = A3Modern._meta
    assert m.pk_field.model_field_name == "id" and m.pk_field.auto_increment  # primary_key
    assert m.get_field("name").index is True  # db_index
    assert m.get_field("alias").db_column == "alias_col"  # source_field (column name)
    assert m.get_field("count").default == 0  # db_default
    assert m.relations["owner"].field.reference == "A3Country"  # to=


def test_fk_and_m2m_require_a_target():
    """
    GIVEN a relation field with no target
    WHEN constructed
    THEN it raises a clear TypeError
    """
    with pytest.raises(TypeError):
        fields.ForeignKeyField()
    with pytest.raises(TypeError):
        fields.ManyToManyField()


@pytest.mark.asyncio
async def test_modern_params_roundtrip(db):
    """
    GIVEN a model declared with modern params
    WHEN a row is created and fetched
    THEN it persists correctly (the column name honors source_field)
    """
    row = await A3Modern.create(name="x", alias="a")
    assert (await A3Modern.get(id=row.id)).alias == "a"


# -- P2.10 extra lookups ------------------------------------------------------


@pytest.mark.asyncio
async def test_not_isnull_and_date_parts_and_date(db):
    """
    GIVEN rows with nullable and datetime columns
    WHEN using not_isnull / quarter / week / date lookups
    THEN they filter correctly
    """
    await _seed()
    await A3Author.create(name=None)
    assert [a.name for a in await A3Author.filter(name__not_isnull=True)] == ["Ada"]
    assert [b.title for b in await A3Book.filter(created__quarter=1)] == ["B1"]
    assert isinstance(await A3Book.filter(created__week=5).count(), int)
    assert [b.title for b in await A3Book.filter(created__date=dt.date(2024, 2, 1))] == ["B1"]


@pytest.mark.asyncio
async def test_regex_microsecond_unsupported_on_sqlite(db):
    """
    GIVEN the SQLite backend
    WHEN using posix_regex / microsecond lookups
    THEN UnSupportedError is raised
    """
    from yara_orm import UnSupportedError

    if db != "sqlite":
        pytest.skip("sqlite guard")
    await _seed()
    with pytest.raises(UnSupportedError):
        await A3Book.filter(title__posix_regex="^B").count()
    with pytest.raises(UnSupportedError):
        await A3Book.filter(created__microsecond=0).count()


# -- P2.9 multi-sender signals ------------------------------------------------


@pytest.mark.asyncio
async def test_multi_sender_signal(db):
    """
    GIVEN a handler registered for two models
    WHEN each is saved
    THEN the handler fires for both
    """
    fired = []

    @post_save(A3Author, A3Tag)
    async def handler(sender, instance, created, using_db, update_fields):
        fired.append(sender.__name__)

    await A3Author.create(name="z")
    await A3Tag.create(label="t")
    assert sorted(set(fired)) == ["A3Author", "A3Tag"]


# -- P2.11 per-model exceptions -----------------------------------------------


@pytest.mark.asyncio
async def test_per_model_exceptions(db):
    """
    GIVEN a model
    WHEN get() misses or returns many
    THEN the per-model exception (a subclass of the global one) is raised
    """
    assert issubclass(A3Author.DoesNotExist, DoesNotExist)
    assert issubclass(A3Author.MultipleObjectsReturned, MultipleObjectsReturned)
    assert A3Author.DoesNotExist is not A3Country.DoesNotExist
    await _seed()
    with pytest.raises(A3Author.DoesNotExist):
        await A3Author.get(name="missing")


# -- P2.12 Meta options -------------------------------------------------------


def test_meta_options_recorded():
    """
    GIVEN a model
    WHEN its Meta options are read
    THEN the previously-dropped options are recorded on _meta
    """
    m = A3Author._meta
    assert hasattr(m, "schema") and hasattr(m, "app")
    assert hasattr(m, "default_connection") and hasattr(m, "fetch_db_defaults")


# -- P2.14 construct / fetch_for_list -----------------------------------------


@pytest.mark.asyncio
async def test_construct_and_fetch_for_list(db):
    """
    GIVEN construct() and fetch_for_list()
    WHEN building a detached instance and prefetching across a list
    THEN they behave as expected
    """
    await _seed()
    detached = A3Author.construct(id=123, name="D")
    assert detached.name == "D" and detached._in_db is False
    detached_db = A3Author.construct(_from_db=True, id=1)
    assert detached_db._in_db is True

    books = await A3Book.all()
    await A3Book.fetch_for_list(books, "author")
    assert books[0].author.name == "Ada"
    assert await A3Book.fetch_for_list([]) == []


# -- coverage: edge branches in the new code ----------------------------------


@pytest.mark.asyncio
async def test_subquery_in_filter_in_clause(db):
    """
    GIVEN a subquery used with __in
    WHEN filtering
    THEN it renders as ``col IN (subquery)``
    """
    _, ada, _, _ = await _seed()
    sub = Subquery(A3Author.filter(name="Ada").only("id"))
    assert [b.title for b in await A3Book.filter(author_id__in=sub)] == ["B1"]


@pytest.mark.asyncio
async def test_select_related_shared_prefix_and_null_hop(db):
    """
    GIVEN select_related paths sharing a prefix, and a null intermediate FK
    WHEN executed
    THEN the prefix joins once and a null hop yields None grandchildren
    """
    uk = await A3Country.create(name="UK")
    ada = await A3Author.create(name="Ada", country=uk)
    # editor left null -> the editor__country hop has a null parent.
    await A3Book.create(title="B1", author=ada)
    books = await A3Book.all().select_related("author__country", "author", "editor__country")
    assert books[0].author.country.name == "UK"
    assert books[0].__dict__["_prefetch"]["editor"] is None


@pytest.mark.asyncio
async def test_multi_level_prefetch_forward_fk_and_empty(db):
    """
    GIVEN a forward-FK chain and a null first hop
    WHEN prefetched multi-level
    THEN the present chain loads and the null chain yields nothing
    """
    uk = await A3Country.create(name="UK")
    ada = await A3Author.create(name="Ada", country=uk)
    await A3Book.create(title="B1", author=ada)  # editor is null
    books = await A3Book.all().prefetch_related("author__country", "editor__country")
    assert books[0].author.country is not None  # forward-FK hop gathered + country loaded
    assert books[0].__dict__["_prefetch"].get("editor") is None  # null hop, no grandchildren


def test_m2m_to_alias_resolves():
    """
    GIVEN a ManyToManyField declared with to=
    WHEN the model is built
    THEN the relation target resolves
    """
    assert A3Book._meta.m2m["tags"].reference == "A3Tag"


def test_postgres_truncate_date_sql():
    """
    GIVEN the PostgreSQL dialect
    WHEN rendering the __date truncation
    THEN it casts to DATE
    """
    from yara_orm.dialects import PostgresDialect

    assert PostgresDialect().truncate_date_sql('"t"."c"') == 'CAST("t"."c" AS DATE)'


# -- P1.3 use_tz / timezone wired into init -----------------------------------


@pytest.mark.asyncio
async def test_use_tz_wired_into_init():
    """
    GIVEN init(use_tz=True, timezone=...)
    WHEN the ORM initialises
    THEN the timezone config reflects it (and resets on close)
    """
    assert get_use_tz() is False
    await YaraOrm.init("sqlite://:memory:", use_tz=True, timezone="Europe/London")
    try:
        from yara_orm.timezone import get_timezone, now

        assert get_use_tz() is True
        assert get_timezone() == "Europe/London"
        assert now().tzinfo is not None  # aware when use_tz is on
    finally:
        await YaraOrm.close()
    assert get_use_tz() is False  # reset on close


# -- P0.1 transaction connection routing + P2.12 default_connection routing ---


@pytest.mark.asyncio
async def test_transaction_targets_named_connection():
    """
    GIVEN a default and a named connection
    WHEN a transaction is opened on the named one
    THEN the write lands there, not in default
    """
    await YaraOrm.init("sqlite://:memory:")
    await YaraOrm.add_connection("second", "sqlite://:memory:")
    try:
        for name in ("default", "second"):
            await _CONNECTIONS[name][0].execute(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)", []
            )
        async with in_transaction("second") as txn:
            await txn.execute("INSERT INTO t (v) VALUES (?1)", ["x"])
        d = await _CONNECTIONS["default"][0].fetch_rows("SELECT v FROM t", [])
        s = await _CONNECTIONS["second"][0].fetch_rows("SELECT v FROM t", [])
        assert s and not d
    finally:
        await YaraOrm.close()


@pytest.mark.asyncio
async def test_default_connection_routes_model():
    """
    GIVEN a model with Meta.default_connection
    WHEN it is created and queried
    THEN its statements run on the named connection, not default
    """
    A3Pinned._meta.default_connection = "second"
    await YaraOrm.init("sqlite://:memory:")
    await YaraOrm.add_connection("second", "sqlite://:memory:")
    try:
        await YaraOrm.generate_schemas(models=[A3Pinned])  # routes to "second"
        await A3Pinned.create(v="x")
        assert await A3Pinned.all().count() == 1
        # The table only exists on "second"; default never saw it.
        rows = await _CONNECTIONS["second"][0].fetch_rows("SELECT v FROM a3_pinned", [])
        assert len(rows) == 1
    finally:
        A3Pinned._meta.default_connection = None
        await YaraOrm.close()
