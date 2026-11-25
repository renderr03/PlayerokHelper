"""Parse seller input without fetching arbitrary URLs or guessing UUIDs."""

from __future__ import annotations

import re
import uuid
from urllib.parse import unquote, urlsplit

UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


def item_reference(value):
    value = value.strip()
    if not value or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_item_reference")
    try:
        return "id", str(uuid.UUID(value))
    except ValueError:
        pass
    url = urlsplit(value)
    if (
        url.scheme not in {"https", "http"}
        or url.hostname not in {"playerok.com", "www.playerok.com"}
        or url.username is not None
        or url.password is not None
        or url.port not in {None, 80, 443}
    ):
        raise ValueError("invalid_item_reference")
    parts = unquote(url.path, errors="strict").strip("/").split("/")
    if len(parts) != 2 or parts[0] != "products":
        raise ValueError("invalid_item_reference")
    slug = parts[1]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,255}", slug):
        raise ValueError("invalid_item_reference")
    match = re.match(rf"({UUID})(?:-|$)", slug)
    if match:
        return "id", str(uuid.UUID(match[1]))
    return "slug", slug
