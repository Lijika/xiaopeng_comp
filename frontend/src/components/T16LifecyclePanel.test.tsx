import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import T16LifecyclePanel, { T16SettlementPanel } from "./T16LifecyclePanel";
import { fetchRouter, renderWithQuery } from "../test-utils";
import {
  ARTIFACT_DIGEST,
  S14_APPLICATION_ID,
  S14_APPROVER_SUBJECT,
  S14_PERMISSION_ID,
  s14AcceptedCancel,
  s14AcceptedGrant,
  s14AcceptedReopen,
  s14CurrentRoute,
  s14OperatorDeliveryView,
  s14OutstandingSettle,
  s14TerminatedSettle,
} from "../test-fixtures/s14";

const CANCEL_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/cancel`;
const SETTLE_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/settle-termination`;
const GRANT_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/grant-reopen-permission`;
const REOPEN_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/reopen`;
const NOTIFY_PATH = "/controlled/s01/api/commands/process-termination-notification";
const ROUTE_PATH = `/controlled/s01/api/queries/applications/${S14_APPLICATION_ID}/current-route`;
const HISTORY_PATH = `/controlled/s01/api/queries/applications/${S14_APPLICATION_ID}/history`;
const DELIVERY_PATH = `/controlled/s13/delivery/${S14_APPLICATION_ID}`;

function jsonRoute(payload: unknown): () => Response {
  return () =>
    new Response(JSON.stringify(payload), {
      headers: { "Content-Type": "application/json" },
    });
}

function historyPayload(
  runs: Array<{ cycle: number; run_id: string; current?: boolean }>,
  extras: {
    cancellations?: Array<Record<string, unknown>>;
    terminations?: Array<Record<string, unknown>>;
    reopens?: Array<Record<string, unknown>>;
    late_input_receipts?: Array<Record<string, unknown>>;
  } = {},
) {
  return {
    schema_version: "s04-application-history/1",
    application_id: S14_APPLICATION_ID,
    current_run_id: runs.find((run) => run.current)?.run_id ?? null,
    runs: runs.map((run) => ({
      run_id: run.run_id,
      status: "complete",
      authority_digest: "d".repeat(64),
      current: run.current ?? false,
      currentness_reason: run.current ? "CURRENT_CONTEXT_MATCH" : "CONTEXT_NOT_CURRENT",
      cycle: run.cycle,
      lifecycle_revision: 5,
      evidence_revision: 2,
      evidence_snapshot_id: "snap_t16",
      evidence_snapshot_digest: "a".repeat(64),
      release_id: "auto_lease@1.9.0",
      release_digest: ARTIFACT_DIGEST,
      checker_build: "checker-t16",
      finding_ids: ["finding_t16"],
      cas_mismatches: [],
      selected_observation_ids: [],
      decision_ids: [],
      exception_ids: [],
      applicable_decision_ids: [],
      applicable_exception_ids: [],
      membership_decisions: [],
      entity_link_decisions: [],
      evidence_document_instance_ids: [],
      components: [],
    })),
    corrections: [],
    business_exceptions: [],
    attachment_versions: [],
    memberships: [],
    membership_history: [],
    entity_links: [],
    entity_link_history: [],
    cancellations: extras.cancellations ?? [],
    terminations: extras.terminations ?? [],
    reopens: extras.reopens ?? [],
    late_input_receipts: extras.late_input_receipts ?? [],
  };
}

describe("T16LifecyclePanel (integrator cancellation)", () => {
  it("renders the authoritative facts from current-route", async () => {
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(s14CurrentRoute()),
      [`GET ${HISTORY_PATH}`]: jsonRoute(historyPayload([{ cycle: 1, run_id: "run_1", current: true }])),
    });
    renderWithQuery(<T16LifecyclePanel applicationId={S14_APPLICATION_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("t16-phase")).toHaveTextContent("Manual Review"),
    );
    expect(screen.getByTestId("t16-cycle")).toHaveTextContent("1");
    expect(screen.getByTestId("t16-lifecycle-revision")).toHaveTextContent("5");
  });

  it("shows an explicit not-found state that hides existence", async () => {
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: () =>
        new Response(
          JSON.stringify({ detail: { error: "S03_NOT_FOUND" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${HISTORY_PATH}`]: () =>
        new Response(
          JSON.stringify({ detail: { error: "S03_NOT_FOUND" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<T16LifecyclePanel applicationId={S14_APPLICATION_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("t16-error-not-found")).toBeInTheDocument(),
    );
  });

  it("renders an explicit sanitized history error instead of empty history and hides cycle actions", async () => {
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(s14CurrentRoute()),
      [`GET ${HISTORY_PATH}`]: () =>
        new Response(
          JSON.stringify({ detail: { error: "S03_NOT_FOUND" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<T16LifecyclePanel applicationId={S14_APPLICATION_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("t16-error-not-found")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("t16-history-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t16-history-run-cycle")).not.toBeInTheDocument();
  });

  it("shows the authoritative selected-cycle facts with lifecycle events and late receipts", async () => {
    const onCycleSelected = vi.fn();
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(
        s14CurrentRoute({
          phase: "Intake",
          cycle: 2,
          lifecycle_revision: 8,
          current_run_id: null,
          currentness_reason: "NO_CURRENT_RUN",
        }),
      ),
      [`GET ${HISTORY_PATH}`]: jsonRoute(
        historyPayload(
          [
            { cycle: 1, run_id: "run_cycle1" },
            { cycle: 2, run_id: "run_cycle2" },
          ],
          {
            cancellations: [
              {
                event_id: "canc_1",
                cycle: 1,
                lifecycle_revision: 6,
                reason_code: "UPSTREAM_WITHDRAWN",
                authority_subject: "t16-registered-integrator",
                fenced_effects: { review_work_items: 1 },
                cancelled_at: 100,
              },
            ],
            terminations: [
              {
                event_id: "term_1",
                cycle: 1,
                lifecycle_revision: 7,
                settled_effects: [],
                terminated_at: 200,
              },
            ],
            reopens: [
              {
                event_id: "reopen_1",
                predecessor_cycle: 1,
                cycle: 2,
                lifecycle_revision: 8,
                target_phase: "Intake",
                reopened_by: "t16-operator",
                reopened_at: 300,
              },
            ],
            late_input_receipts: [
              {
                receipt_id: "late_1",
                reason_code: "evidence.late_input_requires_reopen",
                request_id: "req_1",
                occurred_at: 250,
              },
            ],
          },
        ),
      ),
    });
    renderWithQuery(
      <T16LifecyclePanel
        applicationId={S14_APPLICATION_ID}
        selectedCycle={1}
        onCycleSelected={onCycleSelected}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("t16-cycle-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t16-cycle-banner")).toHaveTextContent(
      "Cycle 1",
    );
    expect(screen.getByTestId("t16-cycle-run")).toHaveTextContent(
      "run_cycle1",
    );
    expect(screen.getByTestId("t16-cycle-cancellation")).toHaveTextContent(
      "UPSTREAM_WITHDRAWN",
    );
    expect(screen.getByTestId("t16-cycle-termination")).toBeInTheDocument();
    expect(screen.getByTestId("t16-cycle-reopen")).toHaveTextContent("Intake");
    expect(screen.getByTestId("t16-late-receipt")).toHaveTextContent(
      "evidence.late_input_requires_reopen",
    );
    // The historical cycle view is read-only: no command surface.
    expect(screen.queryByTestId("t16-cancel-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t16-cancel-button")).not.toBeInTheDocument();
  });

  it("posts one exact cancel command and renders the typed accepted outcome", async () => {
    const user = userEvent.setup();
    let routeReads = 0;
    const router = fetchRouter({
      [`GET ${ROUTE_PATH}`]: () => {
        routeReads += 1;
        return new Response(
          JSON.stringify(
            routeReads < 2
              ? s14CurrentRoute()
              : s14CurrentRoute({
                  phase: "Terminating",
                  lifecycle_revision: 6,
                  route: "s14_cancelled",
                }),
          ),
          { headers: { "Content-Type": "application/json" } },
        );
      },
      [`GET ${HISTORY_PATH}`]: jsonRoute(historyPayload([{ cycle: 1, run_id: "run_1", current: true }])),
      [`POST ${CANCEL_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedCancel()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderWithQuery(<T16LifecyclePanel applicationId={S14_APPLICATION_ID} />);
    const cancel = await screen.findByTestId("t16-cancel-button");
    await user.click(cancel);
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent("accepted"),
    );
    expect(screen.getByTestId("t16-result-status")).toHaveTextContent("Terminating");
    expect(router.calls.filter(({ method }) => method === "POST")).toHaveLength(1);
    expect(router.calls.find(({ method }) => method === "POST")?.body).toMatchObject({
      expected_lifecycle_revision: 5,
      reason_code: "UPSTREAM_WITHDRAWN",
    });
    // The authoritative read converges to the server-owned Terminating fact.
    await waitFor(() =>
      expect(screen.getByTestId("t16-phase")).toHaveTextContent("Terminating"),
    );
    await waitFor(() => expect(screen.getByTestId("t16-cancel-button")).toBeDisabled());
  });

  it("retains the identical idempotency key across an unknown transport retry", async () => {
    const user = userEvent.setup();
    let cancelAttempts = 0;
    const postBodies: Array<{ idempotency_key: string }> = [];
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(s14CurrentRoute()),
      [`GET ${HISTORY_PATH}`]: jsonRoute(historyPayload([{ cycle: 1, run_id: "run_1", current: true }])),
      [`POST ${CANCEL_PATH}`]: (_url, init) => {
        cancelAttempts += 1;
        postBodies.push(JSON.parse(String(init?.body)));
        if (cancelAttempts === 1) {
          throw new TypeError("network lost");
        }
        return new Response(JSON.stringify(s14AcceptedCancel()), {
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    renderWithQuery(<T16LifecyclePanel applicationId={S14_APPLICATION_ID} />);
    const cancel = await screen.findByTestId("t16-cancel-button");
    await user.click(cancel);
    await waitFor(() => expect(screen.getByTestId("t16-error-unknown")).toBeInTheDocument());
    await user.click(screen.getByTestId("t16-retry"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent("accepted"),
    );
    expect(postBodies).toHaveLength(2);
    expect(postBodies[0].idempotency_key).toBe(postBodies[1].idempotency_key);
  });

  it("renders an unauthorized envelope verbatim and requires a reload before retry", async () => {
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(s14CurrentRoute()),
      [`GET ${HISTORY_PATH}`]: jsonRoute(
        historyPayload([{ cycle: 1, run_id: "run_1", current: true }]),
      ),
      [`POST ${CANCEL_PATH}`]: () =>
        new Response(
          JSON.stringify({
            status: "rejected",
            replayed: false,
            application_id: S14_APPLICATION_ID,
            reason_code: "S14_FORBIDDEN",
            reason: "lifecycle.cancel_forbidden",
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<T16LifecyclePanel applicationId={S14_APPLICATION_ID} />);
    await user.click(await screen.findByTestId("t16-cancel-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent(
        "rejected",
      ),
    );
    expect(screen.getByTestId("t16-result-reason")).toHaveTextContent(
      "S14_FORBIDDEN",
    );
    await waitFor(() =>
      expect(screen.getByTestId("t16-reload-required")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t16-cancel-button")).toBeDisabled();
  });

  it("keeps showing Terminating until FastAPI reports Terminated and surfaces the bounded unknown otherwise", async () => {
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(
        s14CurrentRoute({
          phase: "Terminating",
          lifecycle_revision: 6,
          route: "s14_cancelled",
        }),
      ),
      [`GET ${HISTORY_PATH}`]: jsonRoute(historyPayload([])),
    });
    renderWithQuery(
      <T16LifecyclePanel
        applicationId={S14_APPLICATION_ID}
        convergenceOptions={{ intervalMs: 5, maxAttempts: 3 }}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("t16-phase")).toHaveTextContent("Terminating"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t16-poll-timeout")).toBeInTheDocument(),
    );
    // The bounded ceiling never claims termination.
    expect(screen.queryByTestId("t16-terminated")).not.toBeInTheDocument();
    expect(screen.getByTestId("t16-phase")).toHaveTextContent("Terminating");
  });

  it("labels every history run with its owning cycle for old-cycle navigation", async () => {
    const onCycleSelected = vi.fn();
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: jsonRoute(
        s14CurrentRoute({
          phase: "Intake",
          cycle: 2,
          lifecycle_revision: 8,
          current_run_id: null,
          currentness_reason: "NO_CURRENT_RUN",
        }),
      ),
      [`GET ${HISTORY_PATH}`]: jsonRoute(
        historyPayload([
          { cycle: 1, run_id: "run_cycle1" },
          { cycle: 2, run_id: "run_cycle2" },
        ]),
      ),
    });
    renderWithQuery(
      <T16LifecyclePanel
        applicationId={S14_APPLICATION_ID}
        onCycleSelected={onCycleSelected}
      />,
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("t16-history-run")).toHaveLength(2),
    );
    const cycles = screen.getAllByTestId("t16-history-run-cycle");
    expect(cycles[0]).toHaveTextContent("1");
    expect(cycles[1]).toHaveTextContent("2");
    await user.click(cycles[0]);
    expect(onCycleSelected).toHaveBeenCalledWith(1);
  });
});

describe("T16SettlementPanel (operator)", () => {
  it("renders the operator-visible phase from the S13 delivery authority", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: jsonRoute(s14OperatorDeliveryView()),
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("t16-settlement-phase")).toHaveTextContent(
        "Terminating",
      ),
    );
    expect(screen.getByTestId("t16-settle-button")).toBeEnabled();
  });

  it("runs settle-arm, notification, settle-seal and renders every typed outcome", async () => {
    const user = userEvent.setup();
    let settleCalls = 0;
    const router = fetchRouter({
      [`GET ${DELIVERY_PATH}`]: jsonRoute(s14OperatorDeliveryView()),
      [`POST ${SETTLE_PATH}`]: () => {
        settleCalls += 1;
        const payload =
          settleCalls === 1 ? s14OutstandingSettle() : s14TerminatedSettle();
        return new Response(JSON.stringify(payload), {
          status: settleCalls === 1 ? 202 : 200,
          headers: { "Content-Type": "application/json" },
        });
      },
      [`POST ${NOTIFY_PATH}`]: () =>
        new Response(
          JSON.stringify({ status: "delivered", replayed: false }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);
    await user.click(await screen.findByTestId("t16-settle-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent("outstanding"),
    );
    expect(screen.getByTestId("t16-unresolved-effects")).toHaveTextContent(
      "termination_notification",
    );

    await user.click(screen.getByTestId("t16-notification-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-notification-status")).toHaveTextContent("delivered"),
    );

    await user.click(screen.getByTestId("t16-settle-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent("terminated"),
    );
    const settlePosts = router.calls.filter(
      ({ method, url }) => method === "POST" && url === SETTLE_PATH,
    );
    expect(settlePosts).toHaveLength(2);
    // Distinct semantic commands carry distinct idempotency keys.
    expect((settlePosts[0].body as { idempotency_key: string }).idempotency_key).not.toBe(
      (settlePosts[1].body as { idempotency_key: string }).idempotency_key,
    );
  });

  it("enables reopen only after the granted permission and posts its server-owned bindings once", async () => {
    const user = userEvent.setup();
    let deliveryPhase: string = "Terminated";
    const router = fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () =>
        new Response(
          JSON.stringify(s14OperatorDeliveryView({ phase: deliveryPhase, lifecycle_revision: 7 })),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${GRANT_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedGrant()), {
          headers: { "Content-Type": "application/json" },
        }),
      [`POST ${REOPEN_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedReopen()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);

    const reopen = await screen.findByTestId("t16-reopen-button");
    expect(reopen).toBeDisabled();

    const approver = screen.getByTestId("t16-grant-approver");
    await user.type(approver, S14_APPROVER_SUBJECT);
    await user.type(screen.getByTestId("t16-grant-permission-id"), S14_PERMISSION_ID);
    await user.click(screen.getByTestId("t16-grant-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-grant-binding")).toHaveTextContent(S14_PERMISSION_ID),
    );
    expect(screen.getByTestId("t16-grant-binding")).toHaveTextContent(ARTIFACT_DIGEST);

    await user.selectOptions(screen.getByTestId("t16-reopen-target"), "Intake");
    await user.click(reopen);
    await waitFor(() =>
      expect(screen.getByTestId("t16-reopen-result")).toHaveTextContent("2"),
    );
    expect(screen.getByTestId("t16-reopen-result")).toHaveTextContent("Intake");
    expect(router.calls.find(({ url }) => url === REOPEN_PATH)?.body).toEqual({
      expected_lifecycle_revision: 7,
      idempotency_key: expect.any(String),
      target_phase: "Intake",
      reopen_policy: {
        permission_id: S14_PERMISSION_ID,
        release_digest: ARTIFACT_DIGEST,
      },
    });
    // Exactly one reopen POST: the explicit click. Reload/mount never reopens.
    expect(router.calls.filter(({ url }) => url === REOPEN_PATH)).toHaveLength(1);
  });

  it("renders a replayed duplicate settle verbatim with its server facts", async () => {
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: jsonRoute(s14OperatorDeliveryView()),
      [`POST ${SETTLE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s14TerminatedSettle({ status: "replayed", replayed: true }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);
    await user.click(await screen.findByTestId("t16-settle-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent(
        "replayed",
      ),
    );
    expect(screen.getByTestId("t16-settled-effects")).toBeInTheDocument();
  });

  it("restores the server-owned reopen binding from a replayed grant", async () => {
    const user = userEvent.setup();
    let grantCalls = 0;
    const router = fetchRouter({
      [`GET ${DELIVERY_PATH}`]: jsonRoute(
        s14OperatorDeliveryView({ phase: "Terminated", lifecycle_revision: 7 }),
      ),
      [`POST ${GRANT_PATH}`]: () => {
        grantCalls += 1;
        return new Response(
          JSON.stringify(
            grantCalls === 1
              ? s14AcceptedGrant()
              : s14AcceptedGrant({ status: "replayed", replayed: true }),
          ),
          { headers: { "Content-Type": "application/json" } },
        );
      },
      [`POST ${REOPEN_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedReopen()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);
    const approver = await screen.findByTestId("t16-grant-approver");
    await user.type(approver, S14_APPROVER_SUBJECT);
    await user.type(
      screen.getByTestId("t16-grant-permission-id"),
      S14_PERMISSION_ID,
    );
    await user.click(screen.getByTestId("t16-grant-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent(
        "accepted",
      ),
    );
    // The second grant with the same key replays the original binding.
    await user.click(screen.getByTestId("t16-grant-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent(
        "replayed",
      ),
    );
    // Reopen is enabled from the replayed server-owned binding.
    await user.selectOptions(screen.getByTestId("t16-reopen-target"), "Intake");
    await user.click(screen.getByTestId("t16-reopen-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-reopen-result")).toHaveTextContent("2"),
    );
    const reopenPost = router.calls.find(({ url }) => url === REOPEN_PATH);
    // The fetch boundary records parsed JSON; the reopen body shape is bound
    // by the generated S14ReopenCommand type at the mutation call site.
    const reopenBody = reopenPost?.body as
      | { reopen_policy?: { release_digest?: string } }
      | undefined;
    expect(reopenBody?.reopen_policy?.release_digest).toBe(ARTIFACT_DIGEST);
  });

  it("enables notification processing only for the current Terminating cycle with a pending effect", async () => {
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: jsonRoute(s14OperatorDeliveryView()),
      [`POST ${SETTLE_PATH}`]: () =>
        new Response(JSON.stringify(s14OutstandingSettle()), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      [`POST ${NOTIFY_PATH}`]: () =>
        new Response(JSON.stringify({ status: "delivered" }), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);
    const notify = await screen.findByTestId("t16-notification-button");
    // No outstanding settle result yet: the effect is unknown, button stays
    // disabled even though the phase is Terminating.
    expect(notify).toBeDisabled();
    await user.click(screen.getByTestId("t16-settle-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-result-status")).toHaveTextContent(
        "outstanding",
      ),
    );
    expect(notify).toBeEnabled();
  });

  it("surfaces an unknown notification transport with a reconcile affordance", async () => {
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: jsonRoute(s14OperatorDeliveryView()),
      [`POST ${SETTLE_PATH}`]: () =>
        new Response(JSON.stringify(s14OutstandingSettle()), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      [`POST ${NOTIFY_PATH}`]: () => {
        throw new TypeError("network lost");
      },
    });
    renderWithQuery(<T16SettlementPanel applicationId={S14_APPLICATION_ID} />);
    await user.click(await screen.findByTestId("t16-settle-button"));
    await user.click(screen.getByTestId("t16-notification-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t16-notification-unknown")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t16-notification-retry")).toBeEnabled();
  });
});
