"""Route blueprints. Views parse requests and render; business rules live in services/."""
from __future__ import annotations

from flask import flash, request

from ..services.errors import ServiceError


def form_errors(exc: ServiceError) -> dict:
    return {exc.field or "_": exc.message}


def flash_error(exc: ServiceError) -> None:
    flash(exc.message, "error")


def form_dict() -> dict:
    return {k: v for k, v in request.form.items()}


def to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
