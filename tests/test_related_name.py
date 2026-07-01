"""``related_name`` reverse accessors for FK, O2O and M2M relations.

Each relation kind installs a reverse accessor on the *target* model under its
``related_name``:
- forward FK -> reverse ``RelatedManager`` (a chainable set of source rows),
- forward O2O -> reverse single instance (or ``None`` when absent),
- forward M2M -> reverse ``M2MManager`` (a chainable set), on both sides.
"""

import pytest

from yara_orm import Model, fields


class RnAuthor(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "rn_author"


class RnBook(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)
    author = fields.ForeignKeyField("RnAuthor", related_name="books")

    class Meta:
        table = "rn_book"


class RnUser(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)

    class Meta:
        table = "rn_user"


class RnPassport(Model):
    id = fields.IntField(pk=True)
    number = fields.CharField(max_length=20)
    user = fields.OneToOneField("RnUser", related_name="passport")

    class Meta:
        table = "rn_passport"


class RnCourse(Model):
    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=20)

    class Meta:
        table = "rn_course"


class RnStudent(Model):
    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=20)
    courses = fields.ManyToManyField("RnCourse", related_name="students")

    class Meta:
        table = "rn_student"


MODELS = [RnAuthor, RnBook, RnUser, RnPassport, RnCourse, RnStudent]


@pytest.mark.asyncio
async def test_fk_related_name_reverse_manager(db):
    """
    GIVEN an author with books linked by a FK carrying related_name="books"
    WHEN the reverse manager is awaited, filtered and counted
    THEN it yields exactly that author's books and chains like a queryset
    """
    a = await RnAuthor.create(name="Ada")
    other = await RnAuthor.create(name="Zed")
    await RnBook.create(title="A1", author=a)
    await RnBook.create(title="A2", author=a)
    await RnBook.create(title="Z1", author=other)

    assert sorted(b.title for b in await a.books) == ["A1", "A2"]
    assert await a.books.filter(title="A1").count() == 1
    assert [b.title for b in await a.books.order_by("-title")] == ["A2", "A1"]
    # An author with no books gets an empty reverse set (not an error).
    lonely = await RnAuthor.create(name="Solo")
    assert list(await lonely.books) == []


@pytest.mark.asyncio
async def test_fk_related_name_reverse_isnull_filter(db):
    """
    GIVEN authors with and without books
    WHEN filtering the target by the reverse related_name existence
    THEN isnull=True selects authors with no books and False the ones with books
    """
    a = await RnAuthor.create(name="Has")
    await RnAuthor.create(name="Hasnt")
    await RnBook.create(title="B", author=a)

    with_books = {x.name for x in await RnAuthor.filter(books__isnull=False)}
    without = {x.name for x in await RnAuthor.filter(books__isnull=True)}
    assert with_books == {"Has"}
    assert without == {"Hasnt"}


@pytest.mark.asyncio
async def test_o2o_related_name_reverse_single_instance(db):
    """
    GIVEN a user with (and without) a passport via a OneToOne related_name
    WHEN the reverse accessor is awaited
    THEN it resolves to the single linked instance, or None when absent
    """
    u = await RnUser.create(name="Owner")
    await RnPassport.create(number="PX1", user=u)

    back = await u.passport
    assert back is not None and back.number == "PX1"
    assert (await back.user).id == u.id  # forward side still works

    # A user with no passport: the reverse O2O resolves to None, not an error.
    nobody = await RnUser.create(name="NoDocs")
    assert await nobody.passport is None


@pytest.mark.asyncio
async def test_m2m_related_name_reverse_both_directions(db):
    """
    GIVEN students linked to courses via a M2M with related_name="students"
    WHEN both the forward (student.courses) and reverse (course.students) managers
         are awaited
    THEN each yields the correct set and reverse filtering works
    """
    s1 = await RnStudent.create(name="S1")
    s2 = await RnStudent.create(name="S2")
    c1 = await RnCourse.create(title="Math")
    c2 = await RnCourse.create(title="Art")
    await s1.courses.add(c1, c2)
    await s2.courses.add(c1)

    assert sorted(c.title for c in await s1.courses) == ["Art", "Math"]
    # Reverse: Math has both students, Art only s1.
    assert sorted(s.name for s in await c1.students) == ["S1", "S2"]
    assert [s.name for s in await c2.students] == ["S1"]
    assert await c1.students.filter(name="S1").count() == 1


def test_related_name_colliding_with_target_field_is_rejected():
    """
    GIVEN a related_name equal to a real column on the target
    WHEN the reverse accessor would be installed
    THEN it raises ConfigurationError instead of silently dropping the accessor
    """
    from yara_orm.exceptions import ConfigurationError
    from yara_orm.registry import _check_related_name

    # RnAuthor declares the column "name": claiming it as a related_name would
    # silently shadow the reverse accessor, so it must be rejected.
    with pytest.raises(ConfigurationError, match="conflicts"):
        _check_related_name("name", RnAuthor, "SomeSource")
    # A free name installs cleanly.
    _check_related_name("articles", RnAuthor, "SomeSource")
