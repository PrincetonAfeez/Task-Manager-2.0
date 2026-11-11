"""Tenant isolation.

These are the highest-value tests in the suite: a regression here is customer data
exposure, which is the one failure a SaaS product cannot recover from.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import PermissionDenied

from kernel.models import Membership, Organization
from kernel.roles import Permission, Role, can_assign_role, has_permission, permissions_for
from kernel.tenancy import NoOrganizationError, TenantContext, resolve_tenant, user_organizations
from tasks.models import Task

pytestmark = pytest.mark.django_db


class TestRoleMatrix:
    def test_owner_has_every_permission(self):
        assert permissions_for(Role.OWNER) == frozenset(
            p for p in permissions_for(Role.OWNER)
        )
        for permission in (
            Permission.TASK_VIEW,
            Permission.TASK_CREATE,
            Permission.MEMBER_MANAGE,
            Permission.ORG_DELETE,
            Permission.BILLING_MANAGE,
        ):
            assert has_permission(Role.OWNER, permission)

    def test_viewer_can_only_read(self):
        assert has_permission(Role.VIEWER, Permission.TASK_VIEW)
        assert has_permission(Role.VIEWER, Permission.AUTOMATION_VIEW)
        assert not has_permission(Role.VIEWER, Permission.TASK_CREATE)
        assert not has_permission(Role.VIEWER, Permission.TASK_EDIT)
        assert not has_permission(Role.VIEWER, Permission.COMMENT_CREATE)

    def test_member_cannot_administer(self):
        assert has_permission(Role.MEMBER, Permission.TASK_CREATE)
        assert not has_permission(Role.MEMBER, Permission.MEMBER_INVITE)
        assert not has_permission(Role.MEMBER, Permission.API_KEY_MANAGE)
        assert not has_permission(Role.MEMBER, Permission.ORG_SETTINGS)

    def test_admin_cannot_delete_workspace_or_manage_billing(self):
        assert has_permission(Role.ADMIN, Permission.ORG_SETTINGS)
        assert not has_permission(Role.ADMIN, Permission.ORG_DELETE)
        assert not has_permission(Role.ADMIN, Permission.BILLING_MANAGE)

    def test_unknown_role_and_permission_deny(self):
        assert not has_permission("wizard", Permission.TASK_VIEW)
        assert not has_permission(Role.OWNER, "task.teleport")
        assert not has_permission(None, Permission.TASK_VIEW)

    def test_admin_cannot_escalate_to_owner(self):
        assert can_assign_role(Role.ADMIN, Role.MEMBER)
        assert can_assign_role(Role.ADMIN, Role.VIEWER)
        # An admin promoting someone (or themselves) to owner or peer admin is escalation.
        assert not can_assign_role(Role.ADMIN, Role.OWNER)
        assert not can_assign_role(Role.ADMIN, Role.ADMIN)

    def test_owner_can_assign_anything(self):
        for role in (Role.OWNER, Role.ADMIN, Role.MEMBER, Role.VIEWER):
            assert can_assign_role(Role.OWNER, role)

    def test_member_cannot_assign_roles(self):
        assert not can_assign_role(Role.MEMBER, Role.VIEWER)
        assert not can_assign_role(Role.VIEWER, Role.VIEWER)


class TestTenantContext:
    def test_scope_filters_by_organization(self, organization, other_organization, task, other_task):
        tenant = TenantContext(organization=organization, role=Role.OWNER)
        scoped = tenant.scope(Task.objects.all())
        assert task in scoped
        assert other_task not in scoped

    def test_scope_of_organization_returns_only_self(self, organization, other_organization):
        tenant = TenantContext(organization=organization, role=Role.OWNER)
        scoped = tenant.scope(Organization.objects.all())
        assert list(scoped) == [organization]

    def test_scope_rejects_models_without_organization(self, organization):
        from django.contrib.auth.models import Permission as DjangoPermission

        tenant = TenantContext(organization=organization, role=Role.OWNER)
        with pytest.raises(TypeError):
            tenant.scope(DjangoPermission.objects.all())

    def test_require_raises_for_missing_permission(self, organization):
        tenant = TenantContext(organization=organization, role=Role.VIEWER)
        with pytest.raises(PermissionDenied):
            tenant.require(Permission.TASK_CREATE)
        tenant.require(Permission.TASK_VIEW)  # does not raise


class TestResolveTenant:
    def test_anonymous_request_raises(self, rf):
        from django.contrib.auth.models import AnonymousUser

        request = rf.get("/")
        request.user = AnonymousUser()
        with pytest.raises(NoOrganizationError):
            resolve_tenant(request)

    def test_user_without_membership_raises(self, rf, other_user):
        request = rf.get("/")
        request.user = other_user
        request.session = {}
        with pytest.raises(NoOrganizationError):
            resolve_tenant(request)

    def test_header_selects_membership_by_slug(self, rf, user, organization):
        second = Organization.objects.create(
            name="Second", slug="second", owner=user, timezone="UTC"
        )
        Membership.objects.create(organization=second, user=user, role=Role.OWNER)

        request = rf.get("/", headers={"X-Organization": "second"})
        request.user = user
        request.session = {}
        assert resolve_tenant(request).organization == second

    def test_header_for_foreign_org_falls_back_to_own(self, rf, user, organization, other_organization):
        """A header naming someone else's workspace must never grant access to it."""

        request = rf.get("/", headers={"X-Organization": other_organization.slug})
        request.user = user
        request.session = {}
        assert resolve_tenant(request).organization == organization

    def test_session_selection_is_honoured(self, rf, user, organization):
        from kernel.tenancy import SESSION_ORG_KEY

        request = rf.get("/")
        request.user = user
        request.session = {SESSION_ORG_KEY: organization.pk}
        assert resolve_tenant(request).organization == organization

    def test_soft_deleted_organization_is_not_resolvable(self, rf, user, organization):
        from django.utils import timezone

        Organization.objects.filter(pk=organization.pk).update(deleted_at=timezone.now())
        request = rf.get("/")
        request.user = user
        request.session = {}
        with pytest.raises(NoOrganizationError):
            resolve_tenant(request)

    def test_suspended_membership_is_not_resolvable(self, rf, user, organization):
        Membership.objects.filter(organization=organization, user=user).update(
            status=Membership.Status.SUSPENDED
        )
        request = rf.get("/")
        request.user = user
        request.session = {}
        with pytest.raises(NoOrganizationError):
            resolve_tenant(request)


class TestUserOrganizations:
    def test_lists_only_memberships(self, user, organization, other_organization):
        assert list(user_organizations(user)) == [organization]

    def test_anonymous_gets_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        assert list(user_organizations(AnonymousUser())) == []

    def test_excludes_soft_deleted(self, user, organization):
        from django.utils import timezone

        Organization.objects.filter(pk=organization.pk).update(deleted_at=timezone.now())
        assert list(user_organizations(user)) == []
