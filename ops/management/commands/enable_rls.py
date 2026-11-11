"""Install or remove PostgreSQL row-level security policies for tenant tables.

Usage::

    python manage.py enable_rls
    python manage.py enable_rls --remove

Requires PostgreSQL. Pair with ``ENABLE_POSTGRES_RLS=1`` so requests SET LOCAL the
tenant id. Table owners (the usual Django migrate role) bypass RLS unless FORCE is set;
this command uses FORCE ROW LEVEL SECURITY so the app role is actually constrained.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from kernel.rls import TENANT_TABLES

POLICY_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE schemaname = 'public' AND tablename = '{table}'
      AND policyname = 'tenant_isolation'
  ) THEN
    EXECUTE 'CREATE POLICY tenant_isolation ON {table}
      FOR ALL
      USING (
        current_setting(''app.bypass_rls'', true) = ''1''
        OR organization_id = NULLIF(current_setting(''app.current_org_id'', true), '''')::integer
      )
      WITH CHECK (
        current_setting(''app.bypass_rls'', true) = ''1''
        OR organization_id = NULLIF(current_setting(''app.current_org_id'', true), '''')::integer
      )';
  END IF;
END $$;
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
"""

REMOVE_SQL = """
DROP POLICY IF EXISTS tenant_isolation ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


class Command(BaseCommand):
    help = "Enable or remove Postgres RLS policies on tenant-scoped tables."

    def add_arguments(self, parser):
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Drop tenant_isolation policies and disable RLS on those tables.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("enable_rls only applies to PostgreSQL.")

        existing = set(connection.introspection.table_names())

        applied = 0
        for table in TENANT_TABLES:
            if table not in existing:
                self.stdout.write(self.style.WARNING(f"skip missing table {table}"))
                continue
            sql = REMOVE_SQL if options["remove"] else POLICY_SQL
            with connection.cursor() as cursor:
                cursor.execute(sql.format(table=table))
            applied += 1
            action = "removed" if options["remove"] else "enabled"
            self.stdout.write(f"{action} RLS on {table}")

        self.stdout.write(self.style.SUCCESS(f"Done ({applied} tables). Set ENABLE_POSTGRES_RLS=1."))
