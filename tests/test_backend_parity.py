"""Cross-backend parity suite, born with the MySQL backend (phase A).

Runs the behaviours the MySQL port had to re-implement — RETURNING-less pk
backfill, INSERT IGNORE / ON DUPLICATE KEY upserts, LIKE case semantics and
escaping, UTC datetime canonicalisation, transactions with savepoints — on
every configured backend via the standard ``db`` fixture, asserting the same
outcome everywhere and encoding the documented per-backend differences
(SQLite's case-insensitive LIKE, MySQL's naive DATETIME) explicitly.
"""

import datetime as dt
import enum
import uuid
from decimal import Decimal

import pytest

from yara_orm import Model, RawSQL, fields, in_transaction
from yara_orm.db_defaults import RandomHex
from yara_orm.exceptions import IntegrityError


class MyColor(str, enum.Enum):
    RED = "red"
    BLUE = "blue"


class MyPriority(enum.IntEnum):
    LOW = 1
    HIGH = 2


class MyAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50, unique=True)
    score = fields.IntField(default=0)

    class Meta:
        table = "my_be_author"


class MyPost(Model):
    id = fields.BigIntField(pk=True)
    author = fields.ForeignKeyField("MyAuthor", related_name="posts")
    title = fields.CharField(max_length=100)
    tags = fields.ManyToManyField("MyTag", related_name="posts")

    class Meta:
        table = "my_be_post"


class MyTag(Model):
    id = fields.IntField(pk=True)
    label = fields.CharField(max_length=30)

    class Meta:
        table = "my_be_tag"


class MyEverything(Model):
    """One column per field kind, for the round-trip test."""

    id = fields.BigIntField(pk=True)
    small = fields.SmallIntField(default=1)
    number = fields.IntField(default=2)
    big = fields.BigIntField(default=3)
    name = fields.CharField(max_length=80)
    body = fields.TextField(null=True)
    flag = fields.BooleanField(default=False)
    ratio = fields.FloatField(null=True)
    price = fields.DecimalField(max_digits=12, decimal_places=3, null=True)
    when = fields.DatetimeField(null=True)
    day = fields.DateField(null=True)
    at = fields.TimeField(null=True)
    span = fields.TimeDeltaField(null=True)
    ref = fields.UUIDField(null=True)
    data = fields.JSONField(null=True)
    blob = fields.BinaryField(null=True)
    color = fields.CharEnumField(MyColor, max_length=10, null=True)
    priority = fields.IntEnumField(MyPriority, null=True)

    class Meta:
        table = "my_be_everything"


class MyGuidRow(Model):
    """Client-supplied UUID primary key (no auto-increment backfill)."""

    id = fields.UUIDField(pk=True)
    note = fields.CharField(max_length=40)

    class Meta:
        table = "my_be_guid"


class MyStamped(Model):
    """A database-default column (drives the dynamic insert path)."""

    id = fields.IntField(pk=True)
    code = fields.CharField(max_length=32, db_default=RandomHex(4))

    class Meta:
        table = "my_be_stamped"


MODELS = [MyAuthor, MyTag, MyPost, MyEverything, MyGuidRow, MyStamped]


# -- schema + CRUD round-trip --------------------------------------------------


@pytest.mark.asyncio
async def test_full_field_round_trip(db):
    """
    GIVEN a model with one column per field kind
    WHEN a fully-populated row is created and fetched back
    THEN every value round-trips: aware datetimes come back at the same UTC
         instant (naive on MySQL, whose DATETIME has no timezone), decimals
         keep their scale, JSON keeps nesting, uuids are uuid.UUID, bytes are
         bytes and enum members are members
    """
    aware = dt.datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    ref = uuid.uuid4()
    created = await MyEverything.create(
        small=-5,
        number=123,
        big=2**40,
        name="rounder",
        body="long text " * 10,
        flag=True,
        ratio=2.5,
        price=Decimal("12345.678"),
        when=aware,
        day=dt.date(2024, 5, 6),
        at=dt.time(7, 8, 9, 123456),
        span=dt.timedelta(days=1, seconds=5, microseconds=7),
        ref=ref,
        data={"nested": {"list": [1, "two", {"x": None}], "ok": True}},
        blob=b"\x00\xffbinary",
        color=MyColor.BLUE,
        priority=MyPriority.HIGH,
    )
    got = await MyEverything.get(id=created.pk)
    assert got.small == -5
    assert got.number == 123
    assert got.big == 2**40
    assert got.name == "rounder"
    assert got.body == "long text " * 10
    assert got.flag is True
    assert got.ratio == 2.5
    assert got.price == Decimal("12345.678")
    expected_utc = aware.astimezone(dt.timezone.utc)
    if db == "mysql":
        # DATETIME is naive: the aware value is stored as its UTC instant.
        assert got.when == expected_utc.replace(tzinfo=None)
    else:
        assert got.when == expected_utc
    assert got.day == dt.date(2024, 5, 6)
    assert got.at == dt.time(7, 8, 9, 123456)
    assert got.span == dt.timedelta(days=1, seconds=5, microseconds=7)
    assert got.ref == ref and isinstance(got.ref, uuid.UUID)
    assert got.data == {"nested": {"list": [1, "two", {"x": None}], "ok": True}}
    assert got.blob == b"\x00\xffbinary"
    assert got.color is MyColor.BLUE
    assert got.priority is MyPriority.HIGH


@pytest.mark.asyncio
async def test_create_backfills_the_auto_increment_pk(db):
    """
    GIVEN a model with an auto-increment pk
    WHEN instances are created (RETURNING or, on MySQL, the last-insert id)
    THEN each gets its database-assigned pk written back, increasing
    """
    a = await MyAuthor.create(name="first")
    b = await MyAuthor.create(name="second")
    assert isinstance(a.pk, int) and isinstance(b.pk, int)
    assert b.pk == a.pk + 1
    assert (await MyAuthor.get(id=b.pk)).name == "second"


@pytest.mark.asyncio
async def test_explicit_pk_insert_is_not_overwritten(db):
    """
    GIVEN a client-supplied uuid primary key
    WHEN the instance is created (dynamic insert path, nothing to read back)
    THEN the pk survives untouched and the row is fetchable by it
    """
    key = uuid.uuid4()
    row = await MyGuidRow.create(id=key, note="kept")
    assert row.pk == key
    fetched = await MyGuidRow.get(id=key)
    assert fetched.pk == key and isinstance(fetched.pk, uuid.UUID)
    assert fetched.note == "kept"


@pytest.mark.asyncio
async def test_db_default_column_fills_and_accepts_explicit_values(db):
    """
    GIVEN a column with a database-side default (random hex)
    WHEN one row omits it and one supplies it explicitly (the dynamic insert
         path, which must still backfill the auto pk)
    THEN the database fills the first and the explicit value wins on the second
    """
    filled = await MyStamped.create()
    assert isinstance(filled.pk, int)
    from_db = await MyStamped.get(id=filled.pk)
    assert len(from_db.code) == 8  # RandomHex(4) -> 8 hex chars
    explicit = await MyStamped.create(code="fixedval")
    assert isinstance(explicit.pk, int) and explicit.pk != filled.pk
    assert (await MyStamped.get(id=explicit.pk)).code == "fixedval"


# -- bulk_create -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_create_backfills_consecutive_pks(db):
    """
    GIVEN a batch insert into an auto-increment table
    WHEN bulk_create runs (RETURNING pks, or on MySQL the arithmetic backfill
         from the batch's first generated id)
    THEN pks are backfilled consecutively in row order and match the rows
    """
    objs = await MyAuthor.bulk_create([MyAuthor(name=f"bulk{i}") for i in range(5)])
    pks = [o.pk for o in objs]
    assert pks == list(range(pks[0], pks[0] + 5))
    for obj in objs:
        assert (await MyAuthor.get(id=obj.pk)).name == obj.name


@pytest.mark.asyncio
async def test_bulk_create_with_client_pks(db):
    """
    GIVEN objects that carry their own (uuid) primary keys
    WHEN bulk_create runs
    THEN the rows are inserted and the pks stay client-supplied
    """
    rows = [MyGuidRow(id=uuid.uuid4(), note=f"g{i}") for i in range(3)]
    created = await MyGuidRow.bulk_create(rows)
    assert [r.pk for r in created] == [r.pk for r in rows]
    assert await MyGuidRow.all().count() == 3


@pytest.mark.asyncio
async def test_bulk_create_ignore_conflicts_skips_duplicates(db):
    """
    GIVEN a unique column with an existing row
    WHEN bulk_create(ignore_conflicts=True) re-inserts it plus a new row
    THEN the duplicate is skipped and the new row lands (ON CONFLICT DO
         NOTHING; INSERT IGNORE on MySQL)
    """
    await MyAuthor.create(name="dup")
    await MyAuthor.bulk_create(
        [MyAuthor(name="dup"), MyAuthor(name="fresh")], ignore_conflicts=True
    )
    names = sorted(a.name for a in await MyAuthor.all())
    assert names == ["dup", "fresh"]


@pytest.mark.asyncio
async def test_bulk_create_upsert_updates_on_duplicate_key(db):
    """
    GIVEN an existing row keyed by a unique column
    WHEN bulk_create(update_fields=...) re-sends it with new values
    THEN the row is updated in place (ON CONFLICT DO UPDATE; the 8.4-safe
         alias ON DUPLICATE KEY UPDATE on MySQL)
    """
    first = await MyAuthor.create(name="up", score=1)
    await MyAuthor.bulk_create(
        [MyAuthor(name="up", score=99), MyAuthor(name="new", score=5)],
        update_fields=["score"],
        on_conflict=["name"],
    )
    assert await MyAuthor.all().count() == 2
    assert (await MyAuthor.get(id=first.pk)).score == 99
    assert (await MyAuthor.get(name="new")).score == 5


# -- filtering / ordering ----------------------------------------------------------


@pytest.mark.asyncio
async def test_like_lookups_have_correct_case_semantics(db):
    """
    GIVEN rows whose names differ only by case
    WHEN contains/icontains/startswith/iexact filter them
    THEN the i-variants are case-insensitive everywhere, and the plain
         variants are case-SENSITIVE on PostgreSQL (LIKE) and MySQL
         (LIKE BINARY); SQLite's LIKE is documented case-insensitive
    """
    await MyAuthor.create(name="Hello World")
    await MyAuthor.create(name="hello mars")
    both = {"Hello World", "hello mars"}
    assert {a.name for a in await MyAuthor.filter(name__icontains="HELLO")} == both
    assert {a.name for a in await MyAuthor.filter(name__iexact="HELLO WORLD")} == {"Hello World"}
    sensitive_contains = {a.name for a in await MyAuthor.filter(name__contains="Hello")}
    sensitive_starts = {a.name for a in await MyAuthor.filter(name__startswith="hello")}
    if db == "sqlite":
        assert sensitive_contains == both
        assert sensitive_starts == both
    else:
        assert sensitive_contains == {"Hello World"}
        assert sensitive_starts == {"hello mars"}


@pytest.mark.asyncio
async def test_like_metacharacters_match_literally(db):
    """
    GIVEN values containing %, _ and backslash
    WHEN a contains lookup binds them
    THEN they match literally on every backend (the ESCAPE clause survives
         MySQL's backslash-eating string literals) instead of acting as
         wildcards
    """
    await MyAuthor.create(name="100% sure")
    await MyAuthor.create(name="under_score")
    await MyAuthor.create(name="back\\slash")
    await MyAuthor.create(name="decoy xyz")
    assert [a.name for a in await MyAuthor.filter(name__contains="0% s")] == ["100% sure"]
    assert [a.name for a in await MyAuthor.filter(name__contains="r_s")] == ["under_score"]
    assert [a.name for a in await MyAuthor.filter(name__contains="k\\s")] == ["back\\slash"]
    # "_" must not wildcard-match the space in "decoy xyz".
    assert await MyAuthor.filter(name__contains="decoy_xyz").count() == 0


@pytest.mark.asyncio
async def test_like_lookups_accept_sql_expression_values(db):
    """
    GIVEN pattern lookups whose comparison value is a SQL expression (RawSQL)
    WHEN contains and icontains compile against it
    THEN the dialect's LIKE spellings apply to the expression
    """
    await MyAuthor.create(name="Hello World")
    await MyAuthor.create(name="hello mars")
    both = {"Hello World", "hello mars"}
    sensitive = {a.name for a in await MyAuthor.filter(name__contains=RawSQL("'%Hello%'"))}
    assert sensitive == (both if db == "sqlite" else {"Hello World"})
    insensitive = {a.name for a in await MyAuthor.filter(name__icontains=RawSQL("'%HELLO%'"))}
    assert insensitive == both


@pytest.mark.asyncio
async def test_order_limit_and_offset_only_slice(db):
    """
    GIVEN several rows
    WHEN ordering with limit and with an offset-only slice
    THEN both work (the offset-only form needs the dialect's no-limit
         sentinel: -1 on SQLite, the max row count on MySQL)
    """
    await MyAuthor.bulk_create([MyAuthor(name=f"n{i}") for i in range(5)])
    top = await MyAuthor.all().order_by("name").limit(2)
    assert [a.name for a in top] == ["n0", "n1"]
    rest = await MyAuthor.all().order_by("name")[3:]
    assert [a.name for a in rest] == ["n3", "n4"]


@pytest.mark.asyncio
async def test_update_delete_and_date_part_lookup(db):
    """
    GIVEN persisted rows
    WHEN queryset update, date-part filtering and delete run
    THEN each affects exactly the matching rows
    """
    row = await MyEverything.create(name="upd", when=dt.datetime(2023, 11, 5, 4, 3, 2))
    updated = await MyEverything.filter(id=row.pk).update(number=7)
    assert updated == 1
    assert (await MyEverything.get(id=row.pk)).number == 7
    assert await MyEverything.filter(when__year=2023).count() == 1
    assert await MyEverything.filter(when__year=1999).count() == 0
    row2 = await MyEverything.get(id=row.pk)
    await row2.delete()
    assert await MyEverything.all().count() == 0


# -- transactions ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_transaction_commit_and_rollback(db):
    """
    GIVEN in_transaction blocks
    WHEN one exits cleanly and one raises
    THEN the first block's row is persisted and the second's rolled back
    """
    async with in_transaction():
        await MyAuthor.create(name="committed")
    with pytest.raises(RuntimeError, match="boom"):
        async with in_transaction():
            await MyAuthor.create(name="discarded")
            raise RuntimeError("boom")
    names = [a.name for a in await MyAuthor.all()]
    assert names == ["committed"]


@pytest.mark.asyncio
async def test_nested_savepoint_rolls_back_only_the_inner_block(db):
    """
    GIVEN a nested in_transaction block (a savepoint)
    WHEN the inner block raises and the outer commits
    THEN only the inner block's work is discarded
    """
    async with in_transaction():
        await MyAuthor.create(name="outer")
        with pytest.raises(ValueError, match="inner"):
            async with in_transaction():
                await MyAuthor.create(name="inner")
                raise ValueError("inner")
        await MyAuthor.create(name="after")
    names = sorted(a.name for a in await MyAuthor.all())
    assert names == ["after", "outer"]


@pytest.mark.asyncio
async def test_select_for_update_locks_inside_a_transaction(db):
    """
    GIVEN a row and an open transaction
    WHEN it is fetched with select_for_update
    THEN the query executes and returns the row (FOR UPDATE on
         PostgreSQL/MySQL, a documented no-op on SQLite)
    """
    row = await MyAuthor.create(name="locked")
    async with in_transaction():
        got = await MyAuthor.filter(id=row.pk).select_for_update().get()
        assert got.name == "locked"


@pytest.mark.asyncio
async def test_serializable_isolation_level_is_accepted(db):
    """
    GIVEN an explicit isolation level
    WHEN a transaction begins with it (SET TRANSACTION before BEGIN on MySQL)
    THEN the block runs and commits normally on every backend
    """
    async with in_transaction(isolation="SERIALIZABLE"):
        await MyAuthor.create(name="iso")
    assert await MyAuthor.filter(name="iso").exists()


# -- integrity -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_unique_value_raises_integrity_error(db):
    """
    GIVEN a unique column
    WHEN a duplicate value is inserted
    THEN IntegrityError surfaces (MySQL error 1062 mapped to Integrity)
    """
    await MyAuthor.create(name="uniq")
    with pytest.raises(IntegrityError):
        await MyAuthor.create(name="uniq")


@pytest.mark.asyncio
async def test_foreign_key_violation_raises_integrity_error(db):
    """
    GIVEN a table-level FOREIGN KEY constraint (MySQL ignores inline ones)
    WHEN a row referencing a missing parent is inserted
    THEN IntegrityError surfaces, proving the constraint was really created
    """
    with pytest.raises(IntegrityError):
        await MyPost.create(author=999999, title="orphan")


# -- relations -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_related_hydrates_the_parent(db):
    """
    GIVEN a post with an author
    WHEN it is fetched with select_related
    THEN the joined author instance is hydrated alongside the post
    """
    author = await MyAuthor.create(name="rel")
    post = await MyPost.create(author=author, title="joined")
    got = await MyPost.filter(id=post.pk).select_related("author").get()
    assert got.title == "joined"
    assert got.author.pk == author.pk
    assert got.author.name == "rel"


@pytest.mark.asyncio
async def test_m2m_add_list_and_remove(db):
    """
    GIVEN a post and tags
    WHEN tags are added (twice — the duplicate pair is skipped), listed and
         removed
    THEN the join table reflects each step
    """
    author = await MyAuthor.create(name="m2m")
    post = await MyPost.create(author=author, title="tagged")
    red = await MyTag.create(label="red")
    blue = await MyTag.create(label="blue")
    await post.tags.add(red, blue)
    await post.tags.add(red)  # duplicate pair: skipped, not an error
    labels = sorted(t.label for t in await post.tags)
    assert labels == ["blue", "red"]
    await post.tags.remove(red)
    assert [t.label for t in await post.tags] == ["blue"]


@pytest.mark.asyncio
async def test_get_or_create(db):
    """
    GIVEN get_or_create on a unique column
    WHEN called twice with the same key
    THEN the first call creates and the second returns the same row
    """
    first, created = await MyAuthor.get_or_create(name="goc")
    assert created is True
    again, created = await MyAuthor.get_or_create(name="goc")
    assert created is False
    assert again.pk == first.pk
    assert await MyAuthor.all().count() == 1
