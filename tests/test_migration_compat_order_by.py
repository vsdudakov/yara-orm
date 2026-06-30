"""Tortoise-migration compatibility: order_by across a forward relation.

Covers ordering on a related column (``rel__col`` / multi-hop) via a correlated
subquery, plus the public ``BaseDBAsyncClient`` executor type.
"""

import pytest

from yara_orm import BaseDBAsyncClient, Model, connections, fields
from yara_orm.exceptions import FieldError


class ObCountry(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "ob_country"


class ObAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    country = fields.ForeignKeyField("ObCountry", related_name="authors")

    class Meta:
        table = "ob_author"


class ObBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    author = fields.ForeignKeyField("ObAuthor", related_name="books")

    class Meta:
        table = "ob_book"


MODELS = [ObCountry, ObAuthor, ObBook]


@pytest.mark.asyncio
async def test_order_by_forward_relation_column(db):
    """
    GIVEN books whose authors sort differently from the books themselves
    WHEN ordering by ``author__name`` (a forward-relation column)
    THEN rows come back ordered by the related column
    """
    ada = await ObAuthor.create(name="Ada", country=await ObCountry.create(name="UK"))
    bob = await ObAuthor.create(name="Bob", country=await ObCountry.create(name="US"))
    await ObBook.create(title="zeta", author=ada)
    await ObBook.create(title="alpha", author=bob)

    books = await ObBook.all().order_by("author__name")
    assert [b.title for b in books] == ["zeta", "alpha"]  # Ada before Bob

    desc = await ObBook.all().order_by("-author__name")
    assert [b.title for b in desc] == ["alpha", "zeta"]


@pytest.mark.asyncio
async def test_order_by_multi_hop_forward_relation(db):
    """
    GIVEN a two-hop forward path book -> author -> country
    WHEN ordering by ``author__country__name``
    THEN rows are ordered by the far related column
    """
    uk = await ObCountry.create(name="AA")
    us = await ObCountry.create(name="ZZ")
    ada = await ObAuthor.create(name="Ada", country=us)
    bob = await ObAuthor.create(name="Bob", country=uk)
    await ObBook.create(title="from_ada", author=ada)
    await ObBook.create(title="from_bob", author=bob)

    books = await ObBook.all().order_by("author__country__name")
    assert [b.title for b in books] == ["from_bob", "from_ada"]  # AA before ZZ


@pytest.mark.asyncio
async def test_order_by_reverse_relation_rejected(db):
    """
    GIVEN a reverse relation (one-to-many)
    WHEN ordering by it as a relation path
    THEN a FieldError is raised (no single orderable value)
    """
    with pytest.raises(FieldError):
        await ObAuthor.all().order_by("books__title")


@pytest.mark.asyncio
async def test_connections_get_satisfies_base_db_async_client(orm):
    """
    GIVEN the public ``BaseDBAsyncClient`` executor protocol
    WHEN the active connection is inspected
    THEN it is a structural instance of the protocol
    """
    assert isinstance(connections.get(), BaseDBAsyncClient)
