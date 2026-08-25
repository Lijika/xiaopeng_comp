import type { components } from "../generated/api";

export type S14CommandResult = components["schemas"]["S14CommandResult"];
export type S14CurrentRouteResponse =
  components["schemas"]["S01CurrentRouteResponse"];
export type S13DeliveryViewPayload =
  components["schemas"]["S13QueryResponse"];

export const S14_APPLICATION_ID = "app_s14_t16_00000001";
export const S14_PERMISSION_ID = "institutional-reopen-permission/t16-1";
export const S14_APPROVER_SUBJECT = "t16-independent-approver";

const EVIDENCE_DIGEST = "a".repeat(64);
const RELEASE_DIGEST = "b".repeat(64);
const ARTIFACT_DIGEST = "c".repeat(64);

export { EVIDENCE_DIGEST, RELEASE_DIGEST, ARTIFACT_DIGEST };

export function s14CurrentRoute(
  overrides: Partial<S14CurrentRouteResponse> = {},
): S14CurrentRouteResponse {
  return {
    schema_version: "s04-current-route/1",
    application_id: S14_APPLICATION_ID,
    phase: "Manual Review",
    route: "manual_review",
    current_run_id: "run_t16_00000001",
    cycle: 1,
    lifecycle_revision: 5,
    evidence_revision: 2,
    evidence_snapshot_id: "snap_t16_00000001",
    evidence_snapshot_digest: EVIDENCE_DIGEST,
    release_id: "auto_lease@1.9.0",
    release_digest: RELEASE_DIGEST,
    checker_build: "checker-t16",
    currentness_reason: "CURRENT_CONTEXT_MATCH",
    ...overrides,
  };
}

export function s14CommandResult(
  overrides: Partial<S14CommandResult> = {},
): S14CommandResult {
  return {
    status: "accepted",
    replayed: false,
    track: "C-DEMO",
    application_id: S14_APPLICATION_ID,
    ...overrides,
  };
}

export function s14AcceptedCancel(
  overrides: Partial<S14CommandResult> = {},
): S14CommandResult {
  return s14CommandResult({
    phase: "Terminating",
    cycle: 1,
    lifecycle_revision: 6,
    cancel_reason_code: "UPSTREAM_WITHDRAWN",
    cancelled_by: "registered-test-integrator",
    fenced_effects: {
      jobs: 0,
      review_work_items: 1,
      supplement_requests: 0,
      exception_requests: 0,
      deliveries_fenced: 0,
    },
    ...overrides,
  });
}

export function s14OutstandingSettle(
  overrides: Partial<S14CommandResult> = {},
): S14CommandResult {
  return s14CommandResult({
    status: "outstanding",
    phase: "Terminating",
    cycle: 1,
    lifecycle_revision: 6,
    unresolved_effects: [
      {
        kind: "termination_notification",
        id: "outbox_t16_00000001",
        detail: "pending",
        settled: false,
      },
    ],
    ...overrides,
  });
}

export function s14TerminatedSettle(
  overrides: Partial<S14CommandResult> = {},
): S14CommandResult {
  return s14CommandResult({
    status: "terminated",
    phase: "Terminated",
    cycle: 1,
    lifecycle_revision: 7,
    settled_effects: [
      {
        kind: "termination_notification",
        id: "outbox_t16_00000001",
        result: "delivered",
      },
    ],
    unresolved_effects: [],
    ...overrides,
  });
}

export function s14AcceptedGrant(
  overrides: Partial<S14CommandResult> = {},
): S14CommandResult {
  return s14CommandResult({
    permission_id: S14_PERMISSION_ID,
    approved_by: S14_APPROVER_SUBJECT,
    scope: "C-DEMO",
    policy_release_id: "auto_lease@1.9.0",
    policy_release_digest: RELEASE_DIGEST,
    artifact_release_digest: ARTIFACT_DIGEST,
    source_binding: "t16-fixture-intake",
    granted_via_source: "c-demo-operator-control-plane",
    expires_at: 4_102_444_800,
    ...overrides,
  });
}

export function s14AcceptedReopen(
  overrides: Partial<S14CommandResult> = {},
): S14CommandResult {
  return s14CommandResult({
    cycle: 2,
    phase: "Intake",
    lifecycle_revision: 8,
    predecessor_cycle: 1,
    ...overrides,
  });
}

/** The operator-context authoritative read seam: the released S13 delivery
 * view exposes the server-owned phase/cycle/revision for any phase. */
export function s14OperatorDeliveryView(
  overrides: Partial<S13DeliveryViewPayload> = {},
): S13DeliveryViewPayload {
  return {
    schema_version: "s13-delivery-view/1",
    application_id: S14_APPLICATION_ID,
    phase: "Terminating",
    route: "s14_cancelled",
    cycle: 1,
    lifecycle_revision: 6,
    verification_completed: false,
    obligation: null,
    routing_history: [],
    delivery_status: "none",
    attempt_count: 0,
    projection_watermark: 9,
    store_revision: 21,
    ...overrides,
  };
}
