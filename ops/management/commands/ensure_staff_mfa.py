"""List staff/superusers and whether they have two-step verification enrolled.

Does not enrol devices for you — that requires the authenticator app — but it tells an
operator who still needs to visit /account/security/ before STAFF_REQUIRE_MFA will let
them through.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Q

from identity.mfa import user_has_mfa


class Command(BaseCommand):
    help = "Report which staff accounts still need two-step verification."

    def handle(self, *args, **options):
        User = get_user_model()
        staff = (
            User.objects.filter(is_active=True)
            .filter(Q(is_staff=True) | Q(is_superuser=True))
            .order_by("username")
        )
        missing = []
        for user in staff:
            ok = user_has_mfa(user)
            mark = "ok" if ok else "NEEDS ENROLMENT"
            self.stdout.write(
                f"{user.username:<24} staff={user.is_staff} super={user.is_superuser}  {mark}"
            )
            if not ok:
                missing.append(user.username)

        if missing:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{len(missing)} account(s) must visit /account/security/ before "
                    "STAFF_REQUIRE_MFA will allow access."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nAll staff accounts have MFA enrolled."))
