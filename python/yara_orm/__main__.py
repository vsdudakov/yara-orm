"""Command-line migration tool: ``python -m yara_orm <command>``.

Examples:
    python -m yara_orm makemigrations --models myapp.models --name add_age
    python -m yara_orm upgrade   --db postgres://localhost/app --models myapp.models
    python -m yara_orm downgrade --db postgres://localhost/app --models myapp.models
    python -m yara_orm history   --db postgres://localhost/app --models myapp.models
    python -m yara_orm sqlmigrate 0001_initial --db sqlite:///app.db --models myapp.models
"""

from __future__ import annotations

import argparse
import asyncio
import importlib

from . import registry
from .connection import YaraOrm
from .migrations import MigrationManager


def _load_models(spec: str) -> None:
    """Import comma-separated model modules and resolve their relations.

    Args:
        spec: Comma-separated module paths to import.

    Returns:
        None
    """
    if not spec:
        return
    for module in spec.split(","):
        importlib.import_module(module.strip())
    registry.resolve_relations()


async def _run(args: argparse.Namespace) -> None:
    """Dispatch the parsed CLI command against a migration manager.

    Args:
        args: Parsed command-line arguments.

    Returns:
        None
    """
    if args.db:
        await YaraOrm.init(args.db)
    manager = MigrationManager(directory=args.dir, app=args.app)
    try:
        if args.command == "init":
            manager.init()
            print(f"created migrations directory: {manager.directory}")
        elif args.command == "makemigrations":
            name = manager.make_migrations(
                name=args.name, empty=args.empty, allow_destructive=args.allow_destructive
            )
            print(f"created {name}" if name else "no changes detected")
        elif args.command == "upgrade":
            done = await manager.upgrade(target=args.version)
            print("applied: " + (", ".join(done) if done else "nothing to apply"))
        elif args.command == "downgrade":
            reverted = await manager.downgrade(steps=args.steps, target=args.version)
            print("reverted: " + (", ".join(reverted) if reverted else "nothing to revert"))
        elif args.command == "history":
            for row in await manager.history():
                print(f"  {row['name']}  (applied {row['applied_at']})")
        elif args.command == "heads":
            for row in await manager.heads():
                mark = "[x]" if row["applied"] else "[ ]"
                print(f"  {mark} {row['name']}")
        else:  # sqlmigrate (argparse guarantees a valid subcommand)
            for sql in manager.sqlmigrate(args.version, backward=args.backward):
                print(sql + ";")
    finally:
        if args.db:
            await YaraOrm.close()


def main() -> None:
    """Parse command-line arguments and run the requested migration command.

    Returns:
        None
    """
    parser = argparse.ArgumentParser(prog="python -m yara_orm", description="orm migrations")
    parser.add_argument("--dir", default="migrations", help="migrations directory")
    parser.add_argument("--app", default="models", help="application/label name")
    parser.add_argument("--db", default=None, help="database URL")
    parser.add_argument("--models", default="", help="comma-separated model modules to import")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    mk = sub.add_parser("makemigrations")
    mk.add_argument("--name", default=None)
    mk.add_argument("--empty", action="store_true")
    mk.add_argument(
        "--allow-destructive",
        action="store_true",
        help="permit a diff that drops every recorded table (default: abort)",
    )
    up = sub.add_parser("upgrade")
    up.add_argument("version", nargs="?", default=None)
    dn = sub.add_parser("downgrade")
    dn.add_argument("version", nargs="?", default=None)
    dn.add_argument("--steps", type=int, default=1)
    sub.add_parser("history")
    sub.add_parser("heads")
    sm = sub.add_parser("sqlmigrate")
    sm.add_argument("version")
    sm.add_argument("--backward", action="store_true")

    args = parser.parse_args()
    _load_models(args.models)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
