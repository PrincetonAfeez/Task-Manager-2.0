# Local stack profiles

Base stack (Postgres, Redis, web, workers):

```bash
cp .env.example .env   # set DJANGO_SECRET_KEY and POSTGRES_PASSWORD
docker compose up -d
```

Compose defaults `ENABLE_POSTGRES_RLS=1` and `STAFF_REQUIRE_MFA=1` for the app
containers. The migrate service runs `enable_rls` after migrations.

## Profiles

| Profile | What it adds | Typical env |
|---------|--------------|-------------|
| `scanning` | ClamAV | `ATTACHMENT_SCAN_BACKEND=clamav` |
| `storage` | MinIO (+ bucket init) | `MEDIA_BACKEND=s3` |
| `oidc` | Keycloak with a demo realm | configure SSO at `/settings/security/` |

Bring several up together:

```bash
ATTACHMENT_SCAN_BACKEND=clamav MEDIA_BACKEND=s3 \
  docker compose --profile scanning --profile storage --profile oidc up -d
```

### MinIO

- API: `http://localhost:9000`
- Console: `http://localhost:9001` (user/password from `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY`, defaults `minio` / `minio-secret-change-me`)
- Bucket: `task-manager` (created by `minio-init`)

### Keycloak (practice IdP only)

- Admin: `http://localhost:8080` — `admin` / `admin`
- Realm: `task-manager`
- Client: `task-manager-web` / secret `task-manager-dev-secret`
- Issuer URL to paste into workspace SSO settings:
  `http://localhost:8080/realms/task-manager`
- Test user: `alice@example.com` / `alice-password`
- Allowed domain in the app: `example.com`

This is **not** commercial IdP sign-off (Okta / Entra / Google). See `docs/PARKED.md`.

### Staff MFA checklist

```bash
python manage.py ensure_staff_mfa
```

Any account listed as `NEEDS ENROLMENT` must visit `/account/security/` while
`STAFF_REQUIRE_MFA=1`.
