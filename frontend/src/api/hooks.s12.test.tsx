import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError, isDefinitiveS12Rejection } from "./client";
import {
  useS12Bundle,
  useS12Job,
  useS12JobPoll,
  useS12Plans,
  useS12StartProcess,
  type S12StartProcessResult,
} from "./hooks";
import {
  createQueryClient,
  fetchRouter,
} from "../test-utils";

export const S12_PLAN_ID = "plan-c-1";
export const S12_JOB_ID = "s12job_t14hook000000000000000000";
export const S12_BUNDLE_ID = "s12_bundle_sha256_" + "a".repeat(64);
const DIGEST_64 = "b".repeat(64);

export function s12StatisticsBlock(membership: string, count: number) {
  return {
    membership,
    opportunity_count: count,
    denominators: {
      E: 4,
      E_all: 4,
      applicable_opportunities: 4,
      n_consistent: 4,
      n_inconsistent: 0,
      n_consistent_decisive: 4,
      labelability: 4,
      uncertain_on_inconsistent: 0,
      skipped_rate: 0,
      missing_rate: 0,
      error_rate: 0,
      conditional_fpr: 0,
    },
    prediction_counts: {
      consistent: 4,
      inconsistent: 0,
      uncertain: 0,
      skipped: 0,
      missing: 0,
      error: 0,
    },
    point: {
      coverage: 1,
      false_positive_rate: 0,
      false_negative_rate: 0,
      miss_rate: 0,
      labelability: 1,
      uncertain_on_inconsistent: 0,
      skipped_rate: 0,
      missing_rate: 0,
      error_rate: 0,
      conditional_fpr: 0,
    },
    interval_95_two_sided: {
      coverage: [0.8125, 1],
      false_positive_rate: [0, 0.1875],
      false_negative_rate: [0, 0.1875],
      miss_rate: [0, 0.1875],
      labelability: [0.8125, 1],
      uncertain_on_inconsistent: [0, 0.1875],
      skipped_rate: [0, 0.1875],
      missing_rate: [0, 0.1875],
      error_rate: [0, 0.1875],
      conditional_fpr: [0, 0.1875],
    },
    bounds_95_one_sided: {
      coverage_lower: 0.8125,
      false_positive_rate_upper: 0.1875,
      false_negative_rate_upper: 0.1875,
      miss_rate_upper: 0.1875,
    },
    estimable: true,
    not_estimable_reasons: [],
    conclusion: "consistent",
  };
}

export function s12CatalogPayload() {
  return {
    schema_version: "s12-plan-catalog/1",
    plans: [
      {
        plan_id: S12_PLAN_ID,
        plan_digest: DIGEST_64,
        scope: "C",
        frozen_at: 1700000000,
        budget: { max_opportunities: 10, max_runtime_ms: 5000 },
        stop_rule: "plan-exhausted",
        opportunity_count: 4,
      },
    ],
  };
}

export function s12JobPayload(
  overrides: Record<string, unknown> = {},
  result: Record<string, unknown> = {
    bundle_id: S12_BUNDLE_ID,
    status: "PASS(scope=C)",
    reason_codes: [],
  },
) {
  return {
    schema_version: "s12-job/1",
    job_id: S12_JOB_ID,
    plan_id: S12_PLAN_ID,
    plan_digest: DIGEST_64,
    worker_id: "c-demo-evaluation-worker",
    status: "complete",
    fence: 1,
    attempt_no: 1,
    lease_until: null,
    rerun_of_bundle_id: null,
    result,
    reason_codes: null,
    created_at: 1700000000,
    ...overrides,
  };
}

export function s12ProcessPayload(
  overrides: Record<string, unknown> = {},
) {
  return {
    status: "complete",
    job_id: S12_JOB_ID,
    bundle_id: S12_BUNDLE_ID,
    attempt_no: 1,
    reason_code: null,
    reason_codes: null,
    ...overrides,
  };
}

export function s12BundlePayload(
  overrides: Record<string, unknown> = {},
  status = "PASS(scope=C)",
) {
  const block = s12StatisticsBlock("C", 4);
  return {
    schema_version: "s12-evaluation-bundle/1",
    bundle_id: S12_BUNDLE_ID,
    plan_id: S12_PLAN_ID,
    plan_digest: DIGEST_64,
    job_id: S12_JOB_ID,
    fence: 1,
    attempt_no: 1,
    worker_id: "c-demo-evaluation-worker",
    rerun_of_bundle_id: null,
    run_started_at: 1700000001,
    run_settled_at: 1700000002,
    status,
    scope: "C",
    status_reasons: ["acceptance-holdout eligible: holdout is non-empty"],
    tracks: { R: s12StatisticsBlock("R", 0), C: block },
    views: {
      "R-E2E": s12StatisticsBlock("R-E2E", 0),
      "R-T4-conditional": s12StatisticsBlock("R-T4-conditional", 0),
    },
    mandatory_check_families: { "cross-document": block },
    strata: {
      difficulty: { standard: block },
      data_source: { demo: block },
      document_combination: { single: block },
      perturbation_family: { none: block },
    },
    scope_eligibility: {
      holdout_eligible: true,
      reasons: ["acceptance holdout is non-empty"],
    },
    clusters: [
      {
        cluster_id: "cl-0",
        stratum: "c",
        applications: ["app_r53_bad_engine"],
        usage: "development",
        variants: null,
      },
    ],
    opportunities: [
      {
        opportunity_id: "opp-0",
        track: "C",
        cluster: "cl-0",
        application_id: "app_r53_bad_engine",
        cycle: 1,
        check_id: "R_ENGINE_CROSS",
        target_scope: "C",
        evidence_snapshot_id: "snapshot_sha256_" + "c".repeat(64),
        label: "consistent",
        label_custody: "independent",
        run_id: "run_sha256_" + "d".repeat(64),
        variant_id: null,
      },
    ],
    tracks_declared: {
      R: { opportunities: [] },
      C: { opportunities: ["opp-0"] },
    },
    views_declared: {
      "R-E2E": { opportunities: [] },
      "R-T4-conditional": { opportunities: [] },
    },
    evidence_references: [
      {
        application_id: "app_r53_bad_engine",
        cycle: 1,
        snapshot_id: "snapshot_sha256_" + "c".repeat(64),
        snapshot_digest: "e".repeat(64),
      },
    ],
    label_manifest: {
      schema_version: "s12-label-manifest/1",
      manifest_id: "manifest_sha256_" + "f".repeat(64),
      manifest_digest: "f".repeat(64),
      label_custody: "independent",
      labels: { "opp-0": "consistent" },
    },
    cohort: null,
    predictions: { "opp-0": "consistent" },
    prediction_alphabet: [
      "consistent",
      "inconsistent",
      "uncertain",
      "skipped",
      "missing",
      "error",
    ],
    gold_alphabet: [
      "consistent",
      "inconsistent",
      "indeterminate",
      "not_applicable",
    ],
    errors: [],
    missing_opportunities: [],
    release: {
      release_id: "release_t14_release",
      release_digest: "g".repeat(64),
      checker_build: "t14-checker",
      manifest_id: "manifest_sha256_" + "h".repeat(64),
      manifest_digest: "h".repeat(64),
      protected_baseline_digest: "i".repeat(64),
      applicable_check_ids: ["R_ENGINE_CROSS", "R_VIN_CROSS"],
      applicable_check_count: 2,
      limits: { max_documents: 100, max_pages: 200 },
    },
    environment: {
      python: "3.12",
      checker_artifact_id: "artifact_sha256_" + "j".repeat(64),
      checker_build: "t14-checker",
    },
    stop_rule: "plan-exhausted",
    stop_reason: "plan-exhausted",
    stop_elapsed_ms: 42,
    completed_run_ids: ["run_sha256_" + "d".repeat(64)],
    stop_rule_satisfied: true,
    runner_result_digest: "k".repeat(64),
    evidence_snapshot_ids: ["snapshot_sha256_" + "c".repeat(64)],
    seed: 20260820,
    budget: { max_opportunities: 10, max_runtime_ms: 5000 },
    split: {
      scheme: "cluster_usage_partition",
      usage_partitions: ["development", "calibration", "acceptance_holdout"],
    },
    business_before: {
      lifecycle_revision: 0,
      evidence_revision: 0,
      evidence_count: 0,
      evidence_digest: null,
      current_run_reference: null,
      governance_revision: 0,
      activation_count: 0,
      activation_digest: null,
    },
    business_after: {
      lifecycle_revision: 0,
      evidence_revision: 0,
      evidence_count: 0,
      evidence_digest: null,
      current_run_reference: null,
      governance_revision: 0,
      activation_count: 0,
      activation_digest: null,
    },
    business_deltas: {
      lifecycle_revision: 0,
      evidence_rows: 0,
      evidence_digest: null,
      current_run_pointer: 0,
      policy_revision: 0,
      governance_revision: 0,
    },
    result_digest: "l".repeat(64),
    replay_package: {
      schema_version: "s12-replay-package/1",
      plan: { plan_id: S12_PLAN_ID, plan_digest: DIGEST_64 },
      predictions: { "opp-0": "consistent" },
      errors: [],
      missing_opportunities: [],
      applications: [],
      stop: {
        stop_reason: "plan-exhausted",
        elapsed_ms: 42,
        completed_run_ids: ["run_sha256_" + "d".repeat(64)],
      },
      runner_result_digest: "k".repeat(64),
      status,
      status_reasons: ["acceptance-holdout eligible: holdout is non-empty"],
      scope_eligibility: {
        holdout_eligible: true,
        reasons: ["acceptance holdout is non-empty"],
      },
      tracks_statistics: { R: block, C: block },
      views_statistics: { "R-E2E": block, "R-T4-conditional": block },
      mandatory_family_statistics: { "cross-document": block },
      strata: {
        difficulty: { standard: block },
        data_source: { demo: block },
        document_combination: { single: block },
        perturbation_family: { none: block },
      },
      business_before: {},
      business_after: {},
      business_deltas: {
        lifecycle_revision: 0,
        evidence_rows: 0,
        evidence_digest: null,
        current_run_pointer: 0,
        policy_revision: 0,
        governance_revision: 0,
      },
      result_material: { status, scope: "C" },
    },
    replay_package_digest: "m".repeat(64),
    command: `s12:process:${S12_JOB_ID}`,
    ...overrides,
  };
}

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

async function settleWithTimers(check: () => boolean): Promise<void> {
  for (let index = 0; index < 400 && !check(); index += 1) {
    await vi.advanceTimersByTimeAsync(500);
    await Promise.resolve();
  }
  expect(check()).toBe(true);
}

describe("S12 catalog read hook", () => {
  it("reads the frozen-plan catalog exactly once and surfaces closed errors", async () => {
    let catalogRequests = 0;
    const { jsonResponse } = fetchRouter({
      "GET /controlled/s12/plans": () => {
        catalogRequests += 1;
        return jsonResponse(s12CatalogPayload());
      },
    });
    const client = createQueryClient();
    const { result } = renderHook(() => useS12Plans(), {
      wrapper: wrap(client),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(catalogRequests).toBe(1);
    expect(result.current.data?.plans[0]?.plan_id).toBe(S12_PLAN_ID);
    expect(result.current.data?.plans[0]?.opportunity_count).toBe(4);
  });

  it("never retries a catalog read that failed closed (exactly one request)", async () => {
    let catalogRequests = 0;
    const { errorResponse } = fetchRouter({
      "GET /controlled/s12/plans": () => {
        catalogRequests += 1;
        return errorResponse(403, "S12_FORBIDDEN");
      },
    });
    void errorResponse;
    const client = createQueryClient();
    const { result } = renderHook(() => useS12Plans(), {
      wrapper: wrap(client),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(catalogRequests).toBe(1);
    expect(result.current.error).toBeInstanceOf(HttpError);
    expect((result.current.error as HttpError).status).toBe(403);
    expect((result.current.error as HttpError).errorCode).toBe("S12_FORBIDDEN");
  });
});

describe("S12 start/process hook", () => {
  it("posts only the plan id to start and processes the returned job exactly once", async () => {
    const router = fetchRouter({
      "POST /controlled/s12/jobs/start": () =>
        router.jsonResponse(
          s12JobPayload({ status: "queued" }, { bundle_id: null, status: null, reason_codes: [] }),
        ),
      [`POST /controlled/s12/jobs/${S12_JOB_ID}/process`]: () =>
        router.jsonResponse(s12ProcessPayload()),
    });
    const client = createQueryClient();
    const { result } = renderHook(() => useS12StartProcess(), {
      wrapper: wrap(client),
    });
    result.current.mutate({ plan_id: S12_PLAN_ID });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const calls = router.calls;
    expect(calls.map((call) => `${call.method} ${call.url}`)).toEqual([
      "POST /controlled/s12/jobs/start",
      `POST /controlled/s12/jobs/${S12_JOB_ID}/process`,
    ]);
    expect(calls[0]?.body).toEqual({ plan_id: S12_PLAN_ID });
    const outcome = result.current.data as S12StartProcessResult;
    expect(outcome.job.job_id).toBe(S12_JOB_ID);
    expect(outcome.process.bundle_id).toBe(S12_BUNDLE_ID);
  });

  it("a failed start never creates a replacement job (no retry)", async () => {
    let startRequests = 0;
    const { errorResponse } = fetchRouter({
      "POST /controlled/s12/jobs/start": () => {
        startRequests += 1;
        return errorResponse(503, "S12_UNAVAILABLE");
      },
    });
    void errorResponse;
    const client = createQueryClient();
    const { result } = renderHook(() => useS12StartProcess(), {
      wrapper: wrap(client),
    });
    result.current.mutate({ plan_id: S12_PLAN_ID });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toBeInstanceOf(HttpError);
    expect(startRequests).toBe(1);
  });
});

describe("S12 bounded job polling", () => {
  const jobRoute = (): string => `GET /controlled/s12/jobs/${S12_JOB_ID}`;

  it("polls one job GET per second and stops at the terminal status", async () => {
    vi.useFakeTimers();
    try {
      let jobRequests = 0;
      const router = fetchRouter({
        [jobRoute()]: () => {
          jobRequests += 1;
          return router.jsonResponse(s12JobPayload());
        },
      });
      const client = createQueryClient();
      const { result, rerender } = renderHook(
        ({ active }: { active: boolean }) => ({
          job: useS12Job(S12_JOB_ID),
          poll: useS12JobPoll(S12_JOB_ID, active, {
            intervalMs: 1000,
            maxAttempts: 120,
          }),
        }),
        {
          wrapper: wrap(client),
          initialProps: { active: false as boolean },
        },
      );
      // The initial authoritative read settles before the poll starts, so
      // every poll cycle is one real job GET (the bounded budget counts
      // actual job requests).
      await settleWithTimers(() => result.current.job.isSuccess);
      expect(jobRequests).toBe(1);
      rerender({ active: true });
      await settleWithTimers(() => result.current.poll === "terminal");
      expect(jobRequests).toBe(1); // the initial read already observed terminal
      expect(router.calls.filter((call) => call.method !== "GET")).toEqual([]);
      const settled = jobRequests;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(jobRequests).toBe(settled);
      expect(result.current.job.data?.status).toBe("complete");
    } finally {
      vi.useRealTimers();
    }
  });

  it("renders the bounded end after the fixed cycle limit with no new execution request", async () => {
    vi.useFakeTimers();
    try {
      let jobRequests = 0;
      let processRequests = 0;
      const router = fetchRouter({
        [jobRoute()]: () => {
          jobRequests += 1;
          return router.jsonResponse(
            s12JobPayload({ status: "leased" }, { bundle_id: null, status: null, reason_codes: [] }),
          );
        },
        "POST /controlled/s12/jobs/start": () =>
          router.jsonResponse(
            s12JobPayload({ status: "leased" }, { bundle_id: null, status: null, reason_codes: [] }),
          ),
        [`POST /controlled/s12/jobs/${S12_JOB_ID}/process`]: () => {
          processRequests += 1;
          return router.jsonResponse(s12ProcessPayload({ status: "leased" }));
        },
      });
      const client = createQueryClient();
      const { result, rerender } = renderHook(
        ({ active }: { active: boolean }) => ({
          job: useS12Job(S12_JOB_ID),
          poll: useS12JobPoll(S12_JOB_ID, active, {
            intervalMs: 1000,
            maxAttempts: 3,
          }),
        }),
        {
          wrapper: wrap(client),
          initialProps: { active: false as boolean },
        },
      );
      await settleWithTimers(() => result.current.job.isSuccess);
      expect(jobRequests).toBe(1);
      rerender({ active: true });
      await settleWithTimers(() => result.current.poll === "timed_out");
      // Initial read + exactly maxAttempts poll cycles, then the bound ends
      // forever with no further job or execution request.
      expect(jobRequests).toBe(4);
      expect(processRequests).toBe(0);
      expect(router.calls.filter((call) => call.method !== "GET")).toEqual([]);
      const settled = jobRequests;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(jobRequests).toBe(settled);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops immediately on a definitive S12 rejection", async () => {
    vi.useFakeTimers();
    try {
      let jobRequests = 0;
      const router = fetchRouter({
        [jobRoute()]: () => {
          jobRequests += 1;
          return router.errorResponse(404, "S12_NOT_FOUND");
        },
      });
      const client = createQueryClient();
      const { result, rerender } = renderHook(
        ({ active }: { active: boolean }) => ({
          job: useS12Job(S12_JOB_ID),
          poll: useS12JobPoll(S12_JOB_ID, active, {
            intervalMs: 1000,
            maxAttempts: 120,
          }),
        }),
        {
          wrapper: wrap(client),
          initialProps: { active: false as boolean },
        },
      );
      await settleWithTimers(() => result.current.job.isError);
      expect(jobRequests).toBe(1);
      rerender({ active: true });
      await settleWithTimers(() => result.current.poll === "terminal");
      expect(jobRequests).toBe(2);
      const settled = jobRequests;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(jobRequests).toBe(settled);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("S12 bundle read hook", () => {
  it("reads the sealed bundle only after a bundle id exists", async () => {
    let bundleRequests = 0;
    const router = fetchRouter({
      [`GET /controlled/s12/bundles/${S12_BUNDLE_ID}`]: () => {
        bundleRequests += 1;
        return router.jsonResponse(s12BundlePayload());
      },
    });
    const client = createQueryClient();
    const { result, rerender } = renderHook(
      ({ bundleId }: { bundleId: string | null }) => useS12Bundle(bundleId),
      {
        wrapper: wrap(client),
        initialProps: { bundleId: null as string | null },
      },
    );
    expect(result.current.isFetching).toBe(false);
    rerender({ bundleId: S12_BUNDLE_ID });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(bundleRequests).toBe(1);
    expect(result.current.data?.status).toBe("PASS(scope=C)");
  });
});

describe("S12 definitive rejection classifier", () => {
  it("treats the four closed envelopes as definitive and no others", () => {
    expect(
      isDefinitiveS12Rejection(new HttpError(403, { error: "S12_FORBIDDEN" })),
    ).toBe(true);
    expect(
      isDefinitiveS12Rejection(new HttpError(404, { error: "S12_NOT_FOUND" })),
    ).toBe(true);
    expect(
      isDefinitiveS12Rejection(
        new HttpError(422, { error: "S12_INVALID_COMMAND" }),
      ),
    ).toBe(true);
    expect(
      isDefinitiveS12Rejection(
        new HttpError(503, { error: "S12_UNAVAILABLE" }),
      ),
    ).toBe(true);
    expect(isDefinitiveS12Rejection(new HttpError(403, { error: "S01_FORBIDDEN" }))).toBe(
      false,
    );
    expect(isDefinitiveS12Rejection(new HttpError(503, { error: "other" }))).toBe(
      false,
    );
    expect(isDefinitiveS12Rejection(new HttpError(500, { error: "x" }))).toBe(
      false,
    );
    expect(isDefinitiveS12Rejection(new Error("network"))).toBe(false);
  });
});
