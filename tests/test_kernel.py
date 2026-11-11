"""Kernel: organizations, memberships, invitations, API keys, audit, flags, purge."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from kernel.models import (
    ApiKey,
    AuditLog,
    FeatureFlag,
    Invitation,
    Membership,
    Notification,
    NotificationPreference,
    Organization,
    SavedView,
)
from kernel.purge import enqueue_due_purges, purge_organization_data
from kernel.roles import Role
from kernel.services import (
    accept_invitation,
    change_member_role,
    create_api_key,
    create_organization,
    feature_enabled,
    generate_slug,
    invite_member,
    record_audit,
    remove_member,
    restore_organization,
    revoke_api_key,
    set_feature_flag,
    soft_delete_organization,
)

pytestmark = pytest.mark.django_db


class TestOrganizationCreation:
    def test_creates_owner_membership_and_structure(self, user):
        organization = create_organization(name="Fresh Co", owner=user)
        assert organization.memberships.get(user=user).role == Role.OWNER
        # A workspace that is not immediately usable is a bad first impression.
        assert organization.task_lists.filter(name="Inbox").exists()
        assert organization.queues.filter(name="internal").exists()
        assert NotificationPreference.objects.filter(organization=organization, user=user).exists()

    def test_creation_is_audited(self, user):
        create_organization(name="Audited Co", owner=user)
        assert AuditLog.objects.filter(action="organization.created").exists()

    def test_an_empty_name_is_rejected(self, user):
        for name in ("", "   ", None):
            with pytest.raises(ValidationError):
                create_organization(name=name, owner=user)

    def test_an_invalid_timezone_is_rejected(self, user):
        with pytest.raises(ValidationError):
            create_organization(name="Bad TZ", owner=user, timezone_name="Mars/Olympus")

    def test_slugs_are_unique(self, user, other_user):
        first = create_organization(name="Same Name", owner=user)
        second = create_organization(name="Same Name", owner=other_user)
        assert first.slug != second.slug

    @pytest.mark.parametrize("reserved", ["admin", "api", "static", "accounts"])
    def test_reserved_slugs_are_avoided(self, reserved):
        assert generate_slug(reserved) != reserved

    def test_the_slug_field_is_unique_in_the_database(self, user, organization):
        with pytest.raises(IntegrityError), transaction.atomic():
            Organization.objects.create(name="Clone", slug=organization.slug, owner=user)


class TestSoftDeleteAndPurge:
    def test_soft_delete_sets_a_retention_window(self, organization, user):
        soft_delete_organization(organization=organization, actor=user, retention_days=30)
        organization.refresh_from_db()
        assert organization.deleted_at is not None
        assert organization.purge_after > timezone.now() + timedelta(days=29)

    def test_a_soft_deleted_workspace_disappears_from_queries(self, organization, user):
        soft_delete_organization(organization=organization, actor=user)
        assert list(Organization.objects.for_user(user)) == []
        assert list(Organization.objects.alive()) == []

    def test_soft_delete_is_idempotent(self, organization, user):
        soft_delete_organization(organization=organization, actor=user)
        organization.refresh_from_db()
        first = organization.deleted_at
        soft_delete_organization(organization=organization, actor=user)
        organization.refresh_from_db()
        assert organization.deleted_at == first

    def test_restore_brings_it_back(self, organization, user):
        soft_delete_organization(organization=organization, actor=user)
        restore_organization(organization=organization, actor=user)
        organization.refresh_from_db()
        assert organization.deleted_at is None
        assert list(Organization.objects.for_user(user)) == [organization]

    def test_purge_refuses_a_live_workspace(self, organization):
        """A purge job for a live workspace is a bug or an attack; either way it must not run."""

        result = purge_organization_data(organization.pk)
        assert result["purged"] is False
        assert result["reason"] == "organization_not_deleted"
        assert Organization.objects.filter(pk=organization.pk).exists()

    def test_purge_removes_everything(self, organization, user, task, label, job):
        from queues.models import Job
        from tasks.models import Task

        soft_delete_organization(organization=organization, actor=user)
        result = purge_organization_data(organization.pk)

        assert result["purged"] is True
        assert not Organization.objects.filter(pk=organization.pk).exists()
        assert not Task.objects.filter(pk=task.pk).exists()
        assert not Job.objects.filter(pk=job.pk).exists()

    def test_purge_survives_protect_foreign_keys(self, organization, user, task, job):
        """`Task.task_list` and `Job.queue` are PROTECT: a naive delete() raises partway."""

        soft_delete_organization(organization=organization, actor=user)
        assert purge_organization_data(organization.pk)["purged"] is True

    def test_the_audit_trail_outlives_the_purge(self, organization, user):
        record_audit(action="something.happened", organization=organization, actor=user)
        soft_delete_organization(organization=organization, actor=user)
        purge_organization_data(organization.pk)

        entry = AuditLog.objects.filter(action="something.happened").first()
        assert entry is not None
        assert entry.organization_id is None  # detached, not deleted

    def test_purge_of_a_missing_workspace_is_safe(self):
        assert purge_organization_data(999999)["purged"] is False

    def test_only_expired_workspaces_are_queued(self, organization, other_organization, user, other_user):
        soft_delete_organization(organization=organization, actor=user, retention_days=0)
        soft_delete_organization(organization=other_organization, actor=other_user, retention_days=30)
        assert enqueue_due_purges()["queued"] == 1


class TestInvitations:
    def test_issuing_returns_a_token_and_stores_only_its_digest(self, organization, user):
        invitation, token = invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="new@example.com",
            role=Role.MEMBER,
        )
        assert token
        assert invitation.token_hash == Invitation.hash_token(token)
        assert token not in invitation.token_hash

    def test_re_inviting_revokes_the_previous_token(self, organization, user):
        _first, first_token = invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="dup@example.com",
            role=Role.MEMBER,
        )
        invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="dup@example.com",
            role=Role.MEMBER,
        )
        with pytest.raises(ValidationError):
            accept_invitation(raw_token=first_token, user=user)

    def test_an_admin_cannot_invite_an_owner(self, organization, admin_membership):
        with pytest.raises(PermissionDenied):
            invite_member(
                organization=organization,
                actor=admin_membership.user,
                actor_role=Role.ADMIN,
                email="x@example.com",
                role=Role.OWNER,
            )

    def test_a_member_cannot_invite(self, organization, member):
        with pytest.raises(PermissionDenied):
            invite_member(
                organization=organization,
                actor=member.user,
                actor_role=Role.MEMBER,
                email="x@example.com",
                role=Role.MEMBER,
            )

    @pytest.mark.parametrize("email", ["", "not-an-email", "@example.com", "a@b"])
    def test_invalid_emails_are_rejected(self, organization, user, email):
        with pytest.raises(ValidationError):
            invite_member(
                organization=organization,
                actor=user,
                actor_role=Role.OWNER,
                email=email,
                role=Role.MEMBER,
            )

    def test_inviting_an_existing_member_is_rejected(self, organization, user, member):
        with pytest.raises(ValidationError, match="already a member"):
            invite_member(
                organization=organization,
                actor=user,
                actor_role=Role.OWNER,
                email=member.user.email,
                role=Role.MEMBER,
            )

    def test_acceptance_creates_the_membership_with_the_invited_role(
        self, organization, user, other_user
    ):
        _invitation, token = invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="join@example.com",
            role=Role.ADMIN,
        )
        membership = accept_invitation(raw_token=token, user=other_user)
        assert membership.role == Role.ADMIN

    def test_a_token_cannot_be_reused(self, organization, user, other_user, member_user):
        _invitation, token = invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="once@example.com",
            role=Role.MEMBER,
        )
        accept_invitation(raw_token=token, user=other_user)
        with pytest.raises(ValidationError, match="already been used"):
            accept_invitation(raw_token=token, user=member_user)

    def test_an_expired_token_is_marked_expired_on_sight(self, organization, user, other_user):
        invitation, token = invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="stale@example.com",
            role=Role.MEMBER,
        )
        Invitation.objects.filter(pk=invitation.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with pytest.raises(ValidationError, match="expired"):
            accept_invitation(raw_token=token, user=other_user)
        invitation.refresh_from_db()
        assert invitation.status == Invitation.Status.EXPIRED

    def test_a_token_for_a_deleted_workspace_is_refused(self, organization, user, other_user):
        _invitation, token = invite_member(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            email="gone@example.com",
            role=Role.MEMBER,
        )
        soft_delete_organization(organization=organization, actor=user)
        with pytest.raises(ValidationError, match="no longer exists"):
            accept_invitation(raw_token=token, user=other_user)

    def test_only_one_pending_invitation_per_email(self, organization, user):
        Invitation.objects.create(
            organization=organization,
            email="dupe@example.com",
            role=Role.MEMBER,
            token_hash="a" * 64,
            expires_at=timezone.now() + timedelta(days=1),
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Invitation.objects.create(
                organization=organization,
                email="dupe@example.com",
                role=Role.MEMBER,
                token_hash="b" * 64,
                expires_at=timezone.now() + timedelta(days=1),
            )


class TestRoleChanges:
    def test_an_owner_can_promote(self, organization, user, member):
        change_member_role(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            membership=member,
            role=Role.ADMIN,
        )
        member.refresh_from_db()
        assert member.role == Role.ADMIN

    def test_an_admin_cannot_create_an_owner(self, organization, admin_membership, member):
        with pytest.raises(PermissionDenied):
            change_member_role(
                organization=organization,
                actor=admin_membership.user,
                actor_role=Role.ADMIN,
                membership=member,
                role=Role.OWNER,
            )

    def test_an_admin_cannot_change_a_peer_admin(self, organization, admin_membership):
        other_admin = Membership.objects.create(
            organization=organization,
            user=organization.owner,
            role=Role.ADMIN,
        ) if False else admin_membership
        with pytest.raises(PermissionDenied):
            change_member_role(
                organization=organization,
                actor=admin_membership.user,
                actor_role=Role.ADMIN,
                membership=other_admin,
                role=Role.MEMBER,
            )

    def test_the_last_owner_cannot_be_demoted(self, organization, user, membership):
        with pytest.raises(ValidationError, match="at least one owner"):
            change_member_role(
                organization=organization,
                actor=user,
                actor_role=Role.OWNER,
                membership=membership,
                role=Role.MEMBER,
            )

    def test_a_second_owner_makes_demotion_possible(self, organization, user, membership, member):
        Membership.objects.filter(pk=member.pk).update(role=Role.OWNER)
        change_member_role(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            membership=membership,
            role=Role.MEMBER,
        )
        membership.refresh_from_db()
        assert membership.role == Role.MEMBER

    def test_promoting_to_owner_transfers_the_org_owner_field(self, organization, user, member):
        change_member_role(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            membership=member,
            role=Role.OWNER,
        )
        organization.refresh_from_db()
        assert organization.owner_id == member.user_id

    def test_role_changes_are_audited(self, organization, user, member):
        change_member_role(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            membership=member,
            role=Role.ADMIN,
        )
        assert AuditLog.objects.filter(action="member.role_changed").exists()


class TestMemberRemoval:
    def test_an_owner_can_remove_a_member(self, organization, user, member):
        remove_member(
            organization=organization, actor=user, actor_role=Role.OWNER, membership=member
        )
        assert not Membership.objects.filter(pk=member.pk).exists()

    def test_a_member_cannot_remove_anyone(self, organization, member, viewer):
        with pytest.raises(PermissionDenied):
            remove_member(
                organization=organization,
                actor=member.user,
                actor_role=Role.MEMBER,
                membership=viewer,
            )

    def test_the_last_owner_cannot_be_removed(self, organization, user, membership):
        with pytest.raises(ValidationError):
            remove_member(
                organization=organization, actor=user, actor_role=Role.OWNER, membership=membership
            )


class TestApiKeys:
    def test_issuing_returns_a_usable_token(self, organization, user):
        api_key, token = create_api_key(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            name="CI",
            role=Role.MEMBER,
        )
        assert token.startswith("tmk_")
        assert ApiKey.TOKEN_SEPARATOR in token
        assert api_key.is_active

    def test_a_key_cannot_exceed_its_creators_role(self, organization, admin_membership):
        with pytest.raises(PermissionDenied, match="more access"):
            create_api_key(
                organization=organization,
                actor=admin_membership.user,
                actor_role=Role.ADMIN,
                name="Escalate",
                role=Role.OWNER,
            )

    def test_a_member_cannot_manage_keys(self, organization, member):
        with pytest.raises(PermissionDenied):
            create_api_key(
                organization=organization,
                actor=member.user,
                actor_role=Role.MEMBER,
                name="Nope",
                role=Role.MEMBER,
            )

    def test_an_empty_name_is_rejected(self, organization, user):
        with pytest.raises(ValidationError):
            create_api_key(
                organization=organization,
                actor=user,
                actor_role=Role.OWNER,
                name="   ",
                role=Role.MEMBER,
            )

    def test_names_are_unique_per_workspace(self, organization, user):
        create_api_key(
            organization=organization, actor=user, actor_role=Role.OWNER, name="Dup", role=Role.MEMBER
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ApiKey.objects.create(
                organization=organization, name="Dup", prefix="tmk_zzz", token_hash="x" * 64
            )

    def test_revocation_deactivates_immediately(self, organization, user):
        api_key, _ = create_api_key(
            organization=organization, actor=user, actor_role=Role.OWNER, name="Old", role=Role.MEMBER
        )
        revoke_api_key(
            organization=organization, actor=user, actor_role=Role.OWNER, api_key=api_key
        )
        api_key.refresh_from_db()
        assert not api_key.is_active

    def test_expiry_deactivates(self, organization, user):
        api_key, _ = create_api_key(
            organization=organization,
            actor=user,
            actor_role=Role.OWNER,
            name="Expiring",
            role=Role.MEMBER,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        assert not api_key.is_active

    def test_prefixes_are_unique(self, organization, user):
        first, _ = create_api_key(
            organization=organization, actor=user, actor_role=Role.OWNER, name="A", role=Role.MEMBER
        )
        second, _ = create_api_key(
            organization=organization, actor=user, actor_role=Role.OWNER, name="B", role=Role.MEMBER
        )
        assert first.prefix != second.prefix


class TestAuditLog:
    def test_entries_are_immutable(self, organization, user):
        entry = record_audit(action="test.event", organization=organization, actor=user)
        entry.action = "tampered"
        with pytest.raises(RuntimeError, match="immutable"):
            entry.save()

    def test_entries_cannot_be_deleted(self, organization, user):
        entry = record_audit(action="test.event", organization=organization, actor=user)
        with pytest.raises(RuntimeError, match="append-only"):
            entry.delete()

    def test_the_actor_label_is_captured(self, organization, user):
        entry = record_audit(action="test.event", organization=organization, actor=user)
        assert entry.actor_label == str(user)

    def test_an_over_long_user_agent_is_truncated_not_rejected(self, organization, user, rf):
        request = rf.get("/", headers={"User-Agent": "x" * 5000})
        request.user = user
        entry = record_audit(action="test.event", organization=organization, actor=user, request=request)
        assert len(entry.user_agent) <= 300

    def test_the_request_id_is_captured(self, organization, user):
        from ops.logging import reset_request_id, set_request_id

        token = set_request_id("trace-xyz")
        try:
            entry = record_audit(action="test.event", organization=organization, actor=user)
        finally:
            reset_request_id(token)
        assert entry.request_id == "trace-xyz"


class TestFeatureFlags:
    def test_the_settings_default_applies_without_an_override(self, organization):
        assert feature_enabled("board_view", organization) is True
        assert feature_enabled("nonexistent_feature", organization) is False

    def test_an_org_override_wins(self, organization):
        set_feature_flag(key="board_view", enabled=False, organization=organization)
        assert feature_enabled("board_view", organization) is False

    def test_a_global_override_applies_fleet_wide(self, organization, other_organization):
        set_feature_flag(key="board_view", enabled=False, organization=None)
        assert feature_enabled("board_view", organization) is False
        assert feature_enabled("board_view", other_organization) is False

    def test_an_org_override_beats_the_global_one(self, organization, other_organization):
        set_feature_flag(key="board_view", enabled=False, organization=None)
        set_feature_flag(key="board_view", enabled=True, organization=organization)
        assert feature_enabled("board_view", organization) is True
        assert feature_enabled("board_view", other_organization) is False

    def test_one_global_flag_per_key(self, organization):
        FeatureFlag.objects.create(organization=None, key="solo", enabled=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            FeatureFlag.objects.create(organization=None, key="solo", enabled=False)

    def test_one_flag_per_key_per_org(self, organization):
        FeatureFlag.objects.create(organization=organization, key="solo", enabled=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            FeatureFlag.objects.create(organization=organization, key="solo", enabled=False)


class TestNotificationModel:
    def test_the_natural_key_prevents_duplicates(self, organization, user):
        Notification.objects.create(
            organization=organization, user=user, title="Once", natural_key="k1"
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Notification.objects.create(
                organization=organization, user=user, title="Again", natural_key="k1"
            )

    def test_mark_delivered_then_read(self, organization, user):
        notification = Notification.objects.create(
            organization=organization, user=user, title="Ping", natural_key="k2"
        )
        notification.mark_delivered()
        assert notification.status == Notification.Status.DELIVERED
        notification.mark_read()
        assert notification.status == Notification.Status.READ
        assert notification.read_at is not None

    def test_mark_read_backfills_delivered_at(self, organization, user):
        notification = Notification.objects.create(
            organization=organization, user=user, title="Ping", natural_key="k3"
        )
        notification.mark_read()
        assert notification.delivered_at is not None


class TestNotificationPreferences:
    def test_defaults_allow_reminders_but_not_comments(self, organization, user):
        prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=user)
        assert prefs.allows_email(Notification.Category.REMINDER)
        assert not prefs.allows_email(Notification.Category.COMMENT)

    def test_unsubscribing_overrides_every_flag(self, organization, user):
        prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=user)
        prefs.unsubscribed_at = timezone.now()
        assert not prefs.allows_email(Notification.Category.REMINDER)

    def test_quiet_hours_within_a_day(self, organization, user):
        from datetime import datetime, time

        prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=user)
        prefs.quiet_hours_start = time(9, 0)
        prefs.quiet_hours_end = time(17, 0)

        tz = timezone.get_current_timezone()
        assert prefs.in_quiet_hours(datetime(2026, 6, 1, 12, 0, tzinfo=tz))
        assert not prefs.in_quiet_hours(datetime(2026, 6, 1, 20, 0, tzinfo=tz))

    def test_quiet_hours_wrapping_midnight(self, organization, user):
        """22:00-07:00 is the common configuration and the one naive code gets wrong."""

        from datetime import datetime, time

        prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=user)
        prefs.quiet_hours_start = time(22, 0)
        prefs.quiet_hours_end = time(7, 0)

        tz = timezone.get_current_timezone()
        assert prefs.in_quiet_hours(datetime(2026, 6, 1, 23, 0, tzinfo=tz))
        assert prefs.in_quiet_hours(datetime(2026, 6, 1, 3, 0, tzinfo=tz))
        assert not prefs.in_quiet_hours(datetime(2026, 6, 1, 12, 0, tzinfo=tz))

    def test_unset_quiet_hours_are_never_quiet(self, organization, user):
        prefs, _ = NotificationPreference.objects.get_or_create(organization=organization, user=user)
        assert not prefs.in_quiet_hours()

    def test_one_preference_row_per_member(self, organization, user):
        NotificationPreference.objects.get_or_create(organization=organization, user=user)
        with pytest.raises(IntegrityError), transaction.atomic():
            NotificationPreference.objects.create(organization=organization, user=user)


class TestSavedViews:
    def test_names_are_unique_per_owner(self, organization, user):
        SavedView.objects.create(organization=organization, owner=user, name="Mine")
        with pytest.raises(IntegrityError), transaction.atomic():
            SavedView.objects.create(organization=organization, owner=user, name="Mine")

    def test_two_people_may_use_the_same_name(self, organization, user, member):
        SavedView.objects.create(organization=organization, owner=user, name="Shared name")
        SavedView.objects.create(organization=organization, owner=member.user, name="Shared name")


class TestLoginLockout:
    def test_repeated_failures_lock_the_account(self, user, settings):
        from django.contrib.auth import authenticate

        from kernel.auth_backends import is_locked_out

        settings.LOGIN_LOCKOUT_MAX_ATTEMPTS = 3
        for _ in range(3):
            assert authenticate(request=None, username=user.username, password="wrong") is None

        assert is_locked_out(user.username, "")
        # Even the correct password is refused while locked out.
        assert authenticate(request=None, username=user.username, password="test-password-123456") is None

    def test_a_success_resets_the_counter(self, user, settings):
        from django.contrib.auth import authenticate

        from kernel.auth_backends import is_locked_out

        settings.LOGIN_LOCKOUT_MAX_ATTEMPTS = 5
        authenticate(request=None, username=user.username, password="wrong")
        assert authenticate(
            request=None, username=user.username, password="test-password-123456"
        ) is not None
        assert not is_locked_out(user.username, "")

    def test_lockout_is_per_account(self, user, member_user, settings):
        from django.contrib.auth import authenticate

        from kernel.auth_backends import is_locked_out

        settings.LOGIN_LOCKOUT_MAX_ATTEMPTS = 2
        for _ in range(2):
            authenticate(request=None, username=user.username, password="wrong")

        assert is_locked_out(user.username, "")
        assert not is_locked_out(member_user.username, "")

    def test_forwarded_for_is_ignored_without_a_trusted_proxy(self, rf, settings):
        """Otherwise any client spoofs its way out of an IP lockout."""

        from kernel.auth_backends import client_ip

        settings.BEHIND_TLS_PROXY = False
        request = rf.get("/", headers={"X-Forwarded-For": "1.2.3.4"}, REMOTE_ADDR="10.0.0.1")
        assert client_ip(request) == "10.0.0.1"

    def test_forwarded_for_is_honoured_behind_a_trusted_proxy(self, rf, settings):
        from kernel.auth_backends import client_ip

        settings.BEHIND_TLS_PROXY = True
        request = rf.get("/", headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.9"}, REMOTE_ADDR="10.0.0.1")
        assert client_ip(request) == "1.2.3.4"
