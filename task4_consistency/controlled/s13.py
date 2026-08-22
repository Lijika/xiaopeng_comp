"""S13 downstream-delivery obligation, registry, and adapter seam.

Lifecycle owns completion + obligation; the adapter owns transport only.
The downstream process owns the loan decision.  Two real adapter
implementations (in-memory deterministic and institutional controlled)
share one conformance surface.

No adapter here performs network I/O; the controlled adapter simulates an
institution endpoint deterministically for test conformance.  Both are
registered via :class:`RegisteredDownstreamRegistry`.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Closed vocabularies — S13 only, kept intentionally small
# ---------------------------------------------------------------------------

S13_PAYLOAD_SCHEMA = "s13-route-payload/1"
S13_OBLIGATION_SCHEMA = "s13-delivery-obligation/1"
S13_DEFAULT_COMPENSATION_POLICY_ID = "c-demo-forward-compensation/1"
S13_DEFAULT_COMPENSATION_POLICY_VERSION = "1"

OBLIGATION_STATUSES = frozenset(
    {
        "pending",
        "claimed",
        "sent",
        "received",
        "unknown",
        "reconciling",
        "compensation_pending",
        "compensated",
        "blocked",
        "cancelled",
    }
)

# Stable reason codes (also used in tests).
REASON_DELIVERY_TARGET_UNREGISTERED = "S13_DELIVERY_TARGET_UNREGISTERED"
REASON_DELIVERY_TARGET_DISABLED = "S13_DELIVERY_TARGET_DISABLED"
REASON_DELIVERY_REGISTRATION_MISMATCH = "S13_DELIVERY_REGISTRATION_MISMATCH"
REASON_PAYLOAD_DIGEST_MISMATCH = "S13_PAYLOAD_DIGEST_MISMATCH"
REASON_WRONG_RECIPIENT = "S13_WRONG_RECIPIENT"
REASON_COMPENSATION_UNAVAILABLE = "S13_COMPENSATION_POLICY_UNAVAILABLE"


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, fingerprint: str) -> str:
    return f"{prefix}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"


# ---------------------------------------------------------------------------
# Registry — like RegisteredSourceBoundary but for downstream delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DownstreamRecipientRegistration:
    """One immutable downstream recipient registration.

    The registration is deployment-owned (operator/membership decision) and
    pins the transport identity the obligation carries.  Transport I/O never
    guesses a URL or adapter from the request body.
    """

    scope: str  # visibility scope this registration matches, e.g. "C-DEMO"
    recipient_registration_id: str
    recipient_id: str
    route_type: str = "verification_route"
    adapter_id: str = "c-demo-inmemory-transport"
    adapter_version: str = "1"
    enabled: bool = True
    compensation_policy_id: str = S13_DEFAULT_COMPENSATION_POLICY_ID
    compensation_policy_version: str = S13_DEFAULT_COMPENSATION_POLICY_VERSION
    payload_schema_version: str = S13_PAYLOAD_SCHEMA


@dataclass(frozen=True)
class ResolvedDownstreamTarget:
    registration: DownstreamRecipientRegistration
    adapter: "DownstreamAdapter"
    registration_digest: str
    compensation_policy_digest: str


class S13CompletionBlocked(Exception):
    """Fail-closed completion block — caller must NOT persist staged state."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class RegisteredDownstreamRegistry:
    """Small stateless-facing boundary for registered downstream delivery."""

    def __init__(
        self,
        registrations: list[DownstreamRecipientRegistration],
        adapters: dict[str, "DownstreamAdapter"],
    ) -> None:
        self._registrations = list(registrations)
        self._adapters = dict(adapters)
        # Stable digest over the registration manifest (integrity pin).
        manifest = {
            "schema_version": "s13-downstream-registry/1",
            "registrations": [
                {
                    "scope": item.scope,
                    "recipient_registration_id": item.recipient_registration_id,
                    "recipient_id": item.recipient_id,
                    "route_type": item.route_type,
                    "adapter_id": item.adapter_id,
                    "adapter_version": item.adapter_version,
                    "payload_schema_version": item.payload_schema_version,
                    "enabled": item.enabled,
                    "compensation_policy_id": item.compensation_policy_id,
                    "compensation_policy_version": item.compensation_policy_version,
                }
                for item in sorted(
                    self._registrations, key=lambda item: item.recipient_registration_id
                )
            ],
        }
        self.manifest_digest = _digest(manifest)

    def _compensation_digest(self, registration: DownstreamRecipientRegistration) -> str:
        return _digest(
            {
                "compensation_policy_id": registration.compensation_policy_id,
                "compensation_policy_version": registration.compensation_policy_version,
            }
        )

    def adapter_registration_digest(
        self, registration: DownstreamRecipientRegistration
    ) -> str:
        return _digest(
            {
                "scope": registration.scope,
                "recipient_registration_id": registration.recipient_registration_id,
                "recipient_id": registration.recipient_id,
                "route_type": registration.route_type,
                "adapter_id": registration.adapter_id,
                "adapter_version": registration.adapter_version,
                "payload_schema_version": registration.payload_schema_version,
                "compensation_policy_id": registration.compensation_policy_id,
                "compensation_policy_version": registration.compensation_policy_version,
            }
        )

    @staticmethod
    def _scope_matches(registration_scope: str, request_scope: str) -> bool:
        if registration_scope == request_scope:
            return True
        # The demo scope family includes the bare "C-DEMO" and any
        # "C-DEMO/session/<hex>" session scope derived from it.
        if registration_scope == "C-DEMO" and (
            request_scope == "C-DEMO"
            or request_scope.startswith("C-DEMO/session/")
            or request_scope.startswith("C-DEMO/")
        ):
            return True
        return False

    def resolve(
        self,
        *,
        scope: str,
        route_type: str = "verification_route",
    ) -> ResolvedDownstreamTarget:
        matches = [
            item
            for item in self._registrations
            if self._scope_matches(item.scope, scope) and item.route_type == route_type
        ]
        if len(matches) != 1:
            raise S13CompletionBlocked(REASON_DELIVERY_TARGET_UNREGISTERED)
        registration = matches[0]
        if not registration.enabled:
            raise S13CompletionBlocked(REASON_DELIVERY_TARGET_DISABLED)
        if (
            registration.payload_schema_version != S13_PAYLOAD_SCHEMA
            or not registration.compensation_policy_id
            or not registration.compensation_policy_version
        ):
            raise S13CompletionBlocked(REASON_DELIVERY_REGISTRATION_MISMATCH)
        adapter = self._adapters.get(registration.adapter_id)
        if adapter is None:
            raise S13CompletionBlocked(REASON_DELIVERY_TARGET_UNREGISTERED)
        if (
            getattr(adapter, "adapter_id", None) != registration.adapter_id
            or getattr(adapter, "adapter_version", None) != registration.adapter_version
        ):
            raise S13CompletionBlocked(REASON_DELIVERY_REGISTRATION_MISMATCH)
        return ResolvedDownstreamTarget(
            registration=registration,
            adapter=adapter,
            registration_digest=self.adapter_registration_digest(registration),
            compensation_policy_digest=self._compensation_digest(registration),
        )

    def lookup_by_obligation(
        self, *, obligation: dict[str, Any]
    ) -> ResolvedDownstreamTarget:
        scope = str(obligation.get("scope") or "")
        matches = [
            item
            for item in self._registrations
            if self._scope_matches(item.scope, scope)
            and item.recipient_registration_id == obligation.get("recipient_registration_id")
            and item.recipient_id == obligation.get("recipient_id")
            and item.route_type == obligation.get("route_type")
            and item.adapter_id == obligation.get("adapter_id")
            and item.adapter_version == obligation.get("adapter_version")
        ]
        if len(matches) != 1:
            raise LookupError(REASON_DELIVERY_TARGET_UNREGISTERED)
        registration = matches[0]
        if not registration.enabled:
            raise LookupError(REASON_DELIVERY_TARGET_DISABLED)
        if (
            registration.payload_schema_version != S13_PAYLOAD_SCHEMA
            or str(obligation.get("payload_schema") or "")
            != registration.payload_schema_version
            or not registration.compensation_policy_id
            or not registration.compensation_policy_version
        ):
            raise LookupError(REASON_DELIVERY_REGISTRATION_MISMATCH)
        adapter = self._adapters.get(registration.adapter_id)
        if adapter is None:
            raise LookupError(REASON_DELIVERY_TARGET_UNREGISTERED)
        if (
            getattr(adapter, "adapter_id", None) != registration.adapter_id
            or getattr(adapter, "adapter_version", None) != registration.adapter_version
        ):
            raise LookupError(REASON_DELIVERY_REGISTRATION_MISMATCH)
        expected_digest = self.adapter_registration_digest(registration)
        if str(obligation.get("adapter_registration_digest") or "") != expected_digest:
            raise LookupError(REASON_DELIVERY_REGISTRATION_MISMATCH)
        return ResolvedDownstreamTarget(
            registration=registration,
            adapter=adapter,
            registration_digest=expected_digest,
            compensation_policy_digest=self._compensation_digest(registration),
        )


def _c_demo_registrations() -> list[DownstreamRecipientRegistration]:
    return [
        DownstreamRecipientRegistration(
            scope="C-DEMO",
            recipient_registration_id="c-demo-downstream-review-default",
            recipient_id="downstream-review-desk",
        ),
        DownstreamRecipientRegistration(
            scope="R-OBSERVED/tenant-test",
            recipient_registration_id="c-demo-downstream-review-default",
            recipient_id="downstream-review-desk",
        ),
    ]


def build_c_demo_registry(
    *,
    extra_registrations: list[DownstreamRecipientRegistration] | None = None,
    extra_adapters: dict[str, "DownstreamAdapter"] | None = None,
) -> RegisteredDownstreamRegistry:
    registrations = _c_demo_registrations()
    if extra_registrations:
        registrations = [*registrations, *extra_registrations]
    adapters: dict[str, DownstreamAdapter] = {
        "c-demo-inmemory-transport": InMemoryDownstreamAdapter(
            adapter_id="c-demo-inmemory-transport",
            adapter_version="1",
        ),
        "c-demo-controlled-transport": ControlledDownstreamAdapter(
            adapter_id="c-demo-controlled-transport",
            adapter_version="1",
        ),
    }
    if extra_adapters:
        adapters.update(extra_adapters)
    return RegisteredDownstreamRegistry(registrations, adapters)


# ---------------------------------------------------------------------------
# Adapter seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliverySendRequest:
    operation_id: str
    recipient_id: str
    recipient_registration_id: str
    adapter_id: str
    adapter_version: str
    adapter_registration_digest: str
    payload_ref: str
    payload_digest: str
    payload_schema: str
    route_basis_digest: str
    obligation_id: str
    scope: str


@dataclass(frozen=True)
class DeliverySendResult:
    outcome: str  # "confirmed" | "timeout" | "transport_error"
    remote_message_id: str | None = None
    response_digest: str | None = None
    reason_code: str | None = None
    executed_remotely: bool = False


@dataclass(frozen=True)
class DeliveryLookupResult:
    outcome: str  # "confirmed" | "not_executed" | "indeterminate"
    remote_message_id: str | None = None
    response_digest: str | None = None
    evidence_digest: str | None = None


@dataclass(frozen=True)
class DeliveryCompensationRequest:
    operation_id: str
    recipient_id: str
    compensation_policy_id: str
    compensation_policy_version: str


@dataclass(frozen=True)
class DeliveryCompensationResult:
    outcome: str  # "compensated" | "failed"
    reason_code: str | None = None


class DownstreamAdapter(ABC):
    """The only surface that may touch a downstream network or simulator.

    Implementations must never synthesize a URL from caller input or expose
    raw document values.  They report outcome and whether the side effect
    was applied beyond a retry boundary.
    """

    adapter_id: str
    adapter_version: str

    @abstractmethod
    def send(self, request: DeliverySendRequest) -> DeliverySendResult: ...

    @abstractmethod
    def lookup(self, *, operation_id: str, recipient_id: str) -> DeliveryLookupResult: ...

    @abstractmethod
    def compensate(
        self, request: DeliveryCompensationRequest
    ) -> DeliveryCompensationResult: ...


# ---------------------------------------------------------------------------
# In-memory adapter — deterministic harness adapter for tests
# ---------------------------------------------------------------------------


class InMemoryDownstreamAdapter(DownstreamAdapter):
    """Deterministic in-memory transport adapter.

    A tiny durable inbox lives on this instance (for unit conformance): the
    ``executed_operations`` map.  Real deployments persist the inbox in the
    SQLite delivery_inbox table, but this harness proves the adapter contract
    (operation identity, idempotent execute, unknown timeout, lookup by same
    operation, wrong recipient, compensation) without any network.
    """

    def __init__(
        self,
        *,
        adapter_id: str = "c-demo-inmemory-transport",
        adapter_version: str = "1",
        behavior: str = "confirm",  # confirm | timeout_after_execute | timeout_without_execute | reject_recipient
        compensation_behavior: str = "succeed",  # succeed | fail
    ) -> None:
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.behavior = behavior
        self.compensation_behavior = compensation_behavior
        # operation_id -> count (one business effect per operation — idempotent).
        self.executed_operations: dict[str, int] = {}
        self.compensated_operations: set[str] = set()

    def _expected_recipient(self) -> str:
        return "downstream-review-desk"

    def send(self, request: DeliverySendRequest) -> DeliverySendResult:
        if request.recipient_id != self._expected_recipient():
            return DeliverySendResult(
                outcome="transport_error",
                reason_code=REASON_WRONG_RECIPIENT,
                executed_remotely=False,
            )
        if request.adapter_id != self.adapter_id:
            return DeliverySendResult(
                outcome="transport_error",
                reason_code=REASON_DELIVERY_REGISTRATION_MISMATCH,
                executed_remotely=False,
            )
        if self.behavior == "timeout_after_execute":
            # The remote effect was applied, but the channel timed out before
            # the caller learned the result.  The local fact must be unknown,
            # reconciled via the original operation id.
            self.executed_operations[request.operation_id] = 1
            return DeliverySendResult(
                outcome="timeout",
                reason_code="transport.timeout",
                executed_remotely=True,
            )
        if self.behavior == "timeout_without_execute":
            return DeliverySendResult(
                outcome="timeout",
                reason_code="transport.timeout",
                executed_remotely=False,
            )
        if self.behavior == "reject_recipient":
            return DeliverySendResult(
                outcome="transport_error",
                reason_code=REASON_WRONG_RECIPIENT,
                executed_remotely=False,
            )
        # Idempotent execute: duplicate sends with the same operation id yield
        # the same remote message with one business effect.
        if request.operation_id not in self.executed_operations:
            self.executed_operations[request.operation_id] = 1
        remote_message_id = _stable_id(
            "remote", f"{request.operation_id}:{self.adapter_id}"
        )
        return DeliverySendResult(
            outcome="confirmed",
            remote_message_id=remote_message_id,
            response_digest=_digest(
                {"operation_id": request.operation_id, "recipient_id": request.recipient_id}
            ),
            executed_remotely=True,
        )

    def lookup(
        self, *, operation_id: str, recipient_id: str
    ) -> DeliveryLookupResult:
        if recipient_id != self._expected_recipient():
            return DeliveryLookupResult(outcome="indeterminate")
        if operation_id in self.executed_operations:
            return DeliveryLookupResult(
                outcome="confirmed",
                remote_message_id=_stable_id("remote", f"{operation_id}:{self.adapter_id}"),
                response_digest=_digest(
                    {"operation_id": operation_id, "recipient_id": recipient_id}
                ),
                evidence_digest=_digest({"evidence": operation_id}),
            )
        return DeliveryLookupResult(outcome="not_executed", evidence_digest=_digest({"absent": operation_id}))

    def compensate(
        self, request: DeliveryCompensationRequest
    ) -> DeliveryCompensationResult:
        if self.compensation_behavior == "fail":
            return DeliveryCompensationResult(outcome="failed", reason_code="operation.compensation_failed")
        if request.operation_id not in self.executed_operations:
            # No applied effect to compensate — treat as already compensated.
            return DeliveryCompensationResult(outcome="compensated")
        self.compensated_operations.add(request.operation_id)
        return DeliveryCompensationResult(outcome="compensated")


class ControlledDownstreamAdapter(DownstreamAdapter):
    """Institution-registered controlled adapter — second conformance surface.

    Behaviorally indistinguishable from the in-memory adapter (one effect per
    operation, same timeout/lookup/compensate contract) so the same
    conformance suite proves transport substitution doesn't change Lifecycle
    facts.  A different ``adapter_id`` and ``remote`` id prefix remain as
    the distinguishable registered transport.
    """

    def __init__(
        self,
        *,
        adapter_id: str = "c-demo-controlled-transport",
        adapter_version: str = "1",
        behavior: str = "confirm",
        compensation_behavior: str = "succeed",
    ) -> None:
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.behavior = behavior
        self.compensation_behavior = compensation_behavior
        self.executed_operations: dict[str, int] = {}
        self.compensated_operations: set[str] = set()

    def _expected_recipient(self) -> str:
        return "downstream-review-desk"

    def send(self, request: DeliverySendRequest) -> DeliverySendResult:
        if request.recipient_id != self._expected_recipient():
            return DeliverySendResult(
                outcome="transport_error",
                reason_code=REASON_WRONG_RECIPIENT,
                executed_remotely=False,
            )
        if request.adapter_id != self.adapter_id:
            return DeliverySendResult(
                outcome="transport_error",
                reason_code=REASON_DELIVERY_REGISTRATION_MISMATCH,
                executed_remotely=False,
            )
        if self.behavior == "timeout_after_execute":
            self.executed_operations[request.operation_id] = 1
            return DeliverySendResult(
                outcome="timeout",
                reason_code="transport.timeout",
                executed_remotely=True,
            )
        if self.behavior == "timeout_without_execute":
            return DeliverySendResult(
                outcome="timeout",
                reason_code="transport.timeout",
                executed_remotely=False,
            )
        if self.behavior == "reject_recipient":
            return DeliverySendResult(
                outcome="transport_error",
                reason_code=REASON_WRONG_RECIPIENT,
                executed_remotely=False,
            )
        if request.operation_id not in self.executed_operations:
            self.executed_operations[request.operation_id] = 1
        return DeliverySendResult(
            outcome="confirmed",
            remote_message_id=_stable_id(
                "remote-ctl", f"{request.operation_id}:{self.adapter_id}"
            ),
            response_digest=_digest(
                {
                    "operation_id": request.operation_id,
                    "recipient_id": request.recipient_id,
                    "controlled": True,
                }
            ),
            executed_remotely=True,
        )

    def lookup(self, *, operation_id: str, recipient_id: str) -> DeliveryLookupResult:
        if recipient_id != self._expected_recipient():
            return DeliveryLookupResult(outcome="indeterminate")
        if operation_id in self.executed_operations:
            return DeliveryLookupResult(
                outcome="confirmed",
                remote_message_id=_stable_id("remote-ctl", f"{operation_id}:{self.adapter_id}"),
                response_digest=_digest(
                    {"operation_id": operation_id, "recipient_id": recipient_id, "controlled": True}
                ),
                evidence_digest=_digest({"evidence": operation_id}),
            )
        return DeliveryLookupResult(outcome="not_executed", evidence_digest=_digest({"absent": operation_id}))

    def compensate(self, request: DeliveryCompensationRequest) -> DeliveryCompensationResult:
        if self.compensation_behavior == "fail":
            return DeliveryCompensationResult(outcome="failed", reason_code="operation.compensation_failed")
        if request.operation_id not in self.executed_operations:
            return DeliveryCompensationResult(outcome="compensated")
        self.compensated_operations.add(request.operation_id)
        return DeliveryCompensationResult(outcome="compensated")


# ---------------------------------------------------------------------------
# Minimized payload + obligation helpers shared by the service
# ---------------------------------------------------------------------------

OBLIGATION_KIND_AUTO = "automatic"
OBLIGATION_KIND_HUMAN = "human"
OBLIGATION_KIND_EXCEPTION = "business_exception"


def build_route_basis(
    *,
    application_id: str,
    cycle: int,
    run_id: str,
    evidence_snapshot_id: str,
    evidence_snapshot_digest: str,
    release_id: str,
    release_digest: str,
    checker_build: str,
    fence: int,
    route: str,
    completion_event_id: str,
    completion_lifecycle_revision: int,
    completion_reason_code: str,
    attribution_kind: str,
    attribution_ref: dict[str, Any],
) -> dict[str, Any]:
    """The immutable route basis — no raw values, no loan decision."""
    basis: dict[str, Any] = {
        "application_id": application_id,
        "cycle": cycle,
        "run_id": run_id,
        "evidence_snapshot_id": evidence_snapshot_id,
        "evidence_snapshot_digest": evidence_snapshot_digest,
        "release_id": release_id,
        "release_digest": release_digest,
        "checker_build": checker_build,
        "fence": fence,
        "route": route,
        "completion_event_id": completion_event_id,
        "completion_lifecycle_revision": completion_lifecycle_revision,
        "completion_reason_code": completion_reason_code,
        "attribution_kind": attribution_kind,
        "attribution_ref": dict(attribution_ref),
    }
    return basis


def payload_from_route_basis(route_basis: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Return (payload_ref, payload_digest, payload)."""
    digest = _digest(route_basis)
    payload_ref = f"s13-payload-ref:{digest}"
    payload = {"route_basis": dict(route_basis), "payload_schema": S13_PAYLOAD_SCHEMA}
    return payload_ref, digest, payload
