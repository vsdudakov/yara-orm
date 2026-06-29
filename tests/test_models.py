"""Coverage: model construction, persistence and bulk branches."""

import datetime as dt
import uuid

import pytest

from yara_orm import FieldError, Model, fields, registry


class CvMUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    alias = fields.CharField(max_length=20, db_column="alias_col", null=True)
    touched = fields.DatetimeField(auto_now=True, null=True)

    class Meta:
        table = "cov_muser"


class CvMOnlyPk(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "cov_monlypk"


class CvMRef(Model):
    id = fields.IntField(pk=True)
    user = fields.ForeignKeyField("CvMUser", related_name="refs", null=True)
    pals = fields.ManyToManyField("CvMUser", related_name="palled", through="cov_pals")

    class Meta:
        table = "cov_mref"


class CvMTimestamped(Model):
    id = fields.UUIDField(pk=True, default=uuid.uuid4)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        abstract = True


class CvMConcrete(CvMTimestamped):
    name = fields.CharField(max_length=20)

    class Meta:
        table = "cov_mconcrete"


MODELS = [CvMUser, CvMOnlyPk, CvMConcrete, CvMRef]


def test_abstract_base_is_not_registered():
    """
    GIVEN an abstract model and a concrete subclass of it
    WHEN their classes are created
    THEN only the concrete subclass is registered and gets a table
    """
    names = {m.__name__ for m in registry.all_models()}
    assert "CvMTimestamped" not in names
    assert "CvMConcrete" in names
    assert CvMTimestamped._meta.abstract is True


def test_abstract_is_not_inherited_and_fields_propagate():
    """
    GIVEN a concrete subclass of an abstract base
    WHEN its metadata is inspected
    THEN it is itself concrete and inherits the base's fields and UUID pk
    """
    assert CvMConcrete._meta.abstract is False
    assert list(CvMConcrete._meta.fields) == ["id", "created_at", "name"]
    assert CvMConcrete._meta.pk_field.model_field_name == "id"
    assert isinstance(CvMConcrete(name="x").id, uuid.UUID)


@pytest.mark.asyncio
async def test_m2m_kwarg_rejected_at_construction(db):
    """
    GIVEN a model with a many-to-many field
    WHEN it is constructed with that field as a kwarg
    THEN a TypeError explains to use the manager after saving
    """
    with pytest.raises(TypeError):
        CvMRef(pals=[1])


@pytest.mark.asyncio
async def test_init_relation_value_variants(db):
    """
    GIVEN a foreign-key relation
    WHEN constructing with None, a model instance, and a raw id
    THEN the backing column is set from each form
    """
    u = await CvMUser.create(name="u")
    assert CvMRef(user=None).user_id is None
    assert CvMRef(user=u).user_id == u.id
    assert CvMRef(user=u.id).user_id == u.id


@pytest.mark.asyncio
async def test_db_column_override_kwarg(db):
    """
    GIVEN a field whose db_column differs from its attribute name
    WHEN constructing with the column name as a kwarg
    THEN the value is assigned and round-trips
    """
    u = await CvMUser.create(name="x", alias_col="al")
    assert u.alias == "al"
    assert (await CvMUser.get(id=u.id)).alias == "al"


@pytest.mark.asyncio
async def test_auto_now_updates_on_save(db):
    """
    GIVEN a model with an auto_now timestamp
    WHEN it is saved and re-saved
    THEN the timestamp is (re)populated on every save
    """
    u = await CvMUser.create(name="a")
    assert isinstance(u.touched, dt.datetime)
    u.name = "b"
    await u.save()
    assert (await CvMUser.get(id=u.id)).name == "b"


@pytest.mark.asyncio
async def test_save_update_fields_writes_only_named(db):
    """
    GIVEN a persisted row with several mutated columns
    WHEN save(update_fields=["name"]) is called
    THEN only the named column is written; the others keep their stored value
    """
    u = await CvMUser.create(name="a", alias="ax")
    u.name = "b"
    u.alias = "bx"  # mutated but NOT in update_fields
    await u.save(update_fields=["name"])

    reloaded = await CvMUser.get(id=u.id)
    assert reloaded.name == "b"
    assert reloaded.alias == "ax"  # the unnamed column was left untouched


@pytest.mark.asyncio
async def test_save_update_fields_restricts_auto_now(db):
    """
    GIVEN a model with an auto_now timestamp
    WHEN save(update_fields=[...]) omits / includes that column
    THEN the timestamp is bumped only when it is named
    """
    u = await CvMUser.create(name="a")
    before = (await CvMUser.get(id=u.id)).touched

    u.name = "b"
    await u.save(update_fields=["name"])  # touched not named -> not bumped
    assert (await CvMUser.get(id=u.id)).touched == before

    await u.save(update_fields=["touched"])  # named -> bumped
    assert (await CvMUser.get(id=u.id)).touched > before


@pytest.mark.asyncio
async def test_save_update_fields_empty_is_noop(db):
    """
    GIVEN a mutated in-memory instance
    WHEN save(update_fields=[]) is called
    THEN no UPDATE runs and the stored row is unchanged
    """
    u = await CvMUser.create(name="a")
    u.name = "b"
    await u.save(update_fields=[])
    assert (await CvMUser.get(id=u.id)).name == "a"


@pytest.mark.asyncio
async def test_save_update_fields_unknown_raises(db):
    """
    GIVEN a persisted row
    WHEN save(update_fields=[...]) names a field the model does not have
    THEN a FieldError is raised
    """
    u = await CvMUser.create(name="a")
    u.name = "b"
    with pytest.raises(FieldError):
        await u.save(update_fields=["nope"])


@pytest.mark.asyncio
async def test_save_update_fields_relation_maps_to_fk_column(db):
    """
    GIVEN a row with a foreign key
    WHEN save(update_fields=["user"]) names the relation
    THEN the relation's backing FK column is written
    """
    u1 = await CvMUser.create(name="u1")
    u2 = await CvMUser.create(name="u2")
    ref = await CvMRef.create(user=u1)

    ref.user = u2
    await ref.save(update_fields=["user"])
    assert (await CvMRef.get(id=ref.id)).user_id == u2.id


@pytest.mark.asyncio
async def test_save_update_fields_skips_pk_and_duplicates(db):
    """
    GIVEN update_fields naming the primary key and a repeated field
    WHEN save(update_fields=[...]) runs
    THEN the pk and duplicate entries are skipped and the real column is written
    """
    u = await CvMUser.create(name="a")
    u.name = "b"
    await u.save(update_fields=["pk", "name", "name"])
    assert (await CvMUser.get(id=u.id)).name == "b"


@pytest.mark.asyncio
async def test_save_pk_only_model_is_noop(db):
    """
    GIVEN a persisted model whose only column is its primary key
    WHEN it is saved again with no update_fields
    THEN there is nothing to UPDATE and the call is a no-op
    """
    row = await CvMOnlyPk.create()
    await row.save()  # no non-pk columns -> no UPDATE statement
    assert await CvMOnlyPk.filter(id=row.id).exists() is True


@pytest.mark.asyncio
async def test_create_with_explicit_pk_uses_full_insert(db):
    """
    GIVEN an explicit primary key on a new instance
    WHEN it is created
    THEN the full INSERT path (not the cached fast path) is used
    """
    u = await CvMUser.create(id=500, name="z")
    assert u.id == 500
    assert (await CvMUser.get(id=500)).name == "z"


@pytest.mark.asyncio
async def test_default_values_insert_and_bulk(db):
    """
    GIVEN a model whose only column is an auto-increment primary key
    WHEN rows are created singly and via bulk_create
    THEN DEFAULT VALUES inserts assign primary keys
    """
    one = await CvMOnlyPk.create()
    assert one.pk is not None
    rows = await CvMOnlyPk.bulk_create([CvMOnlyPk(), CvMOnlyPk()])
    assert len(rows) == 2 and all(r.pk is not None for r in rows)
    assert await CvMOnlyPk.all().count() == 3


@pytest.mark.asyncio
async def test_bulk_create_empty_is_noop(db):
    """
    GIVEN an empty iterable
    WHEN bulk_create is called
    THEN it returns an empty list without touching the database
    """
    assert await CvMUser.bulk_create([]) == []


@pytest.mark.asyncio
async def test_prefetch_related_classmethod(db):
    """
    GIVEN a model
    WHEN prefetch_related is called as a classmethod
    THEN it returns a queryset configured with the prefetch
    """
    u = await CvMUser.create(name="p")
    await CvMRef.create(user=u)
    [ref] = await CvMRef.prefetch_related("user")
    assert ref.user.id == u.id  # prefetched -> synchronous access


@pytest.mark.asyncio
async def test_get_and_get_or_none_operator_fallback(db):
    """
    GIVEN lookups that use an operator (not plain equality)
    WHEN get / get_or_none run
    THEN they fall back to the full query builder
    """
    u = await CvMUser.create(name="solo")
    assert (await CvMUser.get(id__gte=1)).id == u.id
    assert (await CvMUser.get_or_none(name__icontains="ol")).id == u.id
    assert await CvMUser.get_or_none(name__icontains="zzz") is None
