# PostgreSQL row-level security (optional)

Application tenancy is still the primary control (`OrganizationScopedViewSet`,
`TenantContext.scope`, adversarial tests). RLS is defence in depth.

## Enable

1. Apply policies once (as a role that owns the tables):

```bash
python manage.py enable_rls
```

2. Set in the environment:

```bash
ENABLE_POSTGRES_RLS=1
```

`TenantRLSMiddleware` then runs `SET LOCAL app.current_org_id` for the active workspace.
Staff/superuser sessions without a workspace set `app.bypass_rls=1` so the admin can still
operate cross-tenant.

## Disable

```bash
python manage.py enable_rls --remove
# and unset ENABLE_POSTGRES_RLS
```

## Limits

- Child tables without `organization_id` (subtasks, job attempts) are not policy-covered;
  they are only reachable through a parent that is.
- Table owners bypass RLS unless `FORCE ROW LEVEL SECURITY` is set — `enable_rls` forces it.
- SQLite and local tests ignore the setting.
