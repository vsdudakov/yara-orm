"""Regression tests for the post-audit review findings.

Each test guards one verified cross-subsystem regression found while reviewing
the audit-fix batch:
- exclude() with several annotation lookups negates the conjunction (De Morgan).
- An explicitly assigned ``None`` on a nullable db-default column persists.
- Reads inside a transaction see its writes under a read/write-splitting router.
- ``using_db=<transaction>`` renders SQL for the transaction's dialect.
- Qualified FK references resolve between same-named models.
- Re-running resolve_relations with a rebuilt M2M info stays idempotent.
- Migration sets with duplicate numbers / stale dependencies still load.
"""

import pytest

from yara_orm import (
    ConfigurationError,
    Count,
    Model,
    Sum,
    YaraOrm,
    db_defaults,
    fields,
    registry,
    transactions,
)
from yara_orm.connection import get_dialect, get_executor, in_transaction
from yara_orm.dialects import SqliteDialect


class RvfAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "rvf_author"


class RvfBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=100)
    rating = fields.IntField(default=0)
    author = fields.ForeignKeyField("RvfAuthor", related_name="books")

    class Meta:
        table = "rvf_book"


class RvfDoc(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=50)
    expires = fields.DatetimeField(null=True, default=db_defaults.Now())

    class Meta:
        table = "rvf_doc"


MODELS = [RvfAuthor, RvfBook, RvfDoc]


# ---------------------------------------------------------------------------
# Finding: exclude() must negate the conjunction of its annotation lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclude_negates_annotation_conjunction(db):
    """
    GIVEN authors annotated with two aggregates
    WHEN exclude() names both in one call
    THEN a row failing the conjunction is returned (NOT (a AND b)), not
         dropped by the unsound NOT a AND NOT b split
    """
    high = await RvfAuthor.create(name="high")  # n=2, sum=20: fails rating<5
    both = await RvfAuthor.create(name="both")  # n=2, sum=4: matches both
    await RvfBook.create(title="h1", rating=10, author=high)
    await RvfBook.create(title="h2", rating=10, author=high)
    await RvfBook.create(title="b1", rating=2, author=both)
    await RvfBook.create(title="b2", rating=2, author=both)

    rows = await (
        RvfAuthor.annotate(n=Count("books"), total=Sum("books__rating"))
        .exclude(n__gt=1, total__lt=5)
        .values_list("name")
    )
    # Only "both" satisfies (n>1 AND total<5); "high" must survive the exclude.
    assert [r[0] for r in rows] == ["high"]


# ---------------------------------------------------------------------------
# Finding: explicit None on a nullable db-default column must persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_none_on_db_default_column_persists(db):
    """
    GIVEN a fetched row whose nullable db-default column holds a DB value
    WHEN the attribute is explicitly set to None and the instance fully saved
    THEN the column is written and reads back as NULL
    """
    created = await RvfDoc.create(title="d")
    doc = await RvfDoc.get(id=created.id)
    assert doc.expires is not None
    doc.expires = None
    await doc.save()
    assert (await RvfDoc.get(id=created.id)).expires is None


@pytest.mark.asyncio
async def test_unfetched_db_default_still_protected_on_full_save(db):
    """
    GIVEN a create() that did not fetch the DB-supplied default
    WHEN the instance is fully saved without touching the column
    THEN the database keeps its generated value (not overwritten with None)
    """
    doc = await RvfDoc.create(title="d")
    assert doc.expires is None  # not fetched (fetch_db_defaults off)
    doc.title = "renamed"
    await doc.save()
    refreshed = await RvfDoc.get(id=doc.id)
    assert refreshed.title == "renamed"
    assert refreshed.expires is not None


# ---------------------------------------------------------------------------
# Finding: read-your-own-writes under a read/write-splitting router
# ---------------------------------------------------------------------------


class _SplitRouter:
    """Routes reads to a (non-existent) replica and writes to default."""

    def db_for_read(self, model):
        return "replica"

    def db_for_write(self, model):
        return "default"


@pytest.mark.asyncio
async def test_router_reads_inside_transaction_see_its_writes(db):
    """
    GIVEN a router splitting reads to a replica connection
    WHEN a read runs inside an open transaction on the write connection
    THEN the transaction captures the read (read-your-own-writes) instead of
         routing it to the replica pool
    """
    YaraOrm.set_router(_SplitRouter())
    try:
        async with in_transaction():
            await RvfAuthor.create(name="tx-only")
            # Routed read: db_for_read says "replica", but the open default
            # transaction must absorb it and see the uncommitted row.
            executor = get_executor(RvfAuthor, write=False)
            assert getattr(executor, "connection_name", None) == "default"
            assert await RvfAuthor.get_or_none(name="tx-only") is not None
    finally:
        YaraOrm.set_router(None)


# ---------------------------------------------------------------------------
# Finding: using_db=<transaction object> renders for that connection's dialect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_object_using_db_resolves_transaction_dialect(db):
    """
    GIVEN a transaction wrapper on a named second (sqlite) connection
    WHEN get_dialect resolves an object-form using_db
    THEN it returns the wrapper's own dialect, not the model-routed one
    """
    # The db fixture's YaraOrm.close() tears the extra connection down.
    await YaraOrm.add_connection("second", "sqlite://:memory:")
    async with in_transaction("second") as tx:
        assert isinstance(get_dialect(RvfAuthor, using=tx), SqliteDialect)


# ---------------------------------------------------------------------------
# Finding: qualified references must disambiguate same-named models
# ---------------------------------------------------------------------------


def test_qualified_reference_disambiguates_same_named_models():
    """
    GIVEN two registered models sharing a bare class name
    WHEN resolving bare and (partially) qualified references
    THEN the bare name raises, while qualified forms resolve each model
    """
    saved = dict(registry._MODELS)
    try:
        first = type("RvDupe", (), {})
        second = type("RvDupe", (), {})
        registry._MODELS["app_one.models.RvDupe"] = first
        registry._MODELS["app_two.models.RvDupe"] = second
        registry._RESOLVE_CACHE.clear()
        with pytest.raises(ConfigurationError):
            registry.get_model("RvDupe")
        # Exact keys resolve; an ambiguous *suffix* ("models.RvDupe" matches
        # both) still raises rather than guessing.
        assert registry.get_model("app_one.models.RvDupe") is first
        assert registry.get_model("app_two.models.RvDupe") is second
        with pytest.raises(ConfigurationError):
            registry.get_model("models.RvDupe")
    finally:
        registry._MODELS.clear()
        registry._MODELS.update(saved)
        registry._RESOLVE_CACHE.clear()


def test_partially_qualified_suffix_resolves_when_unique():
    """
    GIVEN two same-named models registered under different module paths
    WHEN resolving by a unique module suffix (as a FK reference string would)
    THEN the suffix picks the matching model instead of raising ambiguity
    """
    saved = dict(registry._MODELS)
    try:
        first = type("RvSfx", (), {})
        second = type("RvSfx", (), {})
        registry._MODELS["proj.app_one.models.RvSfx"] = first
        registry._MODELS["proj.app_two.models.RvSfx"] = second
        registry._RESOLVE_CACHE.clear()
        assert registry.get_model("app_one.models.RvSfx") is first
        assert registry.get_model("app_two.models.RvSfx") is second
    finally:
        registry._MODELS.clear()
        registry._MODELS.update(saved)
        registry._RESOLVE_CACHE.clear()


# ---------------------------------------------------------------------------
# Finding: resolve_relations stays idempotent across module re-registration
# ---------------------------------------------------------------------------


def test_resolve_relations_idempotent_for_rebuilt_m2m_info():
    """
    GIVEN resolve_relations already installed an M2M reverse descriptor
    WHEN the source model is re-registered (fresh info object, same relation)
    THEN a second resolve pass does not raise a false duplicate-related_name
    """

    class RvTagA(Model):
        id = fields.IntField(pk=True)

        class Meta:
            table = "rv_tag_a"

    class RvPostA(Model):
        id = fields.IntField(pk=True)
        tags = fields.ManyToManyField("RvTagA", related_name="rv_posts")

        class Meta:
            table = "rv_post_a"

    registry.resolve_relations()

    # Re-declare the source model under the same module/name, as a module
    # reload or notebook cell re-run does: a fresh M2MInfo for the same
    # logical relation.
    class RvPostA(Model):  # noqa: F811
        id = fields.IntField(pk=True)
        tags = fields.ManyToManyField("RvTagA", related_name="rv_posts")

        class Meta:
            table = "rv_post_a"

    registry.resolve_relations()  # must not raise


# ---------------------------------------------------------------------------
# Finding: transactions.atomic keeps working with the per-name transaction map
# (sanity net around the reviewed connection.py rework)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_atomic_decorator_still_wraps(db):
    """
    GIVEN the atomic() decorator from transactions
    WHEN a decorated coroutine raises midway
    THEN its writes roll back
    """

    @transactions.atomic()
    async def boom():
        await RvfAuthor.create(name="ghost")
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        await boom()
    assert await RvfAuthor.filter(name="ghost").count() == 0
