"""Hand-built OpenAPI 3.1 document.

Generated from the live router so the paths can never drift from the implementation, with
hand-written descriptions for the parts a generator cannot infer (auth model, tenancy
header, error envelope, concurrency semantics).
"""

from __future__ import annotations

from django.conf import settings
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

ERROR_SCHEMA = {
    "type": "object",
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {
                "code": {"type": "string", "example": "permission_denied"},
                "message": {"type": "string"},
                "request_id": {"type": "string"},
                "fields": {"type": "object", "additionalProperties": {"type": "array"}},
                "retry_after": {"type": "integer"},
            },
        }
    },
}

PAGINATED_SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer"},
        "page": {"type": "integer"},
        "pages": {"type": "integer"},
        "page_size": {"type": "integer"},
        "next": {"type": "string", "nullable": True},
        "previous": {"type": "string", "nullable": True},
        "results": {"type": "array", "items": {"type": "object"}},
    },
}

_COMMON_ERRORS = {
    "400": {"description": "Validation failed", "content": {"application/json": {"schema": ERROR_SCHEMA}}},
    "401": {"description": "Authentication required", "content": {"application/json": {"schema": ERROR_SCHEMA}}},
    "403": {"description": "Role does not permit this action", "content": {"application/json": {"schema": ERROR_SCHEMA}}},
    "404": {"description": "Not found, or not in your workspace", "content": {"application/json": {"schema": ERROR_SCHEMA}}},
    "409": {"description": "Version conflict or blocked state", "content": {"application/json": {"schema": ERROR_SCHEMA}}},
    "429": {"description": "Rate limited", "content": {"application/json": {"schema": ERROR_SCHEMA}}},
}


def build_schema(request=None) -> dict:
    from .urls import router

    paths: dict[str, dict] = {}

    for prefix, viewset, basename in router.registry:
        base_path = f"/api/v1/{prefix}/"
        tag = prefix.replace("-", " ").title()
        model_name = getattr(getattr(viewset, "serializer_class", None), "Meta", None)
        model_label = getattr(getattr(model_name, "model", None), "__name__", basename)

        methods = {name.lower() for name in getattr(viewset, "http_method_names", [])}
        collection: dict[str, dict] = {}
        detail: dict[str, dict] = {}

        if hasattr(viewset, "list") or issubclass(viewset, _readable()):
            collection["get"] = {
                "tags": [tag],
                "summary": f"List {model_label} records in the active workspace",
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    {"name": "page_size", "in": "query", "schema": {"type": "integer", "maximum": 200}},
                    {"name": "search", "in": "query", "schema": {"type": "string"}},
                    {"name": "ordering", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "Paginated results",
                        "content": {"application/json": {"schema": PAGINATED_SCHEMA}},
                    },
                    **_COMMON_ERRORS,
                },
            }
            detail["get"] = {
                "tags": [tag],
                "summary": f"Retrieve one {model_label}",
                "responses": {"200": {"description": "OK"}, **_COMMON_ERRORS},
            }

        if "post" in methods and hasattr(viewset, "create"):
            collection["post"] = {
                "tags": [tag],
                "summary": f"Create a {model_label}",
                "description": "`organization` is derived from your credentials and is never accepted in the body.",
                "responses": {"201": {"description": "Created"}, **_COMMON_ERRORS},
            }
        if "patch" in methods and hasattr(viewset, "partial_update"):
            detail["patch"] = {
                "tags": [tag],
                "summary": f"Update a {model_label}",
                "description": (
                    "Send `expected_version` to opt into optimistic concurrency; a mismatch "
                    "returns 409 instead of overwriting a concurrent edit."
                ),
                "responses": {"200": {"description": "Updated"}, **_COMMON_ERRORS},
            }
        if "delete" in methods and hasattr(viewset, "destroy"):
            detail["delete"] = {
                "tags": [tag],
                "summary": f"Delete (or archive) a {model_label}",
                "responses": {"204": {"description": "Deleted"}, **_COMMON_ERRORS},
            }

        if collection:
            paths[base_path] = collection
        if detail:
            paths[f"{base_path}{{id}}/"] = {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
                **detail,
            }

    paths["/api/v1/tasks/{id}/toggle/"] = {
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
        "post": {
            "tags": ["Tasks"],
            "summary": "Complete or reopen a task",
            "description": "Returns 409 when an incomplete dependency blocks completion.",
            "responses": {"200": {"description": "Updated task"}, **_COMMON_ERRORS},
        },
    }
    paths["/api/v1/tasks/{id}/move/"] = {
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
        "post": {
            "tags": ["Tasks"],
            "summary": "Move a task on the board",
            "responses": {"200": {"description": "Updated task"}, **_COMMON_ERRORS},
        },
    }
    paths["/api/v1/stats/"] = {
        "get": {
            "tags": ["Meta"],
            "summary": "Workspace counters",
            "responses": {"200": {"description": "Counters"}, **_COMMON_ERRORS},
        }
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Task Manager API",
            "version": "1.0.0",
            "description": (
                "Multi-tenant task management API.\n\n"
                "**Authentication** — send `Authorization: Api-Key <token>`, or use a session "
                "cookie from the web app.\n\n"
                "**Tenancy** — an API key is pinned to the workspace that issued it. Session "
                "callers select a workspace with the `X-Organization` header (id or slug); "
                "without it, the first workspace you belong to is used.\n\n"
                "**Errors** — every non-2xx response is `{\"error\": {\"code\", \"message\", "
                "\"request_id\"}}`. Quote `request_id` when reporting a problem.\n\n"
                "**Rate limits** — 429 responses carry a `Retry-After` header.\n\n"
                "**Concurrency** — task writes accept `expected_version`; a mismatch is 409."
            ),
        },
        "servers": [{"url": getattr(settings, "SITE_URL", "http://127.0.0.1:8000")}],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                    "description": "Format: `Api-Key <token>`",
                },
                "SessionAuth": {"type": "apiKey", "in": "cookie", "name": "sessionid"},
            },
            "schemas": {"Error": ERROR_SCHEMA, "PaginatedResponse": PAGINATED_SCHEMA},
            "parameters": {
                "Organization": {
                    "name": "X-Organization",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "Workspace id or slug. Ignored for API-key callers.",
                }
            },
        },
        "security": [{"ApiKeyAuth": []}, {"SessionAuth": []}],
        "paths": paths,
    }


def _readable():
    from rest_framework import viewsets

    return viewsets.ReadOnlyModelViewSet


@api_view(["GET"])
@permission_classes([AllowAny])
def openapi_schema(request):
    return Response(build_schema(request))
