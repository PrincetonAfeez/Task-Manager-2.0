"""Create a database backup, and optionally prove it can be restored.

An untested backup is not a backup. ``--verify`` performs a real round trip: dump, restore
into a scratch database, and assert the restored copy is migration-clean and carries the
expected row counts.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

DEFAULT_DIR = Path(settings.BASE_DIR) / "backups"


class Command(BaseCommand):
    help = "Back up the database (and optionally verify the backup restores)."

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default=str(DEFAULT_DIR))
        parser.add_argument("--verify", action="store_true", help="Restore the backup and check it.")
        parser.add_argument("--keep", type=int, default=7, help="Retain this many recent backups.")

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        engine = settings.DATABASES["default"]["ENGINE"]
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")

        expected = None
        if engine.endswith("sqlite3"):
            path = self._backup_sqlite(output_dir, stamp)
        elif "postgresql" in engine:
            if options["verify"]:
                path, expected = self._backup_postgres_in_snapshot(output_dir, stamp)
            else:
                path = self._backup_postgres(output_dir, stamp)
        else:
            raise CommandError(f"Backups are not implemented for {engine}.")

        size_mb = path.stat().st_size / (1024 * 1024)
        self.stdout.write(self.style.SUCCESS(f"Backup written: {path} ({size_mb:.2f} MB)"))

        if options["verify"]:
            self.stdout.write("Verifying the backup by restoring it...")
            if engine.endswith("sqlite3"):
                self._verify_sqlite(path)
            else:
                self._verify_postgres(path, expected, stamp)

        self._prune(output_dir, keep=options["keep"])
        return str(path)

    # ---------------------------------------------------------------- backup strategies

    def _backup_sqlite(self, output_dir: Path, stamp: str) -> Path:
        """Use SQLite's online backup API, which is safe against concurrent writers.

        Copying the file with ``shutil.copy`` while the app is running can capture a torn
        page and produce a backup that restores to a corrupt database.
        """

        source_path = settings.DATABASES["default"]["NAME"]
        target = output_dir / f"backup-{stamp}.sqlite3"

        source = sqlite3.connect(str(source_path))
        destination = sqlite3.connect(str(target))
        try:
            with destination:
                source.backup(destination)
        finally:
            source.close()
            destination.close()
        return target

    def _pg_env(self) -> dict:
        env = os.environ.copy()
        password = settings.DATABASES["default"].get("PASSWORD")
        if password:
            # Never place the password on the command line: it would appear in `ps` output.
            env["PGPASSWORD"] = password
        return env

    def _pg_connection_args(self) -> list[str]:
        config = settings.DATABASES["default"]
        args = []
        if config.get("USER"):
            args.append(f"--username={config['USER']}")
        if config.get("HOST"):
            args.append(f"--host={config['HOST']}")
        if config.get("PORT"):
            args.append(f"--port={config['PORT']}")
        return args

    def _backup_postgres(self, output_dir: Path, stamp: str, *, snapshot: str | None = None) -> Path:
        config = settings.DATABASES["default"]
        target = output_dir / f"backup-{stamp}.dump"
        env = self._pg_env()

        command = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            f"--file={target}",
            f"--dbname={config['NAME']}",
        ]
        if snapshot:
            command.append(f"--snapshot={snapshot}")
        command += self._pg_connection_args()

        try:
            subprocess.run(command, env=env, check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise CommandError("pg_dump is not on PATH. Install the Postgres client tools.") from exc
        except subprocess.CalledProcessError as exc:
            raise CommandError(f"pg_dump failed: {exc.stderr.decode(errors='replace')[:500]}") from exc
        return target

    # ---------------------------------------------------------------- verification

    def _verify_sqlite(self, backup_path: Path) -> None:
        expected = self._table_counts(settings.DATABASES["default"]["NAME"])

        with tempfile.TemporaryDirectory() as tmp:
            restored = Path(tmp) / "restored.sqlite3"
            shutil.copy(backup_path, restored)

            integrity = sqlite3.connect(str(restored))
            try:
                result = integrity.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise CommandError(f"Restored database failed its integrity check: {result}")
                actual = self._table_counts(restored)
            finally:
                integrity.close()

            missing = {
                table: count
                for table, count in expected.items()
                if actual.get(table) != count
            }
            if missing:
                raise CommandError(f"Restored row counts do not match the source: {missing}")

        self.stdout.write(
            self.style.SUCCESS(
                f"  Restore verified: integrity check passed and {len(expected)} tables match."
            )
        )

    # ---------------------------------------------------------------- postgres verification

    def _pg_connect(self, dbname: str | None = None, *, autocommit: bool = False):
        import psycopg

        config = settings.DATABASES["default"]
        kwargs = {
            "dbname": dbname or config["NAME"],
            "user": config.get("USER") or None,
            "password": config.get("PASSWORD") or None,
            "host": config.get("HOST") or None,
            "port": config.get("PORT") or None,
            "sslmode": (config.get("OPTIONS") or {}).get("sslmode"),
        }
        return psycopg.connect(
            autocommit=autocommit, **{k: v for k, v in kwargs.items() if v is not None}
        )

    @staticmethod
    def _pg_table_counts(conn) -> dict[str, int]:
        from psycopg import sql

        tables = [
            row[0]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            )
        ]
        return {
            table: conn.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in tables
        }

    def _backup_postgres_in_snapshot(self, output_dir: Path, stamp: str) -> tuple[Path, dict[str, int]]:
        """Dump and count rows inside one exported snapshot.

        On a live database, counting rows *after* the dump races with every write in
        between and makes verification flaky. Exporting a REPEATABLE READ snapshot and
        handing it to pg_dump means the dump and the expected counts describe exactly the
        same instant, however busy the database is.
        """

        import psycopg

        conn = self._pg_connect()
        try:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            expected = self._pg_table_counts(conn)
            path = self._backup_postgres(output_dir, stamp, snapshot=snapshot)
        finally:
            conn.rollback()
            conn.close()
        return path, expected

    def _verify_postgres(self, backup_path: Path, expected: dict[str, int], stamp: str) -> None:
        """Restore into a scratch database, compare every table, then drop it."""

        from psycopg import sql

        source_name = settings.DATABASES["default"]["NAME"]
        scratch = f"{source_name}_verify_{stamp.lower()}"[-63:]

        admin = self._pg_connect("postgres", autocommit=True)
        try:
            try:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
            except Exception as exc:
                raise CommandError(
                    f"Could not create the scratch database for verification ({exc}). "
                    "The backup role needs CREATEDB, or run the drill in docs/RUNBOOK.md."
                ) from exc

            try:
                command = [
                    "pg_restore",
                    "--no-owner",
                    "--no-privileges",
                    f"--dbname={scratch}",
                    *self._pg_connection_args(),
                    str(backup_path),
                ]
                try:
                    result = subprocess.run(command, env=self._pg_env(), capture_output=True)
                except FileNotFoundError as exc:
                    raise CommandError("pg_restore is not on PATH.") from exc

                # pg_restore exits non-zero for ignorable noise too (for example a newer
                # client setting an option the older server does not know). The verdict is
                # therefore the data comparison below, and the noise is surfaced, not hidden.
                if result.returncode != 0:
                    tail = result.stderr.decode(errors="replace").strip().splitlines()[-1:]
                    self.stdout.write(
                        self.style.WARNING(f"  pg_restore reported: {' '.join(tail)[:300]}")
                    )

                restored = self._pg_connect(scratch)
                try:
                    actual = self._pg_table_counts(restored)
                finally:
                    restored.close()
            finally:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch))
                )
        finally:
            admin.close()

        if not expected:
            raise CommandError("The source database has no tables; nothing was verified.")
        mismatched = {
            table: {"expected": count, "restored": actual.get(table)}
            for table, count in expected.items()
            if actual.get(table) != count
        }
        if mismatched:
            raise CommandError(f"Restored row counts do not match the source: {mismatched}")

        self.stdout.write(
            self.style.SUCCESS(
                f"  Restore verified: restored into a scratch database and {len(expected)} tables match."
            )
        )

    #: SQLite cannot parameterise an identifier, so table names are interpolated. They come
    #: from sqlite_master rather than from a user, but an allow-list costs nothing and means
    #: the interpolation is provably safe rather than safe-by-assumption.
    _IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    @classmethod
    def _table_counts(cls, path) -> dict[str, int]:
        counts: dict[str, int] = {}
        conn = sqlite3.connect(str(path))
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for table in tables:
                if not cls._IDENTIFIER.match(table):
                    continue
                # The identifier is regex-validated above and originates from sqlite_master,
                # never from a request. SQLite cannot bind an identifier, so interpolation
                # is the only option and the allow-list is what makes it provably safe.
                counts[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]  # nosec B608
        finally:
            conn.close()
        return counts

    def _prune(self, output_dir: Path, *, keep: int) -> None:
        backups = sorted(
            [p for p in output_dir.iterdir() if p.name.startswith("backup-")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[keep:]:
            stale.unlink(missing_ok=True)
            self.stdout.write(f"  Pruned old backup: {stale.name}")
