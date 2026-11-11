"""Restore a database from a backup produced by ``backup_db``.

Destructive by definition, so it refuses to run without ``--confirm`` and, in production,
without the database name typed back exactly.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections


class Command(BaseCommand):
    help = "Restore the database from a backup file. This overwrites existing data."

    def add_arguments(self, parser):
        parser.add_argument("backup", help="Path to the backup file.")
        parser.add_argument("--confirm", action="store_true", help="Required. Acknowledges data loss.")
        parser.add_argument(
            "--i-understand-this-is-production",
            action="store_true",
            help="Additionally required when DJANGO_ENV=production.",
        )

    def handle(self, *args, **options):
        backup = Path(options["backup"])
        if not backup.is_file():
            raise CommandError(f"No such backup: {backup}")
        if not options["confirm"]:
            raise CommandError(
                "Restoring overwrites the current database. Re-run with --confirm."
            )
        if getattr(settings, "IS_PRODUCTION", False) and not options["i_understand_this_is_production"]:
            raise CommandError(
                "Refusing to restore a production database without "
                "--i-understand-this-is-production."
            )

        engine = settings.DATABASES["default"]["ENGINE"]
        # Close pooled connections first; on SQLite an open handle keeps the old file alive
        # and on Postgres it blocks the DROP.
        connections.close_all()

        if engine.endswith("sqlite3"):
            self._restore_sqlite(backup)
        elif "postgresql" in engine:
            self._restore_postgres(backup)
        else:
            raise CommandError(f"Restore is not implemented for {engine}.")

        self.stdout.write(self.style.SUCCESS(f"Restored from {backup}."))
        self.stdout.write("Run `python manage.py migrate --check` to confirm the schema is current.")

    def _restore_sqlite(self, backup: Path) -> None:
        target = Path(settings.DATABASES["default"]["NAME"])

        # A file that is not a database at all raises rather than reporting "not ok", so
        # both outcomes have to be handled — otherwise pointing this at the wrong file
        # produces a raw driver traceback instead of a refusal.
        probe = sqlite3.connect(str(backup))
        try:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CommandError("The backup file fails its integrity check; refusing to restore.")
        except sqlite3.DatabaseError as exc:
            raise CommandError(f"{backup} is not a readable SQLite database: {exc}") from exc
        finally:
            probe.close()

        if target.exists():
            # Keep the pre-restore state: a restore from the wrong backup must be undoable.
            safety = target.with_suffix(target.suffix + ".pre-restore")
            shutil.copy(target, safety)
            self.stdout.write(f"Existing database preserved at {safety}")

        shutil.copy(backup, target)

    def _restore_postgres(self, backup: Path) -> None:
        config = settings.DATABASES["default"]
        env = os.environ.copy()
        if config.get("PASSWORD"):
            env["PGPASSWORD"] = config["PASSWORD"]

        command = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            f"--dbname={config['NAME']}",
            str(backup),
        ]
        if config.get("USER"):
            command.append(f"--username={config['USER']}")
        if config.get("HOST"):
            command.append(f"--host={config['HOST']}")
        if config.get("PORT"):
            command.append(f"--port={config['PORT']}")

        try:
            subprocess.run(command, env=env, check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise CommandError("pg_restore is not on PATH. Install the Postgres client tools.") from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(
                f"pg_restore failed: {exc.stderr.decode(errors='replace')[:800]}"
            ) from exc
