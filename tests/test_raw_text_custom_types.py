"""Custom PostgreSQL column types: ``RawText`` binds + ``select_as_text`` reads.

The engine binds a plain ``str`` with a declared ``text`` type, which
PostgreSQL will not implicitly cast to a custom column type (SQLSTATE 42804 —
the pgvector ``vector`` failure mode), and it requests binary result values,
which it cannot decode for such types. The fix is declared on the field class:
``to_db`` returns a ``yara_orm.RawText`` (bound untyped, so the server infers
the column's type and parses the text form itself) and ``select_as_text =
True`` makes PostgreSQL projections read the column through
``CAST(col AS text)``.

The cross-backend model uses ``inet`` — a built-in PostgreSQL type with no
implicit cast from ``text`` and no engine decoder, i.e. exactly the shape of a
pgvector column, but needing no extension. On every other backend the kind is
plain text and ``RawText`` binds like a normal string.
"""

import pytest

from yara_orm import Model, RawText, fields, register_field_kind
from yara_orm.dialects import PostgresDialect, SqliteDialect


class InetField(fields.Field):
    """An IP-network column: PostgreSQL ``inet``, plain text elsewhere."""

    field_kind = "ipaddr"
    read_identity = False
    select_as_text = True

    def to_db(self, value):
        return None if value is None else RawText(str(value))

    def to_python(self, value):
        return value


register_field_kind(
    "ipaddr",
    field_cls=InetField,
    sql={
        "postgres": "inet",
        "sqlite": "TEXT",
        "mysql": "VARCHAR(64)",
        "oracle": "VARCHAR2(64)",
        "mssql": "NVARCHAR(64)",
    },
)


class Host(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=50)
    addr = InetField(null=True)

    class Meta:
        table = "rawtext_host"


class HostGroup(Model):
    id = fields.IntField(pk=True)
    gateway = fields.ForeignKeyField("Host", related_name="groups")
    members = fields.ManyToManyField("Host", related_name="member_of", through="rawtext_group_host")

    class Meta:
        table = "rawtext_hostgroup"


MODELS = [Host, HostGroup]


def test_rawtext_is_a_str():
    """
    GIVEN a RawText value
    WHEN treated as a string
    THEN it behaves exactly like the wrapped str (thin marker subclass)
    """
    v = RawText("10.0.0.1")
    assert isinstance(v, str)
    assert v == "10.0.0.1"
    assert v.upper() == "10.0.0.1"


def test_select_column_casts_only_flagged_fields_on_postgres():
    """
    GIVEN the postgres and sqlite dialects
    WHEN select_column renders a flagged and an unflagged field
    THEN only postgres casts the flagged column, and keeps its reference name
    """
    pg, lite = PostgresDialect(), SqliteDialect()
    flagged, plain = InetField(), fields.CharField(max_length=10)
    assert pg.select_column(flagged, '"addr"') == 'CAST("addr" AS text)'
    assert pg.select_column(plain, '"name"') == '"name"'
    assert lite.select_column(flagged, '"addr"') == '"addr"'


@pytest.mark.asyncio
async def test_create_and_get_roundtrip(db):
    """
    GIVEN a custom-typed column with RawText binds and select_as_text reads
    WHEN a row is created and fetched back (full row and only())
    THEN the value round-trips as its text form on every backend
    """
    created = await Host.create(name="a", addr="10.1.2.3/32")
    assert created.addr == "10.1.2.3/32"
    fetched = await Host.get(id=created.id)
    assert fetched.addr == "10.1.2.3/32"
    partial = await Host.filter(id=created.id).only("id", "addr").first()
    assert partial.addr == "10.1.2.3/32"
    empty = await Host.create(name="b")
    assert (await Host.get(id=empty.id)).addr is None


@pytest.mark.asyncio
async def test_filter_update_and_values_paths(db):
    """
    GIVEN rows with custom-typed values
    WHEN filtering on, updating and projecting the column
    THEN the untyped bind matches the column type and values() decodes text
    """
    row = await Host.create(name="a", addr="10.1.2.3/32")
    await Host.create(name="b", addr="10.9.9.9/32")

    assert (await Host.filter(addr="10.1.2.3/32").count()) == 1
    got = await Host.filter(addr="10.1.2.3/32").first()
    assert got.id == row.id

    await Host.filter(id=row.id).update(addr="172.16.0.1/32")
    assert (await Host.get(id=row.id)).addr == "172.16.0.1/32"

    values = dict(await Host.all().order_by("name").values_list("name", "addr"))
    assert values == {"a": "172.16.0.1/32", "b": "10.9.9.9/32"}


@pytest.mark.asyncio
async def test_bulk_create_and_save_paths(db):
    """
    GIVEN instances carrying custom-typed values
    WHEN persisted via bulk_create and instance save()
    THEN both write paths bind the untyped text form successfully
    """
    await Host.bulk_create([Host(name=f"h{i}", addr=f"10.0.0.{i}/32") for i in range(1, 4)])
    assert (await Host.all().count()) == 3

    one = await Host.get(name="h1")
    one.addr = "10.0.0.100/32"
    await one.save()
    assert (await Host.get(name="h1")).addr == "10.0.0.100/32"


@pytest.mark.asyncio
async def test_select_related_and_prefetch_read_casted_columns(db):
    """
    GIVEN a relation whose target model carries a custom-typed column
    WHEN loaded via select_related and prefetch_related
    THEN the joined/prefetched projections decode the text form
    """
    gw = await Host.create(name="gw", addr="192.168.1.1/32")
    group = await HostGroup.create(gateway=gw)
    await group.members.add(gw)

    joined = await HostGroup.all().select_related("gateway").first()
    assert joined.gateway.addr == "192.168.1.1/32"

    fetched = (await HostGroup.all().prefetch_related("members"))[0]
    members = await fetched.members
    assert [h.addr for h in members] == ["192.168.1.1/32"]
