"""Pagination classes.

The stock ``CursorPagination`` defaults to ``ordering = "-created"``, a field no model in
this project has, which made every list endpoint raise ``FieldError`` and return 500.
Both classes below pin an ordering that actually exists and cap the client-supplied size.
"""

from __future__ import annotations

from collections import OrderedDict

from rest_framework.pagination import CursorPagination, PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    """Offset pagination with an explicit, capped ``page_size``."""

    page_size_query_param = "page_size"
    max_page_size = 200

    def get_paginated_response(self, data):
        return Response(
            OrderedDict(
                [
                    ("count", self.page.paginator.count),
                    ("page", self.page.number),
                    ("pages", self.page.paginator.num_pages),
                    ("page_size", self.get_page_size(self.request)),
                    ("next", self.get_next_link()),
                    ("previous", self.get_previous_link()),
                    ("results", data),
                ]
            )
        )


class TimelineCursorPagination(CursorPagination):
    """Stable pagination for append-heavy feeds (activity, audit log, jobs).

    Offset pagination drifts on these because new rows land at the head between pages;
    a cursor on ``-created_at`` keeps a client's scroll position correct.
    """

    ordering = "-created_at"
    page_size_query_param = "page_size"
    max_page_size = 200
