"""Runtime behaviour of the Tortoise-style relation typing aliases.

Deliberately does NOT use ``from __future__ import annotations``: class-body
annotations here are evaluated at class-definition time, proving the aliases
are real, subscriptable runtime objects (including string forward references),
and that annotated relations behave exactly like unannotated ones.
"""

import pytest

from yara_orm import Model, fields, relations


class TyAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)

    # Annotation-only declarations: the accessors are installed by the FK /
    # M2M ``related_name`` on the other side.
    books: fields.ReverseRelation["TyBook"]
    liked_books: fields.ManyToManyRelation["TyBook"]

    class Meta:
        table = "ty_author"


class TyBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=100)
    author: fields.ForeignKeyRelation[TyAuthor] = fields.ForeignKeyField(
        "TyAuthor", related_name="books"
    )
    editor: fields.ForeignKeyNullableRelation[TyAuthor] = fields.ForeignKeyField(
        "TyAuthor", null=True, related_name="edited_books"
    )
    fans: fields.ManyToManyRelation[TyAuthor] = fields.ManyToManyField(
        "TyAuthor", related_name="liked_books"
    )

    class Meta:
        table = "ty_book"


MODELS = [TyAuthor, TyBook]


def test_aliases_subscript_to_generic_aliases():
    """
    GIVEN the relation typing aliases on both fields and relations modules
    WHEN subscripted with a model class or a string forward reference
    THEN each returns a real parameterised generic (not a no-op None)
    """
    assert fields.ForeignKeyRelation[TyAuthor] is not None
    assert fields.OneToOneRelation[TyAuthor] is not None
    assert fields.OneToOneNullableRelation["TyAuthor"] is not None
    assert fields.ReverseRelation["TyBook"] is not None
    # The fields spellings are the relations aliases, re-exported lazily.
    assert fields.ReverseRelation is relations.RelatedManager
    assert fields.ManyToManyRelation is relations.M2MManager
    assert fields.ForeignKeyRelation == relations.ForeignKeyRelation


def test_fields_module_getattr_rejects_unknown_names():
    """
    GIVEN the lazy PEP 562 re-export hook on the fields module
    WHEN an unknown attribute is requested
    THEN a plain AttributeError names the module and attribute
    """
    with pytest.raises(AttributeError, match="NoSuchExport"):
        fields.NoSuchExport  # noqa: B018


@pytest.mark.asyncio
async def test_annotated_relations_behave_like_unannotated(db):
    """
    GIVEN models whose relations carry typing annotations (evaluated at
          class-definition time) and annotation-only reverse declarations
    WHEN rows are created and the relations are accessed
    THEN forward await, reverse manager and M2M manager all work unchanged
    """
    author = await TyAuthor.create(name="Ada")
    book = await TyBook.create(title="Vol 1", author=author)

    assert (await TyBook.get(id=book.id).select_related("author")).author.name == "Ada"
    assert await book.editor is None  # nullable FK, unset
    assert [b.title for b in await author.books] == ["Vol 1"]

    await book.fans.add(author)
    assert [a.name for a in await book.fans] == ["Ada"]
    assert [b.title for b in await author.liked_books] == ["Vol 1"]
