"""URL prefix khi flood_model được gắn dưới /flood-model trên app chính."""

from __future__ import annotations

from flask import has_request_context, request


def public_path(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    prefix = ""
    if has_request_context() and (request.path or "").startswith("/flood-model"):
        prefix = "/flood-model"
    return prefix + path
