"""S03 Reviewer lifecycle acceptance tests over a real HTTP connection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
import os
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest

from tests.test_s01_http import DEMO_CREDENTIAL, UvicornLoopback
from tests.test_s02_http import (
    CREDENTIAL,
    _configured_http_source,
    _open_session,
)


def _ready_review(
    server: UvicornLoopback,
    submission: dict[str, Any],
    cookie: str,
) -> tuple[str, dict[str, Any]]:
    admission = server.request(
        "POST",
        "/controlled/s02/api/commands/submit",
        body={"idempotency_key": "s03-http-intake", "submission": submission},
        headers={"Cookie": cookie},
        use_session=False,
    ).json()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = server.request(
            "GET",
            "/controlled/s02/api/queries/queue",
            headers={"Cookie": cookie},
            use_session=False,
        )
        item = next(
            (
                candidate
                for candidate in response.json()["items"]
                if candidate["application_id"] == admission["application_id"]
            ),
            None,
        )
        if item is not None:
            return admission["application_id"], item
        time.sleep(0.05)
    raise AssertionError("S03 review work item was not projected")


def _assert_no_store_and_no_raw(*responses: Any) -> None:
    for response in responses:
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["pragma"] == "no-cache"
        for forbidden in (
            "SAFE-VIN-A",
            "http-result-object",
            "http-page-object",
            "result.json",
            "page.png",
            CREDENTIAL,
        ):
            assert forbidden not in response.text


def create_expiring_s03_test_app() -> Any:
    import task4_consistency.web.app as web

    clock_path = Path(os.environ["TASK4_S03_TEST_SESSION_CLOCK_PATH"])
    web.S01_SESSION_CLOCK = lambda: float(clock_path.read_text(encoding="ascii"))
    web.S02_SESSION_TTL_SECONDS = int(
        os.environ["TASK4_S03_TEST_SESSION_TTL_SECONDS"]
    )
    return web.create_s02_test_app()


def _public_review_state(
    server: UvicornLoopback,
    *,
    cookie: str,
    application_id: str,
    work_item_id: str,
) -> dict[str, Any]:
    headers = {"Cookie": cookie}
    return {
        "queue": server.request(
            "GET",
            "/controlled/s02/api/queries/queue",
            headers=headers,
            use_session=False,
        ).json(),
        "work_item": server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json(),
        "workspace": server.request(
            "GET",
            f"/controlled/s02/api/queries/applications/{application_id}/workspace",
            headers=headers,
            use_session=False,
        ).json(),
    }


def test_reviewer_queries_claims_renews_and_releases_over_http(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        work_item = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        )
        expected_context = work_item.json()["command_context"]
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": expected_context},
            headers=headers,
            use_session=False,
        )
        workspace = server.request(
            "GET",
            f"/controlled/s02/api/queries/applications/{application_id}/workspace",
            headers=headers,
            use_session=False,
        )
        renewed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/renew",
            body={
                "expected_fence": claimed.json()["claim_fence"],
                "expected_context": expected_context,
                "idempotency_key": "s03-http-renew",
            },
            headers=headers,
            use_session=False,
        )
        released = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/release",
            body={
                "expected_fence": claimed.json()["claim_fence"],
                "expected_context": expected_context,
                "idempotency_key": "s03-http-release",
            },
            headers=headers,
            use_session=False,
        )
        observed = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        )

    assert work_item.status == 200
    assert work_item.json()["status"] == "unclaimed"
    assert work_item.json()["claim_subject"] is None
    assert work_item.json()["claim_fence"] == 0
    assert work_item.json()["claim_expires_at"] == 0
    assert claimed.status == 200
    assert claimed.json()["status"] == "claimed"
    assert claimed.json()["claim_fence"] == 1
    assert workspace.status == 200
    assert workspace.json()["work_item_id"] == work_item_id
    assert renewed.status == 200
    assert renewed.json()["status"] == "renewed"
    assert released.status == 200
    assert released.json()["status"] == "released"
    assert observed.status == 200
    assert observed.json()["status"] == "released"
    _assert_no_store_and_no_raw(
        work_item,
        claimed,
        workspace,
        renewed,
        released,
        observed,
    )
    json.dumps(observed.json(), sort_keys=True)


def test_structured_submit_is_minimized_atomic_and_idempotent_over_http(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        before = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": before["command_context"]},
            headers=headers,
            use_session=False,
        ).json()
        note = "SYNTHETIC-PRIVATE-NOTE:" + "n" * 1977
        verification = {
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {"finding_id": finding["finding_id"], "outcome": "confirmed"}
                for finding in before["automatic_findings"]
            ],
            "note": note,
        }
        command = {
            "expected_fence": claimed["claim_fence"],
            "expected_context": before["command_context"],
            "idempotency_key": "s03-http-single-submit",
            "verification": verification,
        }
        submitted = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body=command,
            headers=headers,
            use_session=False,
        )
        replay = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body=command,
            headers=headers,
            use_session=False,
        )
        conflict_command = json.loads(json.dumps(command))
        conflict_command["verification"]["note"] = "x" * 2000
        conflict = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body=conflict_command,
            headers=headers,
            use_session=False,
        )
        observed = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        )
        queue = server.request(
            "GET",
            "/controlled/s02/api/queries/queue",
            headers=headers,
            use_session=False,
        )

    assert submitted.status == 200
    assert submitted.json()["status"] == "accepted"
    assert replay.status == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["decision_id"] == submitted.json()["decision_id"]
    assert conflict.status == 409
    assert conflict.json()["detail"] == {
        "error": "S03_CONFLICT",
        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
    }
    assert observed.status == 200
    decision = observed.json()["decision"]
    assert observed.json()["status"] == "completed"
    assert decision["decision_id"] == submitted.json()["decision_id"]
    assert decision["reason_code"] == "HUMAN_REVIEW_COMPLETED"
    assert decision["note_metadata"] == {
        "present": True,
        "character_count": 2000,
        "byte_count": 2000,
        "sha256": hashlib.sha256(note.encode()).hexdigest(),
    }
    assert observed.json()["automatic_findings"] == before["automatic_findings"]
    assert observed.json()["run_authority"] == before["run_authority"]
    assert all(
        item["application_id"] != application_id for item in queue.json()["items"]
    )
    _assert_no_store_and_no_raw(submitted, replay, conflict, observed, queue)
    for response in (submitted, replay, conflict, observed, queue):
        assert note not in response.text


def test_batch_preview_is_read_only_and_submit_is_idempotent_over_http(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        _, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": initial["command_context"]},
            headers=headers,
            use_session=False,
        ).json()
        before_preview = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        items = [
            {
                "work_item_id": work_item_id,
                "finding_id": finding["finding_id"],
                "outcome": "confirmed",
                "reason_code": "HUMAN_REVIEW_COMPLETED",
                "expected_fence": claimed["claim_fence"],
                "expected_context": initial["command_context"],
            }
            for finding in initial["automatic_findings"]
        ]
        preview = server.request(
            "POST",
            "/controlled/s02/api/commands/review-batches/preview",
            body={"items": items},
            headers=headers,
            use_session=False,
        )
        after_preview = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        command = {
            "idempotency_key": "s03-http-batch-submit",
            "plan": preview.json(),
        }
        submitted = server.request(
            "POST",
            "/controlled/s02/api/commands/review-batches/submit",
            body=command,
            headers=headers,
            use_session=False,
        )
        replay = server.request(
            "POST",
            "/controlled/s02/api/commands/review-batches/submit",
            body=command,
            headers=headers,
            use_session=False,
        )
        conflict_command = json.loads(json.dumps(command))
        conflict_command["plan"]["items"][0]["outcome"] = "inconclusive"
        conflict = server.request(
            "POST",
            "/controlled/s02/api/commands/review-batches/submit",
            body=conflict_command,
            headers=headers,
            use_session=False,
        )
        observed = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        )

    assert preview.status == 200
    assert preview.json()["schema_version"] == "review-batch-plan/1"
    assert after_preview == before_preview
    assert submitted.status == 200
    assert submitted.json()["status"] == "accepted"
    assert len(submitted.json()["items"]) == len(initial["automatic_findings"])
    assert replay.status == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["items"] == submitted.json()["items"]
    assert conflict.status == 409
    assert conflict.json()["detail"] == {
        "error": "S03_CONFLICT",
        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
    }
    assert observed.status == 200
    assert observed.json()["status"] == "completed"
    assert len(observed.json()["decisions"]) == len(initial["automatic_findings"])
    _assert_no_store_and_no_raw(preview, submitted, replay, conflict, observed)


def test_two_reviewer_sessions_race_once_and_stale_fences_never_write(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        first_cookie = _open_session(server)
        second_cookie = _open_session(server)
        _, queue_item = _ready_review(server, submission, first_cookie)
        work_item_id = queue_item["work_item_id"]
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers={"Cookie": first_cookie},
            use_session=False,
        ).json()

        def claim(cookie: str) -> Any:
            return server.request(
                "POST",
                f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
                body={"expected_context": initial["command_context"]},
                headers={"Cookie": cookie},
                use_session=False,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            raced = list(pool.map(claim, (first_cookie, second_cookie)))
        accepted = next(response for response in raced if response.status == 200)
        rejected = next(response for response in raced if response.status == 409)
        headers = {"Cookie": first_cookie}
        before_stale = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        stale_fence = accepted.json()["claim_fence"] + 1
        fenced_body = {
            "expected_fence": stale_fence,
            "expected_context": initial["command_context"],
            "idempotency_key": "s03-http-stale-fence",
        }
        stale_renew = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/renew",
            body=fenced_body,
            headers=headers,
            use_session=False,
        )
        stale_release = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/release",
            body=fenced_body,
            headers=headers,
            use_session=False,
        )
        stale_submit = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body={
                **fenced_body,
                "idempotency_key": "s03-http-stale-submit",
                "verification": {
                    "schema_version": "human-decision/1",
                    "outcome": "confirmed",
                    "reason_code": "HUMAN_REVIEW_COMPLETED",
                    "finding_decisions": [
                        {
                            "finding_id": finding["finding_id"],
                            "outcome": "confirmed",
                        }
                        for finding in initial["automatic_findings"]
                    ],
                },
            },
            headers=headers,
            use_session=False,
        )
        after_stale = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()

    assert sorted(response.status for response in raced) == [200, 409]
    assert rejected.json()["detail"] == {
        "error": "S03_CONFLICT",
        "reason_code": "WORK_ITEM_ALREADY_CLAIMED",
    }
    for response in (stale_renew, stale_release, stale_submit):
        assert response.status == 409
        assert response.json()["detail"] == {
            "error": "S03_STALE",
            "reason_code": "STALE_WORK_ITEM_CLAIM",
        }
    assert after_stale == before_stale
    _assert_no_store_and_no_raw(*raced, stale_renew, stale_release, stale_submit)


def test_invalid_stale_and_oversized_commands_are_bounded_and_atomic(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": initial["command_context"]},
            headers=headers,
            use_session=False,
        ).json()

        def public_state() -> dict[str, Any]:
            return {
                "queue": server.request(
                    "GET",
                    "/controlled/s02/api/queries/queue",
                    headers=headers,
                    use_session=False,
                ).json(),
                "work_item": server.request(
                    "GET",
                    f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
                    headers=headers,
                    use_session=False,
                ).json(),
                "workspace": server.request(
                    "GET",
                    f"/controlled/s02/api/queries/applications/{application_id}/workspace",
                    headers=headers,
                    use_session=False,
                ).json(),
            }

        before = public_state()
        verification = {
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "NOT_ALLOWED_SYNTHETIC_REASON",
            "finding_decisions": [
                {"finding_id": finding["finding_id"], "outcome": "confirmed"}
                for finding in initial["automatic_findings"]
            ],
        }
        command = {
            "expected_fence": claimed["claim_fence"],
            "expected_context": initial["command_context"],
            "idempotency_key": "s03-http-invalid",
            "verification": verification,
        }
        invalid_reason = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body=command,
            headers=headers,
            use_session=False,
        )
        overflow_command = json.loads(json.dumps(command))
        overflow_command["idempotency_key"] = "s03-http-note-overflow"
        overflow_command["verification"]["reason_code"] = "HUMAN_REVIEW_COMPLETED"
        overflow_command["verification"]["note"] = "PRIVATE-NOTE-SENTINEL" + "x" * 2001
        note_overflow = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body=overflow_command,
            headers=headers,
            use_session=False,
        )
        invalid_fence = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/renew",
            body={
                "expected_fence": str(claimed["claim_fence"]),
                "expected_context": initial["command_context"],
                "idempotency_key": "s03-http-invalid-fence",
            },
            headers=headers,
            use_session=False,
        )
        missing_lifecycle_key = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/renew",
            body={
                "expected_fence": claimed["claim_fence"],
                "expected_context": initial["command_context"],
            },
            headers=headers,
            use_session=False,
        )
        stale_context = json.loads(json.dumps(initial["command_context"]))
        stale_context["current_context"] = "0" * 64
        stale = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/renew",
            body={
                "expected_fence": claimed["claim_fence"],
                "expected_context": stale_context,
                "idempotency_key": "s03-http-stale-context",
            },
            headers=headers,
            use_session=False,
        )
        oversized = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit",
            body={"padding": "RAW-BODY-SENTINEL" + "x" * (300 * 1024)},
            headers=headers,
            use_session=False,
        )
        after = public_state()

    for response in (
        invalid_reason,
        note_overflow,
        invalid_fence,
        missing_lifecycle_key,
    ):
        assert response.status == 422
        assert response.json()["detail"]["error"] == "S03_INVALID_COMMAND"
    assert stale.status == 409
    assert stale.json()["detail"] == {
        "error": "S03_STALE",
        "reason_code": "STALE_REVIEW_CONTEXT",
    }
    assert oversized.status == 413
    assert oversized.json()["detail"]["error"] == "S03_COMMAND_TOO_LARGE"
    assert after == before
    for response in (
        invalid_reason,
        note_overflow,
        invalid_fence,
        missing_lifecycle_key,
        stale,
        oversized,
    ):
        assert len(response.text) < 220
        assert "PRIVATE-NOTE-SENTINEL" not in response.text
        assert "RAW-BODY-SENTINEL" not in response.text
    _assert_no_store_and_no_raw(
        invalid_reason,
        note_overflow,
        invalid_fence,
        missing_lifecycle_key,
        stale,
        oversized,
    )


def test_missing_cross_scope_and_expired_sessions_hide_work_item_existence(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        valid_cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, valid_cookie)
        work_item_id = queue_item["work_item_id"]
        valid_headers = {"Cookie": valid_cookie}
        before = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=valid_headers,
            use_session=False,
        ).json()
        demo_session = server.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers={"Authorization": f"Bearer {DEMO_CREDENTIAL}"},
            use_session=False,
        )
        cookies = SimpleCookie()
        cookies.load(demo_session.headers["set-cookie"])
        cross_scope_cookie = f"s02_session={cookies['s01_session'].value}"

        hidden_responses = []
        for cookie in (None, cross_scope_cookie):
            headers = {} if cookie is None else {"Cookie": cookie}
            hidden_responses.extend(
                [
                    server.request(
                        "GET",
                        "/controlled/s02/api/queries/queue",
                        headers=headers,
                        use_session=False,
                    ),
                    server.request(
                        "GET",
                        f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
                        headers=headers,
                        use_session=False,
                    ),
                    server.request(
                        "GET",
                        f"/controlled/s02/api/queries/applications/{application_id}/workspace",
                        headers=headers,
                        use_session=False,
                    ),
                    server.request(
                        "POST",
                        f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
                        body={"expected_context": before["command_context"]},
                        headers=headers,
                        use_session=False,
                    ),
                    server.request(
                        "POST",
                        "/controlled/s02/api/commands/review-batches/preview",
                        body={"items": []},
                        headers=headers,
                        use_session=False,
                    ),
                ]
            )
        after = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=valid_headers,
            use_session=False,
        ).json()

    for offset in (0, 5):
        assert hidden_responses[offset].status == 200
        assert hidden_responses[offset].json() == {
            "items": [],
            "projection_watermark": 0,
        }
        assert [response.status for response in hidden_responses[offset + 1 : offset + 5]] == [
            404,
            404,
            404,
            404,
        ]
    assert after == before
    _assert_no_store_and_no_raw(*hidden_responses)

    expiry_root = tmp_path / "expiry"
    expiry_root.mkdir()
    expiring_environment, expiring_submission = _configured_http_source(expiry_root)
    clock_path = tmp_path / "s03-clock"
    clock_path.write_text("1000", encoding="ascii")
    expiring_environment.update(
        {
            "TASK4_S03_TEST_SESSION_CLOCK_PATH": str(clock_path),
            "TASK4_S03_TEST_SESSION_TTL_SECONDS": "2",
        }
    )
    with UvicornLoopback(
        expiring_environment,
        app_target="tests.test_s03_http:create_expiring_s03_test_app",
        app_factory=True,
    ) as server:
        expired_cookie = _open_session(server)
        _, expiring_item = _ready_review(server, expiring_submission, expired_cookie)
        expiring_work_item_id = expiring_item["work_item_id"]
        expired_before = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{expiring_work_item_id}",
            headers={"Cookie": expired_cookie},
            use_session=False,
        ).json()
        clock_path.write_text("1003", encoding="ascii")
        expired_queue = server.request(
            "GET",
            "/controlled/s02/api/queries/queue",
            headers={"Cookie": expired_cookie},
            use_session=False,
        )
        expired_query = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{expiring_work_item_id}",
            headers={"Cookie": expired_cookie},
            use_session=False,
        )
        expired_claim = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{expiring_work_item_id}/claim",
            body={"expected_context": expired_before["command_context"]},
            headers={"Cookie": expired_cookie},
            use_session=False,
        )
        fresh_cookie = _open_session(server)
        expired_after = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{expiring_work_item_id}",
            headers={"Cookie": fresh_cookie},
            use_session=False,
        ).json()

    assert expired_queue.status == 200
    assert expired_queue.json() == {"items": [], "projection_watermark": 0}
    assert expired_queue.headers["x-s02-access-ended"] == "1"
    assert expired_query.status == 404
    assert expired_query.json()["detail"]["error"] == "S03_NOT_FOUND"
    assert expired_claim.status == 404
    assert expired_claim.json()["detail"]["error"] == "S03_NOT_FOUND"
    assert expired_after == expired_before
    _assert_no_store_and_no_raw(expired_queue, expired_query, expired_claim)


@pytest.mark.parametrize("action", ("claim", "renew", "release", "submit"))
def test_review_audit_failure_is_unavailable_and_has_zero_business_side_effects(
    tmp_path: Path,
    action: str,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers={"Cookie": cookie},
            use_session=False,
        ).json()
        claimed = None
        if action != "claim":
            claimed = server.request(
                "POST",
                f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
                body={"expected_context": initial["command_context"]},
                headers={"Cookie": cookie},
                use_session=False,
            ).json()
        before = _public_review_state(
            server,
            cookie=cookie,
            application_id=application_id,
            work_item_id=work_item_id,
        )

    fault_environment = {
        **environment,
        "TASK4_S03_TEST_FAULT_POINT": "review.audit",
    }
    with UvicornLoopback(
        fault_environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        headers = {"Cookie": cookie}
        path = (
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/{action}"
        )
        if action == "claim":
            body = {"expected_context": initial["command_context"]}
        elif action in {"renew", "release"}:
            assert claimed is not None
            body = {
                "expected_fence": claimed["claim_fence"],
                "expected_context": initial["command_context"],
                "idempotency_key": f"s03-http-{action}-audit-failure",
            }
        else:
            assert claimed is not None
            body = {
                "expected_fence": claimed["claim_fence"],
                "expected_context": initial["command_context"],
                "idempotency_key": "s03-http-audit-failure",
                "verification": {
                    "schema_version": "human-decision/1",
                    "outcome": "confirmed",
                    "reason_code": "HUMAN_REVIEW_COMPLETED",
                    "finding_decisions": [
                        {
                            "finding_id": finding["finding_id"],
                            "outcome": "confirmed",
                        }
                        for finding in initial["automatic_findings"]
                    ],
                },
            }
        failed = server.request(
            "POST",
            path,
            body=body,
            headers=headers,
            use_session=False,
        )
        after = _public_review_state(
            server,
            cookie=cookie,
            application_id=application_id,
            work_item_id=work_item_id,
        )

    assert failed.status == 503
    assert failed.json()["detail"] == {
        "error": "S03_UNAVAILABLE",
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert after == before
    _assert_no_store_and_no_raw(failed)


def test_source_read_failure_stops_new_review_writes_without_partial_state(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        before = _public_review_state(
            server,
            cookie=cookie,
            application_id=application_id,
            work_item_id=work_item_id,
        )

    fault_environment = {
        **environment,
        "TASK4_S03_TEST_FAULT_POINT": "review.source_read",
    }
    with UvicornLoopback(
        fault_environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        stopped = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": before["work_item"]["command_context"]},
            headers={"Cookie": cookie},
            use_session=False,
        )
        after = _public_review_state(
            server,
            cookie=cookie,
            application_id=application_id,
            work_item_id=work_item_id,
        )

    assert stopped.status == 503
    assert stopped.json()["detail"] == {
        "error": "S03_STOPPED",
        "reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
    }
    assert after == before
    _assert_no_store_and_no_raw(stopped)


def test_lost_submit_response_replays_the_same_human_decision(tmp_path: Path) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        _, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": initial["command_context"]},
            headers=headers,
            use_session=False,
        ).json()
        command = {
            "expected_fence": claimed["claim_fence"],
            "expected_context": initial["command_context"],
            "idempotency_key": "s03-http-response-loss",
            "verification": {
                "schema_version": "human-decision/1",
                "outcome": "confirmed",
                "reason_code": "HUMAN_REVIEW_COMPLETED",
                "finding_decisions": [
                    {"finding_id": finding["finding_id"], "outcome": "confirmed"}
                    for finding in initial["automatic_findings"]
                ],
            },
        }
        path = (
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/submit"
        )
        server._session_cookie = cookie
        server.send_without_reading("POST", path, body=command, headers={})
        deadline = time.monotonic() + 5
        replay = None
        while time.monotonic() < deadline:
            candidate = server.request(
                "POST",
                path,
                body=command,
                headers=headers,
                use_session=False,
            )
            if candidate.status == 200 and candidate.json()["replayed"] is True:
                replay = candidate
                break
            time.sleep(0.05)
        assert replay is not None
        observed = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        )

    assert replay.status == 200
    assert replay.json()["status"] == "accepted"
    assert observed.status == 200
    assert observed.json()["status"] == "completed"
    assert len(observed.json()["decisions"]) == 1
    assert observed.json()["decision"]["decision_id"] == replay.json()["decision_id"]
    _assert_no_store_and_no_raw(replay, observed)


def test_lost_renew_and_release_responses_replay_the_same_commands(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        _, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": initial["command_context"]},
            headers=headers,
            use_session=False,
        ).json()
        server._session_cookie = cookie

        for action in ("renew", "release"):
            command = {
                "expected_fence": claimed["claim_fence"],
                "expected_context": initial["command_context"],
                "idempotency_key": f"s03-http-{action}-response-loss",
            }
            path = (
                f"/controlled/s02/api/commands/review-work-items/"
                f"{work_item_id}/{action}"
            )
            server.send_without_reading("POST", path, body=command, headers={})
            deadline = time.monotonic() + 5
            replay = None
            while time.monotonic() < deadline:
                candidate = server.request(
                    "POST",
                    path,
                    body=command,
                    headers=headers,
                    use_session=False,
                )
                if candidate.status == 200 and candidate.json().get("replayed") is True:
                    replay = candidate
                    break
                time.sleep(0.05)
            assert replay is not None
            assert replay.json()["status"] == (
                "renewed" if action == "renew" else "released"
            )


def test_batch_audit_failure_has_no_partial_decision_or_lifecycle_write(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        headers = {"Cookie": cookie}
        initial = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": initial["command_context"]},
            headers=headers,
            use_session=False,
        ).json()
        preview = server.request(
            "POST",
            "/controlled/s02/api/commands/review-batches/preview",
            body={
                "items": [
                    {
                        "work_item_id": work_item_id,
                        "finding_id": finding["finding_id"],
                        "outcome": "confirmed",
                        "reason_code": "HUMAN_REVIEW_COMPLETED",
                        "expected_fence": claimed["claim_fence"],
                        "expected_context": initial["command_context"],
                    }
                    for finding in initial["automatic_findings"]
                ]
            },
            headers=headers,
            use_session=False,
        )
        before = _public_review_state(
            server,
            cookie=cookie,
            application_id=application_id,
            work_item_id=work_item_id,
        )

    with UvicornLoopback(
        {**environment, "TASK4_S03_TEST_FAULT_POINT": "review.audit"},
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        failed = server.request(
            "POST",
            "/controlled/s02/api/commands/review-batches/submit",
            body={
                "idempotency_key": "s03-http-batch-audit-failure",
                "plan": preview.json(),
            },
            headers={"Cookie": cookie},
            use_session=False,
        )
        after = _public_review_state(
            server,
            cookie=cookie,
            application_id=application_id,
            work_item_id=work_item_id,
        )

    assert failed.status == 503
    assert failed.json()["detail"] == {
        "error": "S03_UNAVAILABLE",
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert after == before
    _assert_no_store_and_no_raw(preview, failed)
