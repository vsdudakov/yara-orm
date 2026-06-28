"""Default query ordering via ``Meta.ordering``."""

import pytest

from yara_orm import Model, fields
from yara_orm.exceptions import FieldError


class OrdPost(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    rank = fields.IntField()

    class Meta:
        table = "ord_post"
        ordering = ["-rank", "title"]


def test_meta_ordering_parsed_onto_meta():
    """
    GIVEN a model declaring Meta.ordering
    WHEN its metadata is inspected
    THEN the specs are parsed into (field, descending) tuples
    """
    assert OrdPost._meta.ordering == [("rank", True), ("title", False)]


def test_meta_ordering_rejects_unknown_field():
    """
    GIVEN Meta.ordering referencing a field that does not exist
    WHEN the class is defined
    THEN a FieldError is raised at class-creation time
    """
    with pytest.raises(FieldError):

        class Bad(Model):
            id = fields.IntField(pk=True)

            class Meta:
                table = "ord_bad"
                ordering = ["nonexistent"]


@pytest.mark.asyncio
async def test_default_ordering_applied(sqlite_db):
    """
    GIVEN rows and a model with Meta.ordering = ["-rank", "title"]
    WHEN they are fetched without an explicit order_by
    THEN they come back in the default order
    """
    await OrdPost.create(title="b", rank=1)
    await OrdPost.create(title="a", rank=2)
    await OrdPost.create(title="c", rank=2)

    titles = [p.title for p in await OrdPost.all()]
    # rank DESC first (2 before 1), then title ASC within the same rank.
    assert titles == ["a", "c", "b"]


@pytest.mark.asyncio
async def test_explicit_order_by_overrides_default(sqlite_db):
    """
    GIVEN a model with a default ordering
    WHEN an explicit order_by is supplied
    THEN the explicit ordering wins
    """
    await OrdPost.create(title="b", rank=1)
    await OrdPost.create(title="a", rank=2)

    ranks = [p.rank for p in await OrdPost.all().order_by("rank")]
    assert ranks == [1, 2]


@pytest.mark.asyncio
async def test_default_ordering_applies_through_filter(sqlite_db):
    """
    GIVEN a filtered query with no explicit order_by
    WHEN it is evaluated
    THEN the model's default ordering still applies
    """
    await OrdPost.create(title="x", rank=5)
    await OrdPost.create(title="y", rank=9)
    await OrdPost.create(title="z", rank=7)

    ranks = [p.rank for p in await OrdPost.filter(rank__gte=5)]
    assert ranks == [9, 7, 5]
