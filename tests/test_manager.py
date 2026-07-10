"""Custom model managers scope every query entry point, including lazy
relation reads (reverse FK and m2m), which build their queryset through the
model's ``Meta.manager`` to match the prefetch path's scoping."""

import pytest

from yara_orm import Manager, Model, fields
from yara_orm.connection import get_dialect, get_engine


class ActiveManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class MgrItem(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, unique=True)
    deleted = fields.BooleanField(default=False)

    class Meta:
        table = "mgr_item"
        manager = ActiveManager()


class RfeActiveManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class RfeBoard(Model):
    id = fields.IntField(pk=True)

    class Meta:
        table = "rfe_board"


class RfePost(Model):
    id = fields.IntField(pk=True)
    board = fields.ForeignKeyField("RfeBoard", related_name="posts")
    deleted = fields.BooleanField(default=False)

    class Meta:
        table = "rfe_post"
        manager = RfeActiveManager()


class RfeCard(Model):
    id = fields.IntField(pk=True)
    labels = fields.ManyToManyField("RfeLabel", related_name="cards")

    class Meta:
        table = "rfe_card"


class RfeLabel(Model):
    id = fields.IntField(pk=True)
    deleted = fields.BooleanField(default=False)

    class Meta:
        table = "rfe_label"
        manager = RfeActiveManager()


MODELS = [MgrItem, RfeBoard, RfePost, RfeCard, RfeLabel]


@pytest.mark.asyncio
async def test_custom_manager_scopes_queries(db):
    """
    GIVEN a model whose Meta.manager filters out soft-deleted rows
    WHEN querying via all/filter/get/get_or_none
    THEN only non-deleted rows are visible through the manager
    """
    await MgrItem.create(name="live", deleted=False)
    await MgrItem.create(name="gone", deleted=True)

    assert [i.name for i in await MgrItem.all()] == ["live"]
    assert await MgrItem.filter(name="gone").count() == 0
    assert (await MgrItem.get(name="live")).name == "live"
    assert await MgrItem.get_or_none(name="gone") is None

    # A row excluded by the manager is still absent after it would match.
    assert await MgrItem.all().count() == 1


# ---------------------------------------------------------------------------
# Lazy relation reads honour a custom Meta.manager
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reverse_fk_lazy_read_matches_prefetch_under_soft_delete(db):
    """
    GIVEN a reverse FK whose source model has a soft-delete Meta.manager
    WHEN the relation is read lazily and via prefetch_related
    THEN both exclude the soft-deleted rows (pre-fix the lazy read leaked them)
    """
    board = await RfeBoard.create()
    live = await RfePost.create(board=board, deleted=False)
    await RfePost.create(board=board, deleted=True)

    lazy_ids = [p.id for p in await board.posts]
    assert lazy_ids == [live.id]
    # Chained access goes through the same manager-scoped queryset.
    assert [p.id for p in await board.posts.all()] == [live.id]
    assert await board.posts.filter(id=live.id).count() == 1

    prefetched = await RfeBoard.filter(id=board.id).prefetch_related("posts")
    assert [p.id for p in await prefetched[0].posts] == lazy_ids


@pytest.mark.asyncio
async def test_m2m_lazy_read_honours_target_manager(db):
    """
    GIVEN an m2m whose target model has a soft-delete Meta.manager
    WHEN links to a live and a soft-deleted target exist
    THEN lazy reads exclude the soft-deleted target while the join rows (and
    add/remove/clear, which operate on the join table) are unaffected
    """
    card = await RfeCard.create()
    live = await RfeLabel.create(deleted=False)
    gone = await RfeLabel.create(deleted=True)
    await card.labels.add(live, gone)

    # Reads are manager-scoped, both awaited and chained.
    assert [x.id for x in await card.labels] == [live.id]
    assert [x.id for x in await card.labels.all()] == [live.id]

    # The join table still holds both links: manager scoping only affects
    # reads of target-model rows, not join-table writes.
    q = get_dialect(RfeCard).quote
    rows = await get_engine().fetch_rows(f"SELECT COUNT(*) FROM {q('rfecard_rfelabel')}", [])
    assert rows[0][0] == 2

    # remove() still unlinks a soft-deleted target.
    await card.labels.remove(gone)
    rows = await get_engine().fetch_rows(f"SELECT COUNT(*) FROM {q('rfecard_rfelabel')}", [])
    assert rows[0][0] == 1


@pytest.mark.asyncio
async def test_reverse_fk_bulk_writes_ignore_manager_scope(db):
    """
    GIVEN a reverse FK whose source model has a soft-delete Meta.manager
    WHEN .update() / .delete() are delegated through the related manager
    THEN every related row is written, not just the manager-visible ones
    (a read scope silently narrowing a relation-wide write would leave
    soft-deleted children pointing at a parent the caller detached)
    """
    board = await RfeBoard.create()
    await RfePost.create(board=board, deleted=False)
    await RfePost.create(board=board, deleted=True)

    # The soft-deleted row is invisible to reads but the write reaches it
    # (MySQL counts only changed rows, so assert on state, not the count).
    assert len(await board.posts) == 1
    await board.posts.update(deleted=False)
    assert len(await board.posts) == 2

    other = await RfeBoard.create()
    await RfePost.create(board=other, deleted=True)
    assert await other.posts.delete() == 1


@pytest.mark.asyncio
async def test_m2m_manager_bulk_writes_use_the_default_write_queryset(db):
    """
    GIVEN an m2m manager (no custom write scoping of its own)
    WHEN .update() is delegated through the manager
    THEN it runs against the base related queryset (the default _write_qs)
    """
    card = await RfeCard.create()
    label = await RfeLabel.create(deleted=False)
    await card.labels.add(label)

    await card.labels.update(deleted=False)
    assert [lb.id for lb in await card.labels] == [label.id]
