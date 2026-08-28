import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import S16GovernedDeletionPanel from "./S16GovernedDeletionPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";
import {
  s16PreflightPayload,
  s16QueryPayload,
} from "../api/hooks.s16.test";
import type { S16PreflightResponse } from "../api/client";

const PREFLIGHT_PATH = "/controlled/s16/api/deletions/preflight";
const QUERY_PATH = "/controlled/s16/api/deletions/s16req_test_00000001";
const APPROVE_PATH = "/controlled/s16/api/deletions/s16req_test_00000001/approve";
const COMMIT_PATH = "/controlled/s16/api/deletions/s16req_test_00000001/commit";
const REPAIR_PATH = "/controlled/s16/api/deletions/s16req_test_00000001/repair";
const PROCESS_PATH = "/controlled/s16/api/process";
const RECEIPT_PATH = "/controlled/s16/api/deletions/s16req_test_00000001/receipt";

function preflightRouter(
  overrides: Partial<S16PreflightResponse> = {},
) {
  return fetchRouter({
    [`POST ${PREFLIGHT_PATH}`]: () =>
      new Response(
        JSON.stringify(s16PreflightPayload(overrides)),
        { headers: { "Content-Type": "application/json" } },
      ),
    [`GET ${QUERY_PATH}`]: () =>
      new Response(
        JSON.stringify(s16QueryPayload({ job: null })),
        { headers: { "Content-Type": "application/json" } },
      ),
    [`POST ${APPROVE_PATH}`]: () =>
      new Response(
        JSON.stringify({
          status: "accepted",
          request_id: "s16req_test_00000001",
          approved_by: "approver",
        }),
        { headers: { "Content-Type": "application/json" } },
      ),
  });
}

async function runPreflight() {
  const user = userEvent.setup();
  await user.type(
    screen.getByTestId("s16-reference"),
    "APP-REFERENCE-1",
  );
  await user.click(screen.getByTestId("s16-preflight-button"));
  await screen.findByTestId("s16-manifest");
  return user;
}

describe("S16GovernedDeletionPanel", () => {
  it("renders the nine-class dry-run manifest with digests only", async () => {
    preflightRouter();
    renderWithQuery(<S16GovernedDeletionPanel />);
    await runPreflight();

    const manifest = screen.getByTestId("s16-manifest");
    const classNames = within(manifest)
      .getAllByTestId("s16-entry-class")
      .map((node) => node.textContent);
    expect(new Set(classNames)).toEqual(
      new Set([
        "source_object",
        "derived_object",
        "evidence",
        "run_or_finding",
        "projection_or_cache",
        "export_or_temp",
        "evaluation_copy",
        "replica",
        "backup_manifest",
      ]),
    );
    // Only digests and counts render; the reference echoes in-session once.
    expect(screen.getByTestId("s16-application-reference")).toHaveTextContent(
      "APP-REFERENCE-1",
    );
    expect(screen.getByTestId("s16-manifest-digest")).toHaveTextContent(
      /^d{64}$/,
    );
    expect(screen.queryByText("tenant-test")).toBeNull();
    expect(screen.queryByText("result-object")).toBeNull();
  });

  it("shows the approval flow and blocks commit until two distinct approvers", async () => {
    preflightRouter();
    renderWithQuery(<S16GovernedDeletionPanel />);
    await runPreflight();

    // Early deletion: commit stays blocked until two approvals.
    const commitButton = screen.getByTestId("s16-commit-button");
    await userEvent.click(screen.getByTestId("s16-commit-confirm"));
    expect(commitButton).toBeDisabled();

    await userEvent.type(
      screen.getByTestId("s16-approver-token"),
      "approver-token-1",
    );
    await userEvent.click(screen.getByTestId("s16-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("s16-approved-count")).toHaveTextContent("1 / 2"),
    );
    expect(commitButton).toBeDisabled();

    await userEvent.type(
      screen.getByTestId("s16-approver-token"),
      "approver-token-2",
    );
    await userEvent.click(screen.getByTestId("s16-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("s16-approved-count")).toHaveTextContent("2 / 2"),
    );
    expect(commitButton).toBeEnabled();
  });

  it("commits after confirmation and shows the durable job with repair surface", async () => {
    let jobStatus: string = "repair_required";
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${APPROVE_PATH}`]: () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            request_id: "s16req_test_00000001",
            approved_by: "approver",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${COMMIT_PATH}`]: () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            request_id: "s16req_test_00000001",
            job_id: "s16job_1",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: {
                job_id: "s16job_1",
                status: jobStatus,
                attempt: 5,
                fence: 5,
                lease_owner: null,
                pending_owner_fingerprints: { s02: 2 },
                owner_results: { s02: "failed" },
                stable_failure: {
                  owner_id: "s02",
                  reason_code: "S16_OWNER_DELETE_FAILED",
                  responsible_party: "runtime_operations_owner",
                  recovery_action: "repair_owner_and_resume_the_same_job",
                  attempt: 5,
                },
                completed_at: null,
              },
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${REPAIR_PATH}`]: () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            request_id: "s16req_test_00000001",
            job_id: "s16job_1",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${PROCESS_PATH}`]: () => {
        jobStatus = "complete";
        return new Response(
          JSON.stringify({ status: "complete", job_id: "s16job_1" }),
          { headers: { "Content-Type": "application/json" } },
        );
      },
      [`GET ${RECEIPT_PATH}`]: () =>
        new Response(
          JSON.stringify({
            receipt_id: "s16receipt_1",
            schema_version: "s16-receipt/1",
            action: "governed_deletion",
            policy: "s16-governed-deletion/1",
            scope_fingerprint: "c".repeat(64),
            completed_at: 1_800_000_001,
            authority: "s16-governance",
            result: "deleted",
            owner_counts: { s01: 4, s02: 2 },
            restore_replay_status: "pending",
            subject: "s16-deletion-worker",
            role: "system",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { client } = renderWithQuery(<S16GovernedDeletionPanel />);
    client.setQueryData(["s01", "queue"], { stale: true });
    await runPreflight();
    // Skip approvals for the test focus: commit requires two approvals, so
    // approve twice first.
    await userEvent.type(
      screen.getByTestId("s16-approver-token"),
      "approver-token-1",
    );
    await userEvent.click(screen.getByTestId("s16-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("s16-approved-count")).toHaveTextContent("1 / 2"),
    );
    await userEvent.type(
      screen.getByTestId("s16-approver-token"),
      "approver-token-2",
    );
    await userEvent.click(screen.getByTestId("s16-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("s16-approved-count")).toHaveTextContent("2 / 2"),
    );

    // Without confirmation the commit is disabled; with it, it commits.
    await userEvent.click(screen.getByTestId("s16-commit-confirm"));
    await userEvent.click(screen.getByTestId("s16-commit-button"));
    await screen.findByTestId("s16-job");

    expect(screen.getByTestId("s16-job-status")).toHaveTextContent(
      "repair_required",
    );
    expect(screen.getByTestId("s16-stable-failure")).toHaveTextContent("s02");
    expect(screen.getByTestId("s16-stable-failure")).toHaveTextContent(
      "S16_OWNER_DELETE_FAILED",
    );

    // Repair resumes the same job; process completes; receipt renders.
    await userEvent.type(
      screen.getByTestId("s16-repair-fact"),
      "s02-repair-verified",
    );
    await userEvent.click(screen.getByTestId("s16-repair-button"));
    await waitFor(() =>
      expect(router.calls.some((call) => call.url === REPAIR_PATH)).toBe(true),
    );
    await userEvent.click(screen.getByTestId("s16-process-button"));
    await screen.findByTestId("s16-receipt");
    expect(screen.getByTestId("s16-receipt-result")).toHaveTextContent(
      "deleted",
    );
    expect(screen.getByTestId("s16-receipt-owner-counts")).toHaveTextContent(
      "s01:4",
    );
    // The completed deletion cleared the seeded S01 application cache.
    expect(client.getQueryData(["s01", "queue"])).toBeUndefined();
    const requestPaths = router.calls.map((call) => call.url);
    expect(requestPaths).not.toContain("/controlled/s01/api/queries/applications/x/current-route");
  });

  it("keeps the same idempotency key visible after an unknown transport outcome", async () => {
    fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        Promise.reject(new TypeError("network lost")),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    await screen.findByTestId("s16-preflight-unknown");
    expect(screen.getByTestId("s16-reference")).toHaveValue("APP-REFERENCE-1");
  });

  it("surfaces typed errors without inventing identifiers", async () => {
    fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S16_NOT_FOUND", message: "unavailable" },
          }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    await screen.findByTestId("s16-error-not-found");
    expect(screen.getByTestId("s16-error-code")).toHaveTextContent(
      "S16_NOT_FOUND",
    );
  });
});

describe("S16 legal hold controls", () => {
  it("imposes and releases holds with the closed vocabulary", async () => {
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: null,
              legal_holds: [
                {
                  hold_id: "hold_1",
                  generation: 1,
                  reason_code: "litigation",
                  owner: "all",
                  effective_time: 1_800_000_000,
                  expiry: null,
                  released: false,
                },
              ],
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s16/api/legal-holds/impose": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            hold_id: "hold_2",
            generation: 2,
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s16/api/legal-holds/hold_1/release": () =>
        new Response(
          JSON.stringify({ status: "accepted", hold_id: "hold_1" }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    await runPreflight();
    await screen.findByTestId("s16-legal-holds");

    // The existing hold renders with its closed fields and a release
    // command; imposing posts the closed vocabulary.
    expect(screen.getByTestId("s16-hold-entry")).toHaveTextContent(
      "litigation",
    );
    await userEvent.click(screen.getByTestId("s16-impose-hold-button"));
    await waitFor(() =>
      expect(
        router.calls.some(
          (call) =>
            call.url === "/controlled/s16/api/legal-holds/impose" &&
            (call.body as { reason_code?: string }).reason_code ===
              "litigation",
        ),
      ).toBe(true),
    );
    await userEvent.click(screen.getByTestId("s16-release-hold-hold_1"));
    await waitFor(() =>
      expect(
        router.calls.some(
          (call) =>
            call.url === "/controlled/s16/api/legal-holds/hold_1/release",
        ),
      ).toBe(true),
    );
  });
});

describe("R2 completion and identity-invalidation states", () => {
  it("unloads every preflight surface after completion (receipt only)", async () => {
    let jobStatus = "complete";
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: {
                job_id: "s16job_1",
                status: jobStatus,
                attempt: 1,
                fence: 1,
                lease_owner: null,
                pending_owner_fingerprints: { s01: 4 },
                owner_results: { s01: "complete" },
                stable_failure: null,
                completed_at: 1_800_000_001,
              },
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${RECEIPT_PATH}`]: () =>
        new Response(
          JSON.stringify({
            receipt_id: "s16receipt_1",
            schema_version: "s16-receipt/1",
            action: "governed_deletion",
            policy: "s16-governed-deletion/1",
            scope_fingerprint: "c".repeat(64),
            completed_at: 1_800_000_001,
            authority: "s16-governance",
            result: "deleted",
            owner_counts: { s01: 4 },
            restore_replay_status: "pending",
            subject: "s16-deletion-worker",
            role: "system",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    await screen.findByTestId("s16-receipt");
    // R2 P1-13: only the receipt remains.
    expect(screen.queryByTestId("s16-manifest")).toBeNull();
    expect(screen.queryByTestId("s16-legal-holds")).toBeNull();
    expect(screen.queryByTestId("s16-approvals")).toBeNull();
    expect(screen.queryByTestId("s16-commit")).toBeNull();
    expect(screen.queryByTestId("s16-reference")).toBeNull();
    expect(screen.queryByTestId("s16-application-reference")).toBeNull();
    expect(screen.getByTestId("s16-complete-only")).toBeVisible();
    expect(screen.getByTestId("s16-receipt-result")).toHaveTextContent("deleted");
    expect(router.calls.length).toBeGreaterThan(0);
  });

  it("clears local S16 state on a governance 403 and shows only the error", async () => {
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S16_FORBIDDEN", message: "identity invalid" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    const { client } = renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    await screen.findByTestId("s16-error-forbidden");
    // R2 P1-14: the manifest, reference, approvals and commit surfaces are
    // gone; only the authorization error remains.
    expect(screen.queryByTestId("s16-manifest")).toBeNull();
    expect(screen.queryByTestId("s16-reference")).toBeNull();
    expect(screen.queryByTestId("s16-legal-holds")).toBeNull();
    expect(screen.queryByTestId("s16-approvals")).toBeNull();
    expect(screen.queryByTestId("s16-commit")).toBeNull();
    expect(client.getQueryData(["s16", "deletions", "s16req_test_00000001"])).toBeUndefined();
    expect(router.calls.some((call) => call.url === QUERY_PATH)).toBe(true);
  });

  it("invalidates the governance identity on a receipt query 403", async () => {
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: {
                job_id: "s16job_1",
                status: "complete",
                attempt: 1,
                fence: 1,
                lease_owner: null,
                pending_owner_fingerprints: { s01: 4 },
                owner_results: { s01: "complete" },
                stable_failure: null,
                completed_at: 1_800_000_001,
              },
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${RECEIPT_PATH}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S16_FORBIDDEN", message: "identity invalid" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    // R3 (P1-15): the receipt 403 invalidates the whole governance surface.
    await screen.findByTestId("s16-error-forbidden");
    expect(screen.queryByTestId("s16-reference")).toBeNull();
    expect(screen.queryByTestId("s16-receipt")).toBeNull();
    expect(router.calls.some((call) => call.url === RECEIPT_PATH)).toBe(true);
  });

  it("invalidates the governance identity on a process mutation 403", async () => {
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: {
                job_id: "s16job_1",
                status: "pending",
                attempt: 1,
                fence: 1,
                lease_owner: null,
                pending_owner_fingerprints: { s01: 4 },
                owner_results: {},
                stable_failure: null,
                completed_at: null,
              },
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${PROCESS_PATH}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S16_FORBIDDEN", message: "identity invalid" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    await screen.findByTestId("s16-process-button");
    await user.click(screen.getByTestId("s16-process-button"));
    await screen.findByTestId("s16-error-forbidden");
    expect(screen.queryByTestId("s16-process-button")).toBeNull();
    expect(router.calls.some((call) => call.url === PROCESS_PATH)).toBe(true);
  });

  it("clears only the approver state on an approver 403", async () => {
    const router = fetchRouter({
      [`POST ${PREFLIGHT_PATH}`]: () =>
        new Response(
          JSON.stringify(s16PreflightPayload()),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`GET ${QUERY_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: {
                job_id: "s16job_1",
                status: "pending",
                attempt: 0,
                fence: 0,
                lease_owner: null,
                pending_owner_fingerprints: { s01: 4 },
                owner_results: {},
                stable_failure: null,
                completed_at: null,
              },
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
      [`POST ${APPROVE_PATH}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S16_FORBIDDEN", message: "approver invalid" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<S16GovernedDeletionPanel />);
    const user = userEvent.setup();
    await user.type(screen.getByTestId("s16-reference"), "APP-REFERENCE-1");
    await user.click(screen.getByTestId("s16-preflight-button"));
    await screen.findByTestId("s16-approver-token");
    await user.type(screen.getByTestId("s16-approver-token"), "stale-approver");
    await user.click(screen.getByTestId("s16-approve-button"));
    // R3 (P1-15): the approver surface 403 clears the approver token and
    // any previous approval error, but the governance surface stays intact.
    await waitFor(() => {
      expect((screen.getByTestId("s16-approver-token") as HTMLInputElement).value).toBe("");
    });
    expect(screen.queryByTestId("s16-error-code")).toBeNull();
    expect(screen.queryByTestId("s16-commit")).not.toBeNull();
    expect(screen.queryByTestId("s16-error-forbidden")).toBeNull();
    expect(router.calls.some((call) => call.url === APPROVE_PATH)).toBe(true);
  });
});
