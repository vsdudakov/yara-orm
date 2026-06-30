"""Relation-manager queryset chaining, instance-side date coercion, JSON byte
coercion, and null-safe membership subqueries (1.5.x follow-ups).

- ``M2MManager`` and ``RelatedManager`` chain like a queryset (``.all()``,
  ``.limit()``, ``.select_related()``, ``.filter()`` …).
- creating a row from an ISO string leaves a ``datetime``/``date`` on the instance.
- ``JSONField`` coerces ``bytes`` (base64) and errors clearly on unknown leaves.
- ``exclude(col__in=Subquery(values_list))`` is correct even when the subquery
  column is nullable (no ``NOT IN (… NULL …)`` pitfall).
"""

import base64
import datetime as dt

import pytest

from yara_orm import Model, Subquery, fields
from yara_orm.exceptions import FieldError


class RmOrg(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "rm_org"


class RmUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    organisation = fields.ForeignKeyField("RmOrg", related_name="users", null=True)

    class Meta:
        table = "rm_user"


class RmPortfolio(Model):
    id = fields.IntField(pk=True)
    subscribers = fields.ManyToManyField("RmUser", related_name="portfolios", through="rm_pf_user")

    class Meta:
        table = "rm_portfolio"


class RmCompany(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20, null=True)
    portfolio = fields.ForeignKeyField("RmPortfolio", related_name="companies")
    org = fields.ForeignKeyField("RmOrg", related_name="companies", null=True)

    class Meta:
        table = "rm_company"


class RmReport(Model):
    id = fields.IntField(pk=True)
    created_at = fields.DatetimeField(null=True)
    on = fields.DateField(null=True)

    class Meta:
        table = "rm_report"


class RmEvent(Model):
    id = fields.IntField(pk=True)
    report_id = fields.IntField(null=True)

    class Meta:
        table = "rm_event"


class RmDoc(Model):
    id = fields.IntField(pk=True)
    props = fields.JSONField(null=True)

    class Meta:
        table = "rm_doc"


MODELS = [RmOrg, RmUser, RmPortfolio, RmCompany, RmReport, RmEvent, RmDoc]


@pytest.mark.asyncio
async def test_m2m_manager_all_is_chainable_queryset(db):
    """
    GIVEN a portfolio with subscribed users
    WHEN m2m.all() is chained with select_related/order_by
    THEN it returns the related users with the relation eager-loaded
    """
    org = await RmOrg.create(name="Acme")
    u1 = await RmUser.create(name="Ann", organisation=org)
    u2 = await RmUser.create(name="Bob", organisation=org)
    p = await RmPortfolio.create()
    await p.subscribers.add(u1, u2)

    subs = await p.subscribers.all().select_related("organisation").order_by("id")
    assert [s.name for s in subs] == ["Ann", "Bob"]
    assert subs[0].organisation.name == "Acme"


@pytest.mark.asyncio
async def test_m2m_manager_filter_and_limit(db):
    """
    GIVEN a portfolio with subscribers
    WHEN the m2m manager is filtered / limited
    THEN only the matching related rows are returned
    """
    u1 = await RmUser.create(name="Ann")
    u2 = await RmUser.create(name="Bob")
    p = await RmPortfolio.create()
    await p.subscribers.add(u1, u2)

    assert [s.name for s in await p.subscribers.filter(name="Ann")] == ["Ann"]
    assert len(await p.subscribers.all().limit(1)) == 1


@pytest.mark.asyncio
async def test_m2m_manager_proxies_and_order_by_directly(db):
    """
    GIVEN an m2m manager (not via .all())
    WHEN a queryset method (select_related) or order_by is called directly
    THEN it proxies to the related queryset; private names raise AttributeError
    """
    org = await RmOrg.create(name="Acme")
    u1 = await RmUser.create(name="Bob", organisation=org)
    u2 = await RmUser.create(name="Ann", organisation=org)
    p = await RmPortfolio.create()
    await p.subscribers.add(u1, u2)

    proxied = await p.subscribers.select_related("organisation").order_by("name")
    assert [u.name for u in proxied] == ["Ann", "Bob"]
    assert proxied[0].organisation.name == "Acme"

    ordered = await p.subscribers.order_by("-name")
    assert [u.name for u in ordered] == ["Bob", "Ann"]

    with pytest.raises(AttributeError):
        _ = p.subscribers._not_a_real_method  # noqa: B018


@pytest.mark.asyncio
async def test_related_manager_private_attr_raises(db):
    """
    GIVEN a reverse-FK manager
    WHEN a private (underscore) attribute that does not exist is accessed
    THEN AttributeError is raised (not proxied to the queryset)
    """
    p = await RmPortfolio.create()
    with pytest.raises(AttributeError):
        _ = p.companies._not_a_real_method  # noqa: B018


def test_multi_column_projection_subquery_is_not_null_guarded():
    """
    GIVEN a multi-column values_list projection used as a subquery
    WHEN it renders its SELECT
    THEN both columns are projected and no single-column NULL guard is added
    """
    from yara_orm.dialects import PostgresDialect

    sub = RmEvent.all().values_list("id", "report_id")
    sql, _params, _ = sub._plain_select_sql(PostgresDialect())
    assert '"id"' in sql and '"report_id"' in sql
    assert "IS NOT NULL" not in sql


@pytest.mark.asyncio
async def test_related_manager_proxies_queryset_methods(db):
    """
    GIVEN a reverse-FK manager
    WHEN limit()/select_related()/order_by() are chained on it
    THEN it behaves like a queryset
    """
    org = await RmOrg.create(name="Acme")
    p = await RmPortfolio.create()
    for _ in range(3):
        await RmCompany.create(portfolio=p, org=org, name="c")

    companies = await p.companies.limit(2).select_related("org").order_by("id")
    assert len(companies) == 2
    assert companies[0].org.name == "Acme"
    assert await p.companies.all().count() == 3


@pytest.mark.asyncio
async def test_create_from_iso_string_keeps_datetime_on_instance(db):
    """
    GIVEN datetime/date columns
    WHEN a row is created from ISO strings
    THEN the in-memory attributes are datetime/date (not the raw strings)
    """
    r = await RmReport.create(created_at="2026-07-01T12:00:00+00:00", on="2026-07-01")
    assert isinstance(r.created_at, dt.datetime)
    assert r.created_at.isoformat().startswith("2026-07-01T12:00:00")
    assert isinstance(r.on, dt.date)
    assert r.on == dt.date(2026, 7, 1)


@pytest.mark.asyncio
async def test_jsonfield_coerces_bytes(db):
    """
    GIVEN a JSON column holding a bytes value
    WHEN the row is stored and re-read
    THEN the bytes round-trip as a base64 string
    """
    d = await RmDoc.create(props={"blob": b"hello", "n": 1})
    got = await RmDoc.get(id=d.id)
    assert got.props == {"blob": base64.b64encode(b"hello").decode(), "n": 1}


def test_jsonfield_raises_clear_error_on_unknown_leaf():
    """
    GIVEN a JSON value containing an unserialisable nested object
    WHEN it is prepared for binding
    THEN a FieldError names the offending path
    """
    with pytest.raises(FieldError, match=r"meta\.obj"):
        fields.JSONField().to_db({"meta": {"obj": object()}})


@pytest.mark.asyncio
async def test_exclude_in_subquery_is_null_safe(db):
    """
    GIVEN a subquery over a nullable column that contains a NULL
    WHEN exclude(id__in=Subquery(...)) runs
    THEN the NULL does not defeat the exclusion (no NOT IN (… NULL …) pitfall)
    """
    r1 = await RmReport.create()
    r2 = await RmReport.create()
    await RmEvent.create(report_id=r1.id)
    await RmEvent.create(report_id=None)  # NULL in the subquery

    sub = RmEvent.all().values_list("report_id", flat=True)
    remaining = await RmReport.filter(id__in=[r1.id, r2.id]).exclude(id__in=Subquery(sub))
    assert sorted(r.id for r in remaining) == [r2.id]


@pytest.mark.asyncio
async def test_in_subquery_still_matches(db):
    """
    GIVEN the same nullable subquery
    WHEN it is used positively (id__in)
    THEN only the referenced rows match (NULL filtering does not drop real ids)
    """
    r1 = await RmReport.create()
    await RmReport.create()
    await RmEvent.create(report_id=r1.id)
    await RmEvent.create(report_id=None)

    sub = RmEvent.all().values_list("report_id", flat=True)
    matched = await RmReport.filter(id__in=Subquery(sub))
    assert [r.id for r in matched] == [r1.id]
