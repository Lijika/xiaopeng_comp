import type { components } from "../generated/api";

export type S13QueryResponse = components["schemas"]["S13QueryResponse"];

export const S13_APPLICATION_ID = "app_s13_t15_00000001";
export const S13_OBLIGATION_ID = "obl_s13_t15_00000001";
export const S13_OPERATION_ID = "op_s13_t15_00000000000000000000000001";

export function s13QueryPayload(
  overrides: Partial<S13QueryResponse> = {},
): S13QueryResponse {
  const evidenceDigest = "a".repeat(64);
  const releaseDigest = "b".repeat(64);
  const routeBasisDigest = "c".repeat(64);
  const payloadDigest = "d".repeat(64);
  return {
    schema_version: "s13-delivery-view/1",
    application_id: S13_APPLICATION_ID,
    phase: "Verification Completed",
    route: "human_complete",
    cycle: 1,
    lifecycle_revision: 7,
    verification_completed: true,
    obligation: {
      obligation_id: S13_OBLIGATION_ID,
      application_id: S13_APPLICATION_ID,
      cycle: 1,
      route: "human_complete",
      attribution_kind: "human",
      operation_id: S13_OPERATION_ID,
      recipient_id: "recipient_c_demo_1",
      adapter_id: "c-demo-downstream",
      adapter_version: "1",
      payload_ref: "payload/s13/00000001",
      payload_digest: payloadDigest,
      payload_schema: "s13-route-payload/1",
      status: "pending",
    },
    routing_history: [
      {
        cycle: 1,
        route: "human_complete",
        attribution_kind: "human",
        attribution: {
          decision_id: "decision_t15_0001",
          work_item_id: "review_work_t15_0001",
          request_id: null,
          batch_id: null,
          work_item_ids: [],
        },
        completion_event_id: "lifecycle_event_t15_0001",
        completion_lifecycle_revision: 7,
        run_id: "run_t15_0001",
        evidence_snapshot_id: "snapshot_t15_0001",
        evidence_snapshot_digest: evidenceDigest,
        release_id: "release_t15_0001",
        release_digest: releaseDigest,
        checker_build: "checker-t15",
        route_basis_digest: routeBasisDigest,
        obligation_id: S13_OBLIGATION_ID,
        operation_id: S13_OPERATION_ID,
      },
    ],
    delivery_status: "pending",
    attempt_count: 0,
    projection_watermark: 10,
    store_revision: 42,
    ...overrides,
  };
}
