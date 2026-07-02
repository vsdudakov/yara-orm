"""Audit regression coverage for relations and the registry.

- forward FK descriptor cache: invalidated on None/raw-pk assignment and on a
  direct ``<name>_id`` write; a resolved ``None`` is never cached;
- assigning an unsaved instance to a forward relation raises;
- ``Prefetch(relation, queryset=...)`` constrains forward FK/O2O prefetches;
- ``related_name``: '%(class)s' substitution for abstract bases, duplicate
  claims raise ``ConfigurationError``;
- registry: reverse descriptors resolve by qualified ``module.Name``, and
  ambiguous bare references raise instead of guessing.
"""

import pytest

from yara_orm import Model, Prefetch, fields, registry
from yara_orm.exceptions import ConfigurationError
from yara_orm.models import ModelMeta


class ArAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    active = fields.BooleanField(default=True)

    class Meta:
        table = "ar_author"


class ArBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    author = fields.ForeignKeyField("ArAuthor", related_name="ar_books", null=True)

    class Meta:
        table = "ar_book"


class ArEntryBase(Model):
    id = fields.IntField(pk=True)
    author = fields.ForeignKeyField("ArAuthor", related_name="%(class)s_set")

    class Meta:
        abstract = True


class ArNote(ArEntryBase):
    body = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "ar_note"


class ArPost(ArEntryBase):
    body = fields.CharField(max_length=20, null=True)

    class Meta:
        table = "ar_post"


MODELS = [ArAuthor, ArBook, ArNote, ArPost]


# -- FK descriptor cache invalidation (finding 3) --------------------------------


@pytest.mark.asyncio
async def test_assigning_none_clears_cached_related_object(sqlite_db):
    """
    GIVEN a book whose author relation is cached from assignment
    WHEN the relation is set to None
    THEN the accessor resolves to None instead of serving the stale object
    """
    a = await ArAuthor.create(name="Ada")
    book = await ArBook.create(title="B", author=a)
    assert (await book.author).id == a.id  # cached

    book.author = None
    assert book.author_id is None
    assert await book.author is None


@pytest.mark.asyncio
async def test_assigning_raw_pk_replaces_cached_related_object(sqlite_db):
    """
    GIVEN a book whose author relation is cached
    WHEN a different author's raw primary key is assigned
    THEN the accessor loads the new author, not the stale cached one
    """
    a1 = await ArAuthor.create(name="One")
    a2 = await ArAuthor.create(name="Two")
    book = await ArBook.create(title="B", author=a1)
    assert (await book.author).id == a1.id

    book.author = a2.id
    assert (await book.author).id == a2.id


@pytest.mark.asyncio
async def test_direct_fk_column_write_bypasses_stale_cache(sqlite_db):
    """
    GIVEN a book whose author relation is cached
    WHEN the underlying author_id attribute is written directly
    THEN the accessor notices the mismatch and reloads the current author
    """
    a1 = await ArAuthor.create(name="One")
    a2 = await ArAuthor.create(name="Two")
    book = await ArBook.create(title="B", author=a1)
    assert (await book.author).id == a1.id

    book.author_id = a2.id  # bypasses the relation descriptor
    assert (await book.author).id == a2.id


@pytest.mark.asyncio
async def test_resolved_none_is_not_cached_forever(sqlite_db):
    """
    GIVEN a book whose author lookup once resolved to None (dangling key)
    WHEN the key later points at an existing row
    THEN the accessor loads it instead of serving the cached None
    """
    book = await ArBook.create(title="B", author=None)
    assert await book.author is None

    book.author_id = 4242  # no such author yet
    assert await book.author is None

    await ArAuthor.create(id=4242, name="Late")
    late = await book.author
    assert late is not None and late.name == "Late"


# -- unsaved related instances (finding 5) ----------------------------------------


@pytest.mark.asyncio
async def test_assigning_unsaved_instance_raises(sqlite_db):
    """
    GIVEN an unsaved author (no primary key)
    WHEN it is assigned to a forward relation (constructor or attribute)
    THEN a ValueError is raised instead of silently storing a NULL foreign key
    """
    unsaved = ArAuthor(name="ghost")
    with pytest.raises(ValueError, match="isn't saved"):
        ArBook(title="B", author=unsaved)

    saved = await ArAuthor.create(name="real")
    book = await ArBook.create(title="B", author=saved)
    with pytest.raises(ValueError, match="isn't saved"):
        book.author = unsaved
    # The relation is untouched by the failed assignment.
    assert book.author_id == saved.id


# -- custom queryset for forward prefetch (finding 4) ------------------------------


@pytest.mark.asyncio
async def test_prefetch_custom_queryset_constrains_forward_fk(sqlite_db):
    """
    GIVEN books whose authors are a mix of active and inactive
    WHEN prefetching the forward FK with a queryset filtered to active authors
    THEN only active authors populate the cache (inactive resolve to None)
    """
    live = await ArAuthor.create(name="Live", active=True)
    dead = await ArAuthor.create(name="Dead", active=False)
    b1 = await ArBook.create(title="L", author=live)
    b2 = await ArBook.create(title="D", author=dead)

    books = await ArBook.filter(id__in=[b1.id, b2.id]).prefetch_related(
        Prefetch("author", queryset=ArAuthor.filter(active=True))
    )
    by_title = {b.title: b for b in books}
    assert by_title["L"].author.name == "Live"  # served from the prefetch cache
    assert by_title["D"].__dict__["_prefetch"]["author"] is None


# -- related_name: %(class)s and duplicate detection (finding 8) --------------------


@pytest.mark.asyncio
async def test_class_placeholder_gives_each_subclass_its_own_reverse_name(sqlite_db):
    """
    GIVEN an abstract base FK declaring related_name="%(class)s_set"
    WHEN two concrete subclasses link rows to one author
    THEN each subclass installs its own reverse accessor with distinct contents
    """
    a = await ArAuthor.create(name="Ada")
    await ArNote.create(author=a, body="note")
    await ArPost.create(author=a, body="post")

    assert [n.body for n in await a.arnote_set] == ["note"]
    assert [p.body for p in await a.arpost_set] == ["post"]


def test_duplicate_related_name_on_one_target_raises():
    """
    GIVEN two models claiming the same related_name on one target
    WHEN reverse relations are resolved
    THEN a ConfigurationError names the collision (and the %(class)s remedy)
    """

    class ArDupTarget(Model):
        id = fields.IntField(pk=True)

        class Meta:
            table = "ar_dup_target"

    class ArDupSrcA(Model):
        id = fields.IntField(pk=True)
        target = fields.ForeignKeyField("ArDupTarget", related_name="ar_dups")

        class Meta:
            table = "ar_dup_a"

    class ArDupSrcB(Model):
        id = fields.IntField(pk=True)
        target = fields.ForeignKeyField("ArDupTarget", related_name="ar_dups")

        class Meta:
            table = "ar_dup_b"

    try:
        with pytest.raises(ConfigurationError, match="already used by.*%\\(class\\)s"):
            registry.resolve_relations()
    finally:
        # Unregister the colliding trio so later inits resolve cleanly.
        for model in (ArDupTarget, ArDupSrcA, ArDupSrcB):
            registry._MODELS.pop(f"{model.__module__}.{model.__name__}", None)
        registry._RESOLVE_CACHE.clear()


# -- qualified reverse resolution / ambiguous bare names (finding 9) ----------------


def test_reverse_descriptor_carries_qualified_source_reference():
    """
    GIVEN an installed reverse FK accessor
    WHEN its source reference is inspected
    THEN it is the qualified module.ClassName form (exact registry resolution)
    """
    descriptor = ArAuthor.__dict__["ar_books"]
    assert descriptor.source_reference == f"{ArBook.__module__}.ArBook"
    assert descriptor._resolve_source() is ArBook


def test_ambiguous_bare_model_reference_raises():
    """
    GIVEN two registered models sharing a bare class name in different modules
    WHEN the bare name is resolved
    THEN a ConfigurationError lists the candidates, while qualified names and
         unambiguous bare names still resolve
    """

    def _make(module_name: str) -> type[Model]:
        return ModelMeta(
            "ArAmbiguous",
            (Model,),
            {
                "__module__": module_name,
                "__qualname__": "ArAmbiguous",
                "id": fields.IntField(pk=True),
                "Meta": type("Meta", (), {"table": f"{module_name}_amb", "abstract": False}),
            },
        )

    m1 = _make("ar_fake_mod_one")
    m2 = _make("ar_fake_mod_two")
    try:
        with pytest.raises(ConfigurationError, match="Ambiguous"):
            registry.get_model("ArAmbiguous")
        assert registry.get_model("ar_fake_mod_one.ArAmbiguous") is m1
        assert registry.get_model("ar_fake_mod_two.ArAmbiguous") is m2
        # An unambiguous bare name keeps resolving.
        assert registry.get_model("ArAuthor") is ArAuthor
    finally:
        for model in (m1, m2):
            registry._MODELS.pop(f"{model.__module__}.{model.__name__}", None)
        registry._RESOLVE_CACHE.clear()


def test_fk_related_name_colliding_with_plain_attribute_raises():
    """
    GIVEN a FK whose related_name matches a plain attribute on the target
    WHEN reverse relations are resolved
    THEN a ConfigurationError names the occupied attribute
    """

    class ArAttrTarget(Model):
        id = fields.IntField(pk=True)
        ar_taken = "occupied"

        class Meta:
            table = "ar_attr_target"

    class ArAttrSrc(Model):
        id = fields.IntField(pk=True)
        target = fields.ForeignKeyField("ArAttrTarget", related_name="ar_taken")

        class Meta:
            table = "ar_attr_src"

    try:
        with pytest.raises(ConfigurationError, match="already used by attribute 'ar_taken'"):
            registry.resolve_relations()
    finally:
        for model in (ArAttrTarget, ArAttrSrc):
            registry._MODELS.pop(f"{model.__module__}.{model.__name__}", None)
        registry._RESOLVE_CACHE.clear()


def test_duplicate_m2m_related_name_on_one_target_raises():
    """
    GIVEN two M2M relations claiming the same related_name on one target
    WHEN reverse relations are resolved
    THEN a ConfigurationError names the m2m relation already holding it
    """

    class ArM2mTarget(Model):
        id = fields.IntField(pk=True)

        class Meta:
            table = "ar_m2m_target"

    class ArM2mSrcA(Model):
        id = fields.IntField(pk=True)
        links = fields.ManyToManyField(
            "ArM2mTarget", related_name="ar_m2m_dup", through="ar_m2m_a_t"
        )

        class Meta:
            table = "ar_m2m_a"

    class ArM2mSrcB(Model):
        id = fields.IntField(pk=True)
        links = fields.ManyToManyField(
            "ArM2mTarget", related_name="ar_m2m_dup", through="ar_m2m_b_t"
        )

        class Meta:
            table = "ar_m2m_b"

    try:
        with pytest.raises(ConfigurationError, match="already used by m2m relation 'links'"):
            registry.resolve_relations()
    finally:
        for model in (ArM2mTarget, ArM2mSrcA, ArM2mSrcB):
            registry._MODELS.pop(f"{model.__module__}.{model.__name__}", None)
        registry._RESOLVE_CACHE.clear()


def test_m2m_related_name_colliding_with_plain_attribute_raises():
    """
    GIVEN an M2M whose related_name matches a plain attribute on the target
    WHEN reverse relations are resolved
    THEN a ConfigurationError names the occupied attribute
    """

    class ArM2mAttrTarget(Model):
        id = fields.IntField(pk=True)
        ar_m2m_taken = "occupied"

        class Meta:
            table = "ar_m2m_attr_target"

    class ArM2mAttrSrc(Model):
        id = fields.IntField(pk=True)
        links = fields.ManyToManyField(
            "ArM2mAttrTarget", related_name="ar_m2m_taken", through="ar_m2m_attr_t"
        )

        class Meta:
            table = "ar_m2m_attr_src"

    try:
        with pytest.raises(ConfigurationError, match="already used by attribute 'ar_m2m_taken'"):
            registry.resolve_relations()
    finally:
        for model in (ArM2mAttrTarget, ArM2mAttrSrc):
            registry._MODELS.pop(f"{model.__module__}.{model.__name__}", None)
        registry._RESOLVE_CACHE.clear()


def test_assigning_raw_key_without_prefetch_cache_sets_source_column():
    """
    GIVEN an unsaved instance that never had related objects cached
    WHEN a raw key value is assigned to its forward FK attribute
    THEN the key column is set directly (no prefetch cache to invalidate)
    """
    book = ArBook(title="raw-key")
    book.author = 7
    assert "_prefetch" not in book.__dict__
    assert book.__dict__["author_id"] == 7
