"""List containment matcher."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ListOutcome:
    match: bool
    needle: str | None
    haystack: list[str]
    message: str = ""


def as_list(value: str | list | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value).strip()
    if not text:
        return []
    # Support separators: | ; , 、 /
    for sep in ["|", ";", "、", "/", ","]:
        if sep in text:
            return [p.strip() for p in text.split(sep) if p.strip()]
    return [text]


# backward-compatible alias
_as_list = as_list


def list_contains(
    container: str | list | None,
    item: str | None,
    *,
    reverse: bool = False,
    normalize_item: Callable[[str], str | None] | None = None,
) -> ListOutcome:
    """Check that item is contained in container list.

    If normalize_item is provided, both haystack elements and needle are normalized
    with the same function before membership test (fixes plate_list vs plate_no).
    """
    hay = as_list(container)
    needle = None if item is None else str(item).strip()
    if reverse:
        hay = as_list(item)
        needle = None if container is None else str(container).strip()

    if normalize_item is not None:
        hay_n: list[str] = []
        for h in hay:
            nh = normalize_item(h)
            if nh:
                hay_n.append(nh)
        hay = hay_n
        if needle is not None:
            needle = normalize_item(needle)

    if needle is None or needle == "":
        return ListOutcome(match=False, needle=needle, haystack=hay, message="missing item")
    if not hay:
        return ListOutcome(match=False, needle=needle, haystack=hay, message="empty list")
    ok = needle in hay
    return ListOutcome(
        match=ok,
        needle=needle,
        haystack=hay,
        message="contained" if ok else f"{needle} not in {hay}",
    )
