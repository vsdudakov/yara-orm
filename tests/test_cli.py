"""Coverage: the ``python -m yara_orm`` migration CLI (end-to-end)."""

import os
import sys
import tempfile

import pytest

from yara_orm.__main__ import main


@pytest.fixture
def cli_project():
    """A temp project exposing an importable models module on sys.path."""
    work = tempfile.mkdtemp()
    (open(os.path.join(work, "cli_models.py"), "w")).write(
        "from yara_orm import Model, fields\n\n"
        "class CliThing(Model):\n"
        "    name = fields.CharField(max_length=50)\n"
        "    class Meta:\n"
        "        table = 'cov_cli_thing'\n"
    )
    sys.path.insert(0, work)
    db = os.path.join(work, "app.db")
    mig = os.path.join(work, "migrations")
    yield {"db": f"sqlite://{db}", "dir": mig}
    sys.path.remove(work)
    sys.modules.pop("cli_models", None)


def _run(argv):
    old = sys.argv
    sys.argv = ["orm", *argv]
    try:
        main()
    finally:
        sys.argv = old


# SQLite-only: the CLI is driven end-to-end against a concrete sqlite:// URL
# created by cli_project, so this stays single-backend (no shared db fixture).
def test_cli_full_lifecycle(cli_project, capsys):
    """
    GIVEN a project with a models module
    WHEN the CLI runs init/makemigrations/sqlmigrate/upgrade/history/heads/downgrade
    THEN each command executes against a real SQLite database
    """
    base = ["--dir", cli_project["dir"], "--app", "cli", "--models", "cli_models"]
    db = ["--db", cli_project["db"]]

    _run([*base, "init"])
    assert "migrations directory" in capsys.readouterr().out

    _run([*base, "makemigrations", "--name", "initial"])
    assert "created 0001_initial.py" in capsys.readouterr().out

    # No further changes detected on a second run.
    _run([*base, "makemigrations"])
    assert "no changes detected" in capsys.readouterr().out

    _run([*base, *db, "sqlmigrate", "0001_initial"])
    assert "CREATE TABLE" in capsys.readouterr().out

    _run([*base, *db, "upgrade"])
    assert "applied: 0001_initial" in capsys.readouterr().out

    _run([*base, *db, "history"])
    assert "0001_initial" in capsys.readouterr().out

    _run([*base, *db, "heads"])
    assert "[x] 0001_initial" in capsys.readouterr().out

    _run([*base, *db, "downgrade"])
    assert "reverted: 0001_initial" in capsys.readouterr().out

    # Nothing left to apply/revert.
    _run([*base, *db, "downgrade"])
    assert "nothing to revert" in capsys.readouterr().out
    _run([*base, "makemigrations", "--empty", "--name", "blank"])
    capsys.readouterr()
    _run([*base, *db, "upgrade"])
    assert "applied" in capsys.readouterr().out
