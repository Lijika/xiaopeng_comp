from __future__ import annotations

import task4_consistency.web.app as web


def test_s17_openapi_contains_closed_export_contract() -> None:
    paths = web.app.openapi()["paths"]
    assert "/controlled/s17/api/exports/preview" in paths
    assert "/controlled/s17/api/exports/{request_id}/approve" in paths
    assert "/controlled/s17/api/exports/{request_id}/deny" in paths
    assert "/controlled/s17/api/exports/{request_id}/access" in paths
    assert "S17PreviewResponse" in web.app.openapi()["components"]["schemas"]


def test_s17_shell_alias_is_registered() -> None:
    paths = set(web.app.openapi()["paths"])
    assert {"/controlled/s17", "/controlled/s17/react"} <= paths
