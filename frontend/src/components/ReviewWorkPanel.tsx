import { useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { components } from "../generated/api";
import {
  HttpError,
  isDefinitiveRejection,
  isDefinitiveS05Rejection,
  type ApplicationHistoryResponse,
  type CurrentRouteResponse,
  type ReviewWorkResponse,
  type WorkspaceResponse,
} from "../api/client";
import {
  MANUAL_WORK_KEY,
  HISTORY_KEY,
  ROUTE_KEY,
  SUPPLEMENT_REQUEST_KEY,
  useApplicationHistory,
  useClaimWorkItem,
  useCorrectFieldObservation,
  useCorrectPageMembership,
  useCorrectionConvergence,
  useCurrentRoute,
  useEvidenceConvergence,
  useManualWork,
  useReleaseWorkItem,
  useRenewWorkItem,
  useRequestBusinessException,
  useRequestSupplement,
  useRevealFieldObservation,
  useSubmitVerification,
  useSupplementRequest,
  useWorkspace,
  type BusinessExceptionRequestResult,
  type ClaimCommand,
  type CorrectionCommand,
  type CorrectionConvergence,
  type CorrectionResult,
  type ExceptionRequestCommand,
  type FencedCommand,
  type MembershipCommand,
  type MembershipResult,
  type RevealCommand,
  type RevealResult,
  type SubmitCommand,
  type SupplementRequestCommand,
  type SupplementRequestResult,
  type SupplementRequestView,
} from "../api/hooks";
import { Button } from "./ui/button";
import GateSection from "./GateSection";

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** The server-owned governed-release pin value.  Every governed field is
 * optional on the S01HistoryRun type (legacy runs predate the governed
 * release), so absent values render a placeholder and the browser never
 * invents a candidate, digest, or release. */
function pinText(value: string | number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}

/** True when the run carries any server-governed release identity.  Legacy
 * runs (which always carry a release id/digest and runtime components but
 * never the governed candidate/manifest/validation/approval/activation
 * fields) render exactly as before with no pin block, so the governed pin
 * never changes the legacy history DOM. */
function hasGovernedPin(
  run: components["schemas"]["S01HistoryRun"],
): boolean {
  return [
    run.candidate_id,
    run.manifest_id,
    run.manifest_digest,
    run.validation_bundle_id,
    run.validation_bundle_digest,
    run.approval_binding_id,
    run.approval_binding_digest,
    run.activation_event_id,
    run.active_generation,
  ].some((value) => value !== null && value !== undefined);
}

type S01EvidenceLink = components["schemas"]["S01EvidenceLink"];
type S01WorkspaceMembershipPage = components["schemas"]["S01WorkspaceMembershipPage"];

type Action =
  | "claim"
  | "renew"
  | "release"
  | "submit"
  | "reveal"
  | "correct"
  | "membership"
  | "supplement"
  | "exception";

type Outcome = "confirmed" | "not_confirmed" | "inconclusive";

/** The closed reason vocabulary of the S01 domain authority, bound to the
 * generated OpenAPI literal so an unsupported reason fails typecheck. */
type CorrectionReason = CorrectionCommand["correction"]["reason_code"];

const OUTCOMES: readonly Outcome[] = [
  "confirmed",
  "not_confirmed",
  "inconclusive",
];

/** The registered correction reasons of the S01 domain authority. */
const CORRECTION_REASONS: readonly CorrectionReason[] = [
  "SOURCE_VALUE_MISREAD",
  "SOURCE_VALUE_MISSING",
];

/** The discriminated membership-decision vocabulary of the S01 domain
 * authority (S10), bound to the generated OpenAPI literals. */
type MembershipPayload = MembershipCommand["membership"];
type MembershipAccept = Extract<MembershipPayload, { decision: "accept" }>;
type MembershipUnassign = Extract<MembershipPayload, { decision: "unassign" }>;
type MembershipReason = MembershipPayload["reason_code"];

const MEMBERSHIP_ACCEPT_REASONS: readonly MembershipAccept["reason_code"][] = [
  "MEMBERSHIP_SOURCE_VERIFIED",
  "MEMBERSHIP_SOURCE_MISASSIGNED",
  "MEMBERSHIP_INSTANCE_WRONG",
];

const MEMBERSHIP_UNASSIGN_REASONS: readonly MembershipUnassign["reason_code"][] = [
  "MEMBERSHIP_SOURCE_VERIFIED",
  "MEMBERSHIP_SOURCE_MISASSIGNED",
  "MEMBERSHIP_PAGE_UNASSIGNED",
];

const MEMBERSHIP_STATE_LABELS: Record<
  S01WorkspaceMembershipPage["state"],
  string
> = {
  unresolved: "未解析",
  ambiguous: "歧义（多候选并存）",
  selected: "已归属",
  unassigned: "显式未归集",
};

type MembershipTarget = {
  findingId: string;
  membership: Pick<
    S01WorkspaceMembershipPage,
    | "attachment_id"
    | "page_source_sha256"
    | "page_ordinal"
    | "state"
    | "candidates"
    | "active_decision_ids"
    | "source_evidence"
  >;
};

/** The open membership decision draft for one server-owned ledger target.
 * ``authorityKey`` is the ephemeral serialized work + workspace authority the
 * draft was issued under; a successful refetch that changes any covered
 * server fact closes the draft. */
type MembershipDraft = {
  target: MembershipTarget;
  decision: "accept" | "unassign";
  claimId: string | null;
  reason: MembershipReason;
  authorityKey: string;
};

/** One indivisible reveal/correction authorization: application, work,
 * observation, the complete generated expected context, evidence/lifecycle
 * revisions, claim fence, and claim expiry.  Restricted values may exist
 * only while this exact token is live and unexpired; its key is never
 * persisted and is not a secret. */
type IssuedContextToken = {
  applicationId: string;
  workItemId: string;
  observationId: string;
  expectedContext: ReviewWorkResponse["command_context"];
  evidenceRevision: number;
  lifecycleRevision: number;
  claimFence: number;
  claimExpiresAt: number;
};

function contextTokenKey(token: IssuedContextToken): string {
  return [
    token.applicationId,
    token.workItemId,
    token.observationId,
    JSON.stringify(token.expectedContext),
    token.evidenceRevision,
    token.lifecycleRevision,
    token.claimFence,
    token.claimExpiresAt,
  ].join("|");
}

/** The exact issuance of a restricted command: the token it was authorized
 * under and the observation it targets.  Response storage and replay both
 * require the live ref to be this exact object. */
type IssuedCommand = {
  tokenKey: string;
  observationId: string;
};

/** The restricted reveal payload, keyed to the exact issued token it was
 * authorized under.  It may render only while that token is still live; it
 * is never stored in a query cache, URL, or storage. */
type RevealState = {
  tokenKey: string;
  observationId: string;
  sourceText: string;
};

/** The source-backed correction target pinned to one workspace observation
 * and to the exact token that authorized the draft. */
type CorrectionTarget = {
  tokenKey: string;
  findingId: string;
  observationId: string;
  documentId: string;
  documentRole: string;
  field: string;
  sourceSha256: string;
  sourcePage: number;
  sourceRegion: string;
};

type PendingCommand =
  | { action: "claim"; command: ClaimCommand }
  | { action: "renew"; command: FencedCommand }
  | { action: "release"; command: FencedCommand }
  | { action: "submit"; command: SubmitCommand }
  | { action: "reveal"; command: RevealCommand }
  | { action: "correct"; command: CorrectionCommand }
  | { action: "membership"; command: MembershipCommand }
  | { action: "supplement"; command: SupplementRequestCommand }
  | { action: "exception"; command: ExceptionRequestCommand };

const ACTION_LABELS: Record<Action, string> = {
  claim: "认领",
  renew: "续期",
  release: "释放",
  submit: "核验",
  reveal: "揭示",
  correct: "更正",
  membership: "页归属",
  supplement: "补充请求",
  exception: "请求业务例外",
};

/** Builds the structured manual verification from the Reviewer's explicit
 * outcome choice; every finding decision carries that same chosen value and
 * no automatic verdict/route/target is ever copied into the decision. */
function buildVerification(
  work: ReviewWorkResponse,
  outcome: Outcome,
): SubmitCommand["verification"] {
  return {
    schema_version: "human-decision/1",
    outcome,
    reason_code: "HUMAN_REVIEW_COMPLETED",
    finding_decisions: work.automatic_findings.map((finding) => ({
      finding_id: finding.finding_id,
      outcome,
    })),
  };
}

function WorkFacts({ work }: { work: ReviewWorkResponse }) {
  return (
    <section aria-labelledby="review-facts-title">
      <h3 id="review-facts-title">工作项（服务端权威）</h3>
      <dl className="facts">
        <div>
          <dt>状态</dt>
          <dd data-testid="review-status">{work.status}</dd>
        </div>
        <div>
          <dt>阶段</dt>
          <dd data-testid="review-phase">{work.phase}</dd>
        </div>
        <div>
          <dt>路由</dt>
          <dd data-testid="review-route">{work.route}</dd>
        </div>
        <div>
          <dt>认领围栏</dt>
          <dd data-testid="review-claim-fence">{work.claim_fence}</dd>
        </div>
        <div>
          <dt>认领过期（epoch）</dt>
          <dd data-testid="review-claim-expiry">{work.claim_expires_at}</dd>
        </div>
        <div>
          <dt>生命周期修订</dt>
          <dd data-testid="review-lifecycle-revision">
            {work.lifecycle_revision}
          </dd>
        </div>
        <div>
          <dt>证据修订</dt>
          <dd data-testid="review-evidence-revision">
            {work.evidence_revision}
          </dd>
        </div>
      </dl>
      <h4>自动发现（不改写）</h4>
      <ul data-testid="review-findings">
        {work.automatic_findings.map((finding) => (
          <li key={finding.finding_id} data-testid="review-finding">
            <span data-testid="review-finding-rule">{finding.rule_id}</span>
            {" · "}
            <span data-testid="review-finding-verdict">{finding.verdict}</span>
            {" · "}
            {finding.severity}
            {" · "}
            {finding.reason_code}
          </li>
        ))}
      </ul>
      <dl className="facts">
        <div>
          <dt>运行权威摘要</dt>
          <dd data-testid="review-run-digest">
            {work.run_authority.authority_digest}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function WorkspaceSection({
  work,
  workspace,
  claimed,
  claimExpired,
  liveTokenKey,
  revealed,
  correctionTarget,
  correctionRaw,
  correctionReason,
  correctionReasons,
  revealPending,
  correctPending,
  controlsDisabled,
  membershipDraft,
  membershipPending,
  onReveal,
  onStartCorrection,
  onCorrectionRawChange,
  onCorrectionReasonChange,
  onSubmitCorrection,
  onCancelCorrection,
  onStartMembership,
  onMembershipDecision,
  onMembershipCandidate,
  onMembershipReasonChange,
  onSubmitMembership,
}: {
  work: ReviewWorkResponse;
  workspace: UseQueryResult<WorkspaceResponse>;
  claimed: boolean;
  claimExpired: boolean;
  liveTokenKey: (observationId: string) => string | null;
  revealed: RevealState | null;
  correctionTarget: CorrectionTarget | null;
  correctionRaw: string;
  correctionReason: CorrectionReason;
  correctionReasons: readonly CorrectionReason[];
  revealPending: boolean;
  correctPending: boolean;
  controlsDisabled: boolean;
  membershipDraft: MembershipDraft | null;
  membershipPending: boolean;
  onReveal: (link: S01EvidenceLink) => void;
  onStartCorrection: (link: S01EvidenceLink, findingId: string) => void;
  onCorrectionRawChange: (raw: string) => void;
  onCorrectionReasonChange: (reason: CorrectionReason) => void;
  onSubmitCorrection: () => void;
  onCancelCorrection: () => void;
  onStartMembership: (target: MembershipTarget) => void;
  onMembershipDecision: (decision: "accept" | "unassign") => void;
  onMembershipCandidate: (claimId: string) => void;
  onMembershipReasonChange: (reason: MembershipReason) => void;
  onSubmitMembership: () => void;
}) {
  if (work.status === "completed") {
    return (
      <section data-testid="review-workspace-gone" className="panel-inline">
        <p className="text-sm text-muted-foreground">
          人工核验已完成：工作区已随生命周期结束
        </p>
      </section>
    );
  }
  if (workspace.isPending) {
    return (
      <section data-testid="review-workspace-loading">
        <p>工作区加载中…</p>
      </section>
    );
  }
  if (workspace.isError || workspace.data === undefined) {
    const notFound =
      workspace.error instanceof HttpError && workspace.error.status === 404;
    return (
      <section data-testid="review-workspace-error">
        <p>{notFound ? "工作区未找到或无权访问" : "工作区不可用"}</p>
      </section>
    );
  }
  const finding = workspace.data.selected_finding ?? null;
  const findingId = finding?.finding_id ?? null;
  const membership =
    membershipDraft?.target.membership ??
    (finding === null ? null : (finding.membership ?? null));
  const membershipLedger = workspace.data.membership_ledger ?? [];
  return (
    <section aria-labelledby="review-workspace-title">
      <h3 id="review-workspace-title">最小工作区（发现优先）</h3>
          <dl className="facts" data-testid="review-workspace-facts">
            <div>
              <dt>认领过期（epoch）</dt>
              <dd data-testid="review-workspace-expiry">
                {workspace.data.claim_expires_at}
              </dd>
            </div>
            <div>
              <dt>生命周期修订</dt>
              <dd data-testid="review-workspace-lifecycle">
                {workspace.data.lifecycle_revision}
              </dd>
            </div>
            <div>
              <dt>证据修订</dt>
              <dd data-testid="review-workspace-evidence-revision">
                {workspace.data.evidence_revision}
              </dd>
            </div>
            <div>
              <dt>投影水位</dt>
              <dd data-testid="review-workspace-watermark">
                {workspace.data.projection_watermark}
              </dd>
            </div>
            <div>
              <dt>当前运行</dt>
          <dd data-testid="review-workspace-current-run">
            {workspace.data.current_run_id ?? "None"}
          </dd>
        </div>
        <div>
          <dt>证据快照</dt>
          <dd data-testid="review-workspace-snapshot">
            {workspace.data.evidence_snapshot_id ?? "None"}
          </dd>
        </div>
      </dl>
      {membershipLedger.length > 0 && (
        <section data-testid="review-membership-ledger" aria-labelledby="review-membership-ledger-title">
          <h4 id="review-membership-ledger-title">单据页归属账本（服务端权威）</h4>
          <ol className="history-list">
            {membershipLedger.map((page) => {
              const correctable = workspace.data.mandatory_blockers.find(
                (blocker) => blocker.finding_id === page.finding_id,
              );
              const target =
                correctable?.membership != null
                  ? {
                      findingId: correctable.finding_id,
                      membership: correctable.membership,
                    }
                  : page.finding_id != null
                    ? { findingId: page.finding_id, membership: page }
                    : null;
              return (
                <li
                  key={`${page.attachment_id}:${page.page_ordinal}`}
                  data-testid="review-membership-ledger-page"
                >
                  {page.attachment_id} · 页 {page.page_ordinal} · {page.state} ·{" "}
                  {page.page_source_sha256.slice(0, 12)}…
                  {target !== null && claimed && !controlsDisabled && (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => onStartMembership(target)}
                      aria-label={`选择附件 ${page.attachment_id} 第 ${page.page_ordinal} 页`}
                    >
                      选择
                    </Button>
                  )}
                  <ul>
                    {page.candidates.map((candidate) => (
                      <li key={candidate.claim_id} data-testid="review-membership-ledger-candidate">
                        claim {candidate.claim_id} · {candidate.document_instance_id} ·{" "}
                        {candidate.document_role} ·{" "}
                        {Object.entries(candidate.provenance ?? {})
                          .map(([key, value]) => `${key}=${value}`)
                          .join(" · ")}
                      </li>
                    ))}
                    {page.decisions.map((decision) => (
                      <li key={decision.decision_id} data-testid="review-membership-ledger-decision">
                        {decision.record_kind} · {decision.decision_id} · {decision.status} ·{" "}
                        {decision.document_instance_id ?? "未归属"} ·{" "}
                        {decision.document_role ?? "未归属"} · {decision.reason_code} ·{" "}
                        周期 {decision.cycle} ·{" "}
                        supersedes {decision.supersedes.join(", ") || "None"} ·{" "}
                        {JSON.stringify(decision.source_evidence)}
                      </li>
                    ))}
                  </ul>
                </li>
              );
            })}
          </ol>
        </section>
      )}
      {finding === null || findingId === null ? (
        <p data-testid="review-workspace-empty" className="text-sm text-muted-foreground">
          无可复核发现
        </p>
      ) : (
        <>
          <dl className="facts">
            <div>
              <dt>规则</dt>
              <dd data-testid="review-workspace-rule">{finding.rule_id}</dd>
            </div>
            <div>
              <dt>判定</dt>
              <dd data-testid="review-workspace-verdict">{finding.verdict}</dd>
            </div>
            <div>
              <dt>严重度</dt>
              <dd data-testid="review-workspace-severity">{finding.severity}</dd>
            </div>
            <div>
              <dt>原因</dt>
              <dd data-testid="review-workspace-reason">{finding.reason_code}</dd>
            </div>
          </dl>
          {membership != null && (
            <section
              className="membership-panes"
              data-testid="review-membership"
            >
              <h4>单据页归属（S10）</h4>
              <p className="text-sm text-muted-foreground">
                页 {membership.page_ordinal} ·{" "}
                {membership.page_source_sha256.slice(0, 12)}… · 状态{" "}
                {MEMBERSHIP_STATE_LABELS[membership.state]}
              </p>
              <div className="membership-panes-grid">
                <div className="membership-pane membership-candidates">
                  <h5>并存候选（只读，绝不自动选择）</h5>
                  <ul>
                    {membership.candidates.map((candidate) => (
                      <li key={candidate.claim_id} data-testid="review-membership-candidate">
                        <span>{candidate.document_role}</span> ·{" "}
                        <span data-testid="review-membership-candidate-instance">
                          {candidate.document_instance_id}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {" "}
                          claim {candidate.claim_id}
                          {" · "}
                          <span data-testid="review-membership-candidate-provenance">
                            {Object.entries(candidate.provenance ?? {})
                              .map(([key, value]) => `${key}=${value}`)
                              .join(" · ")}
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="membership-pane membership-decide">
                  <h5>审核员决定</h5>
                  {claimExpired ? (
                    <p
                      className="text-xs text-muted-foreground"
                      data-testid="review-membership-expired"
                    >
                      认领已过期，请重新加载权威上下文
                    </p>
                  ) : !claimed || controlsDisabled ? (
                    <p className="text-xs text-muted-foreground">
                      认领后即可决定
                    </p>
                  ) : membershipDraft === null ? (
                    <Button
                      variant="outline"
                      size="sm"
                      data-testid="review-membership-start"
                      disabled={controlsDisabled}
                      onClick={() =>
                        onStartMembership({
                          findingId: finding.finding_id,
                          membership,
                        })
                      }
                    >
                      开始页归属修正
                    </Button>
                  ) : (
                    <div
                      className="membership-form"
                      data-testid="review-membership-form"
                    >
                      <label className="membership-radio">
                        <input
                          type="radio"
                          name="membership-decision"
                          checked={membershipDraft.decision === "accept"}
                          onChange={() => onMembershipDecision("accept")}
                          data-testid="review-membership-accept-radio"
                        />
                        接受候选实例
                      </label>
                      <label className="membership-radio">
                        <input
                          type="radio"
                          name="membership-decision"
                          checked={membershipDraft.decision === "unassign"}
                          onChange={() => onMembershipDecision("unassign")}
                          data-testid="review-membership-unassign-radio"
                        />
                        显式未归集
                      </label>
                      {/* Both decision variants bind candidate_claim_id, so
                       * the explicit claim selector stays visible and the
                       * Reviewer must choose one server-authorized claim. */}
                      <select
                        aria-label="候选实例"
                        value={membershipDraft.claimId ?? ""}
                        onChange={(event) =>
                          onMembershipCandidate(event.target.value)
                        }
                        data-testid="review-membership-candidate-select"
                      >
                        <option value="" disabled>
                          请选择候选实例
                        </option>
                        {membership.candidates.map((candidate) => (
                          <option
                            key={candidate.claim_id}
                            value={candidate.claim_id}
                          >
                            {candidate.document_role} ·{" "}
                            {candidate.document_instance_id}
                          </option>
                        ))}
                      </select>
                      <select
                        aria-label="原因"
                        value={membershipDraft.reason}
                        onChange={(event) =>
                          onMembershipReasonChange(
                            event.target.value as MembershipReason,
                          )
                        }
                        data-testid="review-membership-reason"
                      >
                        {(membershipDraft.decision === "accept"
                          ? MEMBERSHIP_ACCEPT_REASONS
                          : MEMBERSHIP_UNASSIGN_REASONS
                        ).map((reason) => (
                          <option key={reason} value={reason}>
                            {reason}
                          </option>
                        ))}
                      </select>
                      <Button
                        size="sm"
                        data-testid="review-membership-submit"
                        disabled={
                          membershipPending ||
                          membershipDraft.claimId === null ||
                          membershipDraft.claimId === ""
                        }
                        onClick={onSubmitMembership}
                      >
                        提交页归属修正
                      </Button>
                    </div>
                  )}
                </div>
              </div>
            </section>
          )}
          <h4>证据（已掩码）</h4>
          <ul data-testid="review-evidence-links">
            {finding.evidence_links.map((link) => {
              const revealedHere =
                revealed !== null &&
                revealed.observationId === link.observation_id &&
                revealed.tokenKey === liveTokenKey(link.observation_id);
              const correctable =
                typeof link.source_sha256 === "string" &&
                typeof link.source_page === "number" &&
                typeof link.source_region === "string";
              const formHere =
                correctionTarget !== null &&
                correctionTarget.observationId === link.observation_id;
              return (
                <li
                  key={link.observation_id}
                  data-testid="review-evidence-link"
                >
                  {link.document_id} · {link.field} ·{" "}
                  <span data-testid="review-evidence-masked">
                    {link.raw_masked ?? link.value_state}
                  </span>
                  <dl className="facts">
                    <div>
                      <dt>文档角色</dt>
                      <dd data-testid="review-evidence-role">
                        {link.document_role}
                      </dd>
                    </div>
                    <div>
                      <dt>来源页</dt>
                      <dd data-testid="review-evidence-source-page">
                        {link.source_page ?? "None"}
                      </dd>
                    </div>
                    <div>
                      <dt>来源区域</dt>
                      <dd data-testid="review-evidence-source-region">
                        {link.source_region ?? "None"}
                      </dd>
                    </div>
                    <div>
                      <dt>来源出处摘要</dt>
                      <dd data-testid="review-evidence-provenance">
                        {link.provenance_manifest_digest ?? "None"}
                      </dd>
                    </div>
                    <div>
                      <dt>证据资格</dt>
                      <dd data-testid="review-evidence-eligibility">
                        {link.evidence_eligible === true
                          ? (link.eligibility_reason ?? "eligible")
                          : "ineligible"}
                      </dd>
                    </div>
                  </dl>
                  <div className="evidence-actions">
                    {claimed && (
                      <>
                        {revealPending ? (
                          <span data-testid="review-reveal-pending">
                            来源读取中…
                          </span>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            data-testid="review-reveal-button"
                            disabled={controlsDisabled}
                            onClick={() => onReveal(link)}
                          >
                            查看来源
                          </Button>
                        )}
                        {revealedHere && (
                          <p
                            className="text-sm"
                            data-testid="review-reveal-source"
                          >
                            {revealed.sourceText}
                          </p>
                        )}
                        <Button
                          variant="outline"
                          size="sm"
                          data-testid="review-correct-button"
                          disabled={controlsDisabled || !correctable}
                          onClick={() => onStartCorrection(link, findingId)}
                        >
                          更正该字段
                        </Button>
                        {formHere && (
                          <div
                            className="evidence-correction"
                            data-testid="review-correction-form"
                          >
                            <label className="text-sm">
                              修正值
                              <input
                                data-testid="review-correction-raw"
                                value={correctionRaw}
                                onChange={(event) =>
                                  onCorrectionRawChange(event.target.value)
                                }
                                disabled={correctPending}
                              />
                            </label>
                            <label className="text-sm">
                              原因
                              <select
                                data-testid="review-correction-reason"
                                value={correctionReason}
                                onChange={(event) =>
                                  onCorrectionReasonChange(
                                    event.target.value as CorrectionReason,
                                  )
                                }
                                disabled={correctPending}
                              >
                                {correctionReasons.map((reason) => (
                                  <option key={reason} value={reason}>
                                    {reason}
                                  </option>
                                ))}
                              </select>
                            </label>
                            <Button
                              size="sm"
                              data-testid="review-correction-submit"
                              disabled={
                                correctPending || correctionRaw.length === 0
                              }
                              onClick={onSubmitCorrection}
                            >
                              {correctPending ? "提交中…" : "提交修正"}
                            </Button>
                            <Button
                              variant="outline"
                              size="sm"
                              data-testid="review-correction-cancel"
                              disabled={correctPending}
                              onClick={onCancelCorrection}
                            >
                              取消
                            </Button>
                          </div>
                        )}
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
    </section>
  );
}

function HistorySection({
  history,
}: {
  history: UseQueryResult<ApplicationHistoryResponse>;
}) {
  if (history.isPending) {
    return <p data-testid="review-history-loading">历史加载中…</p>;
  }
  if (history.isError || history.data === undefined) {
    const notFound =
      history.error instanceof HttpError && history.error.status === 404;
    return (
      <p data-testid="review-history-error">
        {notFound ? "历史未找到或无权访问" : "历史不可用"}
      </p>
    );
  }
  const current = history.data.runs.find((run) => run.current === true);
  return (
    <section className="panel" data-testid="history-panel" aria-labelledby="review-history-title">
      <h3 id="review-history-title">历史（服务端权威）</h3>
      <ol data-testid="review-history-runs">
        {history.data.runs.map((run) => (
          <li key={run.run_id} data-testid="review-history-run">
            {run.run_id}
            {" · "}
            {run.status}
            {" · "}
            {run.currentness_reason}
            {run.current === true ? " · 当前" : " · 非当前"}
            {hasGovernedPin(run) && (
              <div data-testid="review-run-governed-pin">
                <h4>治理发布固定（服务端权威）</h4>
                <dl className="facts">
                  <div>
                    <dt>候选</dt>
                    <dd data-testid="review-run-candidate">
                      {pinText(run.candidate_id)}
                    </dd>
                  </div>
                <div>
                  <dt>清单</dt>
                  <dd data-testid="review-run-manifest">
                    {pinText(run.manifest_id)}
                  </dd>
                </div>
                <div>
                  <dt>清单摘要</dt>
                  <dd data-testid="review-run-manifest-digest">
                    {pinText(run.manifest_digest)}
                  </dd>
                </div>
                <div>
                  <dt>验证包</dt>
                  <dd data-testid="review-run-validation-bundle">
                    {pinText(run.validation_bundle_id)}
                  </dd>
                </div>
                <div>
                  <dt>验证包摘要</dt>
                  <dd data-testid="review-run-validation-bundle-digest">
                    {pinText(run.validation_bundle_digest)}
                  </dd>
                </div>
                <div>
                  <dt>审批绑定</dt>
                  <dd data-testid="review-run-approval-binding">
                    {pinText(run.approval_binding_id)}
                  </dd>
                </div>
                <div>
                  <dt>审批绑定摘要</dt>
                  <dd data-testid="review-run-approval-binding-digest">
                    {pinText(run.approval_binding_digest)}
                  </dd>
                </div>
                <div>
                  <dt>激活事件</dt>
                  <dd data-testid="review-run-activation-event">
                    {pinText(run.activation_event_id)}
                  </dd>
                </div>
                <div>
                  <dt>激活代次</dt>
                  <dd data-testid="review-run-active-generation">
                    {pinText(run.active_generation)}
                  </dd>
                </div>
                <div>
                  <dt>发布</dt>
                  <dd data-testid="review-run-release">
                    {pinText(run.release_id)}
                  </dd>
                </div>
                <div>
                  <dt>发布摘要</dt>
                  <dd data-testid="review-run-release-digest">
                    {pinText(run.release_digest)}
                  </dd>
                </div>
                <div>
                  <dt>组件</dt>
                  <dd data-testid="review-run-components">
                    {run.components == null || run.components.length === 0 ? (
                      "—"
                    ) : (
                      <ul>
                        {run.components.map((component) => (
                          <li
                            key={`${component.type}:${component.id}`}
                            data-testid="review-run-component"
                          >
                            {component.type}
                            {" · "}
                            {component.id}
                            {" · "}
                            {component.digest}
                          </li>
                        ))}
                      </ul>
                    )}
                  </dd>
                </div>
              </dl>
              </div>
            )}
          </li>
        ))}
      </ol>
      {history.data.corrections.length > 0 && (
        <>
          <h4>证据更正</h4>
          <ul data-testid="review-history-corrections">
            {history.data.corrections.map((correction) => (
              <li
                key={correction.correction_id}
                data-testid="review-history-correction"
              >
                {correction.correction_id}
                {" · "}
                {correction.superseded_observation_id}
                {" → "}
                {correction.successor_observation_id}
                {" · "}
                {correction.document_id}
                {" · "}
                {correction.field}
                {" · "}
                {correction.reason_code}
                {" · "}
                {correction.actor}
                {" · "}
                证据修订 {correction.evidence_revision}
              </li>
            ))}
          </ul>
        </>
      )}
      {(history.data.memberships?.length ?? 0) > 0 && (
        <>
          <h4>单据页归属账本</h4>
          <ul className="history-list" data-testid="review-history-memberships">
            {history.data.memberships?.map((record, index) => (
              <li
                key={record.membership_id ?? record.decision_id ?? record.claim_id ?? index}
                data-testid="review-history-membership"
              >
                {record.record_kind} · {record.page.attachment_id} · 页{" "}
                {record.page.page_ordinal} · {record.claim_id ?? record.decision_id ?? "None"} ·{" "}
                {record.candidate_document?.document_instance_id ??
                  record.document_instance_id ??
                  "未归属"} ·{" "}
                {record.candidate_document?.document_role ??
                  record.document_role ??
                  "未归属"} · {record.status ?? "claim"}
                {record.cycle != null && <> · 周期 {record.cycle}</>} · supersedes{" "}
                {record.supersedes?.join(", ") || "None"} ·{" "}
                {JSON.stringify(record.provenance ?? record.source_evidence ?? {})}
              </li>
            ))}
          </ul>
        </>
      )}
      {(history.data.membership_history?.length ?? 0) > 0 && (
        <>
          <h4>单据页归属修正</h4>
          <ul className="history-list" data-testid="review-history-membership-corrections">
            {history.data.membership_history?.map((correction) => (
              <li
                key={correction.correction_id}
                data-testid="review-history-membership-correction"
              >
                {correction.correction_id} · {correction.decision_id} ·{" "}
                {correction.decision} · claim {correction.candidate_claim_id} ·{" "}
                {correction.attachment_id} · 页 {correction.page_ordinal} ·{" "}
                {correction.document_instance_id ?? "未归属"} ·{" "}
                {correction.document_role ?? "未归属"} · {correction.reason_code} ·{" "}
                周期 {correction.cycle} ·{" "}
                supersedes {correction.supersedes?.join(", ") || "None"} ·{" "}
                {JSON.stringify(correction.source_evidence)}
              </li>
            ))}
          </ul>
        </>
      )}
      {history.data.business_exceptions.length > 0 && (
        <>
          <h4>业务例外（服务端权威）</h4>
          <ul className="history-list" data-testid="review-history-exceptions">
            {history.data.business_exceptions.map((item) => (
              <li
                key={item.request_id}
                data-testid="review-history-exception"
              >
                {item.request_id}
                {" · "}
                {item.status}
                {item.current === true ? " · 当前" : " · 非当前"}
                {" · "}
                {item.rule_id}
                {" · "}
                {item.machine_verdict}
                {item.decision !== null && item.decision !== undefined && (
                  <>
                    {" · "}
                    决策 {item.decision}
                  </>
                )}
                {item.route !== null && item.route !== undefined && (
                  <>
                    {" · "}
                    路由 {item.route}
                  </>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      <dl className="facts">
        <div>
          <dt>当前运行决策</dt>
          <dd data-testid="review-history-decisions">
            {current === undefined || current.decision_ids.length === 0
              ? "None"
              : current.decision_ids.join(", ")}
          </dd>
        </div>
      </dl>
      {history.data.attachment_versions.length > 0 && (
        <div>
          <h4>附件版本（服务端权威）</h4>
          <ol data-testid="review-history-attachments">
            {history.data.attachment_versions.map((item) => (
              <li key={item.attachment_id}>
                {item.document_role} · v{item.version}
                {" · "}
                {item.current === true ? "当前" : "非当前"}
                {item.supersedes_attachment_id !== null &&
                  item.supersedes_attachment_id !== undefined && (
                    <>
                      {" · 取代 "}
                      {item.supersedes_attachment_id}
                    </>
                  )}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}

/** The Reviewer's authoritative facts after an accepted business-exception
 * request: the request id, the Lifecycle-owned route, and the request's own
 * history entry rendered verbatim.  The browser never infers a phase.
 * Currentness is query-owned: while either owning query (route/history) is
 * in error, or a manual reload has failed, retained values are never
 * presented as current and a named live region announces the state. */
function ExceptionAcceptedBlock({
  accepted,
  history,
  gate,
  onReload,
  reloadDisabled,
  reloadFailed,
}: {
  accepted: { requestId: string };
  history: UseQueryResult<ApplicationHistoryResponse>;
  gate: UseQueryResult<CurrentRouteResponse>;
  onReload: () => void;
  reloadDisabled: boolean;
  reloadFailed: boolean;
}) {
  const record = history.data?.business_exceptions.find(
    (item) => item.request_id === accepted.requestId,
  );
  const ownersInError = gate.isError || history.isError;
  const unavailable = reloadFailed || ownersInError;
  const statusText = reloadFailed
    ? "不可用（权威重新加载失败）"
    : ownersInError
      ? "不可用（权威读取失败）"
      : record === undefined
        ? "等待服务端投影"
        : `${record.status}${record.current === true ? " · 当前" : " · 非当前"}`;
  const recoveryStatus = reloadFailed
    ? "权威状态不可用（重新加载失败）：请重试权威读取"
    : ownersInError
      ? "权威状态不可用（读取失败）：等待权威读取恢复"
      : "权威状态正常";
  return (
    <section
      className="panel"
      data-testid="exception-request-accepted"
      aria-labelledby="exception-accepted-title"
    >
      <h3 id="exception-accepted-title">业务例外请求（服务端权威）</h3>
      <dl className="facts">
        <div>
          <dt>请求编号</dt>
          <dd data-testid="exception-request-id">{accepted.requestId}</dd>
        </div>
        <div>
          <dt>当前路由</dt>
          <dd data-testid="exception-route">
            {unavailable || gate.data === undefined
              ? "unavailable"
              : gate.data.route}
          </dd>
        </div>
        <div>
          <dt>请求状态</dt>
          <dd data-testid="exception-status">{statusText}</dd>
        </div>
      </dl>
      {!unavailable && record !== undefined && record.decision !== null && (
        <p className="text-sm text-muted-foreground" data-testid="exception-decision">
          决策：{record.decision}
        </p>
      )}
      <p
        role="status"
        aria-live="polite"
        aria-label="业务例外恢复状态"
        data-testid="exception-recovery-status"
      >
        {recoveryStatus}
      </p>
      <div className="recovery-actions" data-testid="exception-request-actions">
        <Button
          variant="secondary"
          onClick={onReload}
          disabled={reloadDisabled}
          data-testid="exception-reload-button"
        >
          重新加载
        </Button>
      </div>
    </section>
  );
}

/** The visible outcome of an accepted evidence correction, resolved strictly
 * from server-owned current-route/history facts. */
function CorrectionProgressBanner({
  accepted,
  convergence,
  currentRunId,
  route,
}: {
  accepted: { evidenceRevision: number; applicationId: string };
  convergence: CorrectionConvergence;
  currentRunId: string | null;
  route: string | null;
}) {
  if (convergence === "converged") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-correction-converged"
      >
        更正已收敛（证据修订 {accepted.evidenceRevision}）：当前运行{" "}
        {currentRunId ?? "None"} · {route ?? "None"}
      </p>
    );
  }
  if (convergence === "timed_out") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-correction-timeout"
      >
        更正收敛超时（证据修订 {accepted.evidenceRevision}）：自动刷新已停止，请重新加载查看权威状态
      </p>
    );
  }
  if (convergence === "terminal") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-correction-terminal"
      >
        更正收敛终止（证据修订 {accepted.evidenceRevision}）：权威读取失败，请重新加载查看最新状态
      </p>
    );
  }
  return (
    <p
      role="status"
      aria-live="polite"
      data-testid="review-correction-pending"
    >
      更正已接受（证据修订 {accepted.evidenceRevision}）：等待服务端后续运行收敛…
    </p>
  );
}

/** The visible supplement request status, resolved strictly from the
 * server-owned request view and the shared evidence-revision convergence
 * read.  The browser never marks fulfillment and never chooses a route. */
function SupplementProgressBanner({
  requestView,
  accepted,
  convergence,
  currentRunId,
  route,
}: {
  requestView: UseQueryResult<SupplementRequestView>;
  accepted: { requestId: string };
  convergence: CorrectionConvergence;
  currentRunId: string | null;
  route: string | null;
}) {
  const status = requestView.data?.status;
  if (status === "fulfilled" && convergence === "converged" && requestView.data) {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-supplement-converged"
      >
        补充材料已满足并收敛（证据修订 {requestView.data.evidence_revision}）：
        当前运行 {currentRunId ?? "None"} · {route ?? "None"}
      </p>
    );
  }
  if (convergence === "timed_out") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-supplement-timeout"
      >
        补充材料收敛超时：自动刷新已停止，请重新加载查看权威状态
      </p>
    );
  }
  if (convergence === "terminal") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-supplement-terminal"
      >
        补充材料收敛终止：权威读取失败，请重新加载查看最新状态
      </p>
    );
  }
  if (status === "fulfilled") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-supplement-pending"
      >
        请求已满足：等待服务端后续运行收敛…
      </p>
    );
  }
  if (status === "expired") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-supplement-terminal"
      >
        补充材料请求已过期（deadline）
      </p>
    );
  }
  if (status === "invalidated") {
    return (
      <p
        role="status"
        aria-live="polite"
        data-testid="review-supplement-terminal"
      >
        补充材料请求已作废
      </p>
    );
  }
  return (
    <p
      role="status"
      aria-live="polite"
      data-testid="review-supplement-pending"
    >
      补充材料请求已接受（{accepted.requestId}）：等待材料提供方提交
    </p>
  );
}

/** The Reviewer's authoritative supplement request facts; server data
 * rendered verbatim with fixed copy and stable statuses. */
function SupplementRequestSection({
  requestView,
}: {
  requestView: UseQueryResult<SupplementRequestView>;
}) {
  if (requestView.isPending) {
    return <p data-testid="review-supplement-loading">请求加载中…</p>;
  }
  if (requestView.isError || requestView.data === undefined) {
    const notFound =
      requestView.error instanceof HttpError &&
      requestView.error.status === 404;
    return (
      <p data-testid="review-supplement-error">
        {notFound ? "请求未找到或无权访问" : "请求不可用"}
      </p>
    );
  }
  const request = requestView.data;
  return (
    <section
      className="panel"
      data-testid="review-supplement-request"
      aria-labelledby="review-supplement-title"
    >
      <h3 id="review-supplement-title">补充材料请求（服务端权威）</h3>
      <dl className="facts">
        <div>
          <dt>请求编号</dt>
          <dd data-testid="review-supplement-request-id">{request.request_id}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd data-testid="review-supplement-status">{request.status}</dd>
        </div>
        <div>
          <dt>当前</dt>
          <dd data-testid="review-supplement-current">
            {request.current === true ? "是" : "否"}
          </dd>
        </div>
        <div>
          <dt>到期（epoch）</dt>
          <dd data-testid="review-supplement-due">{request.due_at}</dd>
        </div>
        <div>
          <dt>材料要求</dt>
          <dd data-testid="review-supplement-material">
            {request.material_requirement.material_requirement_id}
            {" · "}
            {request.material_requirement.document_role}
            {" · "}
            {request.material_requirement.operation}
          </dd>
        </div>
        <div>
          <dt>责任方</dt>
          <dd data-testid="review-supplement-responsible">
            {request.material_requirement.responsible_party}
          </dd>
        </div>
      </dl>
      {request.failure !== undefined && request.failure !== null && (
        <p className="text-sm text-muted-foreground" data-testid="review-supplement-failure">
          {request.failure.reason_code} · {request.failure.recovery_action}
        </p>
      )}
    </section>
  );
}

/** The accepted-supplement presentation rendered once by both review-shell
 * branches: the server-derived progress banner, the authoritative request
 * view, and the supplement-specific authoritative reload control.  The
 * authority-specific reload callbacks stay separate. */
function SupplementAcceptedBlock({
  requestView,
  accepted,
  convergence,
  currentRunId,
  route,
  onReload,
  reloadDisabled,
}: {
  requestView: UseQueryResult<SupplementRequestView>;
  accepted: { requestId: string };
  convergence: CorrectionConvergence;
  currentRunId: string | null;
  route: string | null;
  onReload: () => void;
  reloadDisabled: boolean;
}) {
  return (
    <>
      <SupplementProgressBanner
        requestView={requestView}
        accepted={accepted}
        convergence={convergence}
        currentRunId={currentRunId}
        route={route}
      />
      <SupplementRequestSection requestView={requestView} />
      <div className="recovery-actions" data-testid="review-supplement-actions">
        <Button
          variant="secondary"
          onClick={onReload}
          disabled={reloadDisabled}
          data-testid="supplement-reload-button"
        >
          重新加载
        </Button>
      </div>
    </>
  );
}

export default function ReviewWorkPanel({ workId }: { workId: string }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const work = useManualWork(workId);
  // After an accepted correction the old work item is invalidated and its
  // query existence-hides; the last known application id keeps the
  // authoritative current-route/history reads alive during convergence.
  const [acceptedCorrection, setAcceptedCorrection] = useState<{
    evidenceRevision: number;
    applicationId: string;
  } | null>(null);
  // After an accepted supplement request the old work item is invalidated;
  // the request id latch keeps the shell alive across its existence-hiding
  // 404 and drives the authoritative request view read.
  const [acceptedSupplement, setAcceptedSupplement] = useState<{
    applicationId: string;
    requestId: string;
  } | null>(null);
  // After an accepted business-exception request the phase leaves Manual
  // Review and the workspace existence-hides; the request id latch keeps the
  // shell alive across the authoritative route/history refetch.
  const [acceptedException, setAcceptedException] = useState<{
    applicationId: string;
    requestId: string;
  } | null>(null);
  const applicationId =
    work.data?.application_id ??
    acceptedCorrection?.applicationId ??
    acceptedSupplement?.applicationId ??
    acceptedException?.applicationId ??
    null;
  const workspace = useWorkspace(
    work.data !== undefined && work.data.status !== "completed"
      ? work.data.application_id
      : acceptedCorrection?.applicationId ?? null,
  );
  const history = useApplicationHistory(applicationId);
  // The route query is hoisted so action gating can require a current
  // authoritative route read; GateSection observes the same shared query.
  const gate = useCurrentRoute(applicationId);
  const convergence = useCorrectionConvergence(
    applicationId,
    acceptedCorrection?.evidenceRevision ?? null,
  );
  // The fulfilled supplement converges through the same shared evidence
  // predicate: the request view's server evidence revision after fulfillment
  // plus exactly one server-current successor run matching current-route.
  const requestView = useSupplementRequest(
    acceptedSupplement?.requestId ?? null,
  );
  const supplementEvidenceRevision =
    requestView.data?.status === "fulfilled"
      ? requestView.data.evidence_revision
      : null;
  const supplementConvergence = useEvidenceConvergence(
    applicationId,
    supplementEvidenceRevision,
  );

  const claim = useClaimWorkItem(workId);
  const renew = useRenewWorkItem(workId);
  const release = useReleaseWorkItem(workId);
  const submit = useSubmitVerification(workId);
  const reveal = useRevealFieldObservation(workId);
  const correct = useCorrectFieldObservation(workId);
  const membershipCorrect = useCorrectPageMembership(workId);
  const supplement = useRequestSupplement(workId);
  const exception = useRequestBusinessException(workId);

  const [renewKey, setRenewKey] = useState(newIdempotencyKey);
  const [releaseKey, setReleaseKey] = useState(newIdempotencyKey);
  const [submitKey, setSubmitKey] = useState(newIdempotencyKey);
  const [revealKey, setRevealKey] = useState(newIdempotencyKey);
  const [correctionKey, setCorrectionKey] = useState(newIdempotencyKey);
  const [supplementKey, setSupplementKey] = useState(newIdempotencyKey);
  const [pendingCommand, setPendingCommand] = useState<PendingCommand | null>(
    null,
  );
  const [requiresReload, setRequiresReload] = useState(false);
  const [conflictReason, setConflictReason] = useState<string | null>(null);
  const [lastAccepted, setLastAccepted] = useState<Action | null>(null);
  const [rejectedAction, setRejectedAction] = useState<Action | null>(null);
  const [outcome, setOutcome] = useState<Outcome>("confirmed");
  const [revealed, setRevealed] = useState<RevealState | null>(null);
  const [correctionTarget, setCorrectionTarget] =
    useState<CorrectionTarget | null>(null);
  const [correctionRaw, setCorrectionRaw] = useState("");
  const [correctionReason, setCorrectionReason] =
    useState<CorrectionReason>("SOURCE_VALUE_MISREAD");
  // The membership key is minted only when a membership draft actually opens
  // (handleStartMembership) and rotates on acceptance/rejection; the empty
  // initial value is never submitted because submit requires an open draft.
  const [membershipKey, setMembershipKey] = useState("");
  const [membershipDraft, setMembershipDraft] =
    useState<MembershipDraft | null>(null);
  // The exact restricted command (and the token it was issued under) retained
  // only while its transport outcome is genuinely unknown, for exact replay.
  const issuedRef = useRef<
    (IssuedCommand & { command: RevealCommand | CorrectionCommand }) | null
  >(null);
  const owningReadsCurrent =
    work.isSuccess &&
    workspace.isSuccess &&
    history.isSuccess &&
    gate.isSuccess;

  useEffect(() => {
    headingRef.current?.focus();
  }, [workId]);

  /** Scrub every restricted surface.  Only reveal/correct pending commands
   * belong to this lifetime; an unrelated unknown command keeps its exact
   * replay identity while restricted payloads are removed. */
  const invalidateRestricted = () => {
    issuedRef.current = null;
    setPendingCommand((current) =>
      current?.action === "reveal" || current?.action === "correct"
        ? null
        : current,
    );
    setRevealed(null);
    setCorrectionTarget(null);
    setCorrectionRaw("");
    // An open membership draft is server authority too: it must visibly close
    // whenever the issuing claim authorization ends (expiry, owning-read
    // loss, authoritative reload, or context change).
    setMembershipDraft(null);
    reveal.reset();
    correct.reset();
  };

  /** Claim expiry is an explicit reload-required boundary: close every
   * restricted surface (including an open membership draft), latch the
   * authoritative reload state with the local CLAIM_EXPIRED reason, and
   * clear any stale rejected-action label.  Only the public reload action
   * clears the latch. */
  const expireMembershipAuthority = () => {
    invalidateRestricted();
    setRequiresReload(true);
    setConflictReason("CLAIM_EXPIRED");
    setRejectedAction(null);
  };

  /** The serialized work + workspace authority a membership draft binds to.
   * Returns null unless the work item is claimed, its lease is live, and the
   * work and workspace owning reads are successful.  The plain serialized
   * tuple covers work identity/authority, workspace identity/authority, and
   * the membership projections this panel renders (selected-finding
   * membership, membership-bearing mandatory blockers, and the membership
   * ledger). */
  const membershipAuthorityKey = (): string | null => {
    if (work.data === undefined || work.data.status !== "claimed") return null;
    if (Date.now() / 1000 >= work.data.claim_expires_at) return null;
    if (workspace.data === undefined || !work.isSuccess || !workspace.isSuccess) {
      return null;
    }
    const w = work.data;
    const s = workspace.data;
    const membershipBlockers = (s.mandatory_blockers ?? [])
      .filter((blocker) => blocker.membership != null)
      .map((blocker) => blocker.membership);
    return JSON.stringify([
      w.application_id,
      w.work_item_id,
      w.command_context,
      w.evidence_revision,
      w.lifecycle_revision,
      w.claim_fence,
      w.claim_expires_at,
      s.application_id,
      s.work_item_id,
      s.claim_fence,
      s.claim_expires_at,
      s.evidence_revision,
      s.lifecycle_revision,
      s.selected_finding?.membership ?? null,
      membershipBlockers,
      s.membership_ledger ?? [],
    ]);
  };

  // Unmount boundary: drop every restricted payload with the panel and clear
  // the restricted mutations from the MutationCache.  The mutation objects
  // are captured through refs because their identity changes on state
  // updates; the effect itself must stay mounted-once.
  const revealRef = useRef(reveal);
  const correctRef = useRef(correct);
  revealRef.current = reveal;
  correctRef.current = correct;
  useEffect(() => {
    return () => {
      issuedRef.current = null;
      revealRef.current.reset();
      correctRef.current.reset();
    };
  }, []);

  /** The live authorization token for one observation: exists only while the
   * work item is claimed, the exact context is current, and the claim is
   * unexpired. */
  const liveToken = (observationId: string): IssuedContextToken | null => {
    if (work.data === undefined || work.data.status !== "claimed") return null;
    if (Date.now() / 1000 >= work.data.claim_expires_at) return null;
    return {
      applicationId: work.data.application_id,
      workItemId: workId,
      observationId,
      expectedContext: work.data.command_context,
      evidenceRevision: work.data.evidence_revision,
      lifecycleRevision: work.data.lifecycle_revision,
      claimFence: work.data.claim_fence,
      claimExpiresAt: work.data.claim_expires_at,
    };
  };
  const liveTokenKey = (observationId: string): string | null => {
    const token = liveToken(observationId);
    return token === null ? null : contextTokenKey(token);
  };

  // Render-time guard: restricted values may render only while the exact
  // token that authorized them is still live (same application/work/
  // observation/context/revision/fence/expiry).  A mismatch ends any unknown
  // replay (the pending restricted command is dropped with its raw) and
  // clears every restricted holder.
  useEffect(() => {
    if (!owningReadsCurrent) {
      invalidateRestricted();
      return;
    }
    const revealLive =
      revealed === null ||
      revealed.tokenKey === liveTokenKey(revealed.observationId);
    const draftLive =
      correctionTarget === null ||
      correctionTarget.tokenKey === liveTokenKey(correctionTarget.observationId);
    const issuedLive =
      issuedRef.current === null ||
      issuedRef.current.tokenKey ===
        liveTokenKey(issuedRef.current.observationId);
    // The open membership draft is bound to the exact work + workspace
    // authority it was issued under: any successful refetch that changes a
    // covered server fact (context, fence, lease issuance, or membership
    // workspace data) visibly closes the draft.
    const membershipLive =
      membershipDraft === null ||
      membershipDraft.authorityKey === membershipAuthorityKey();
    if (!revealLive || !draftLive || !issuedLive || !membershipLive) {
      invalidateRestricted();
    }
  }, [work.data, workspace.data, workId, owningReadsCurrent]);

  // One expiry clock: the restricted authorization dies at claim expiry
  // without navigation, and any response arriving after that is discarded
  // before storage by the issued-token check.
  useEffect(() => {
    if (work.data === undefined || work.data.status !== "claimed") return;
    const delayMs = work.data.claim_expires_at * 1000 - Date.now();
    if (delayMs <= 0) {
      expireMembershipAuthority();
      return;
    }
    const timer = setTimeout(() => expireMembershipAuthority(), delayMs);
    return () => clearTimeout(timer);
    // The timer is bound to the authoritative work item data; the scrub
    // closure is recreated each render and stays pure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [work.data]);

  const anyPending =
    claim.isPending ||
    renew.isPending ||
    release.isPending ||
    submit.isPending ||
    reveal.isPending ||
    correct.isPending ||
    membershipCorrect.isPending ||
    supplement.isPending ||
    exception.isPending;

  const mutations: Record<
    Action,
    { isPending: boolean; isError: boolean; isSuccess: boolean; error: unknown }
  > = {
    claim,
    renew,
    release,
    submit,
    reveal,
    correct,
    membership: membershipCorrect,
    supplement,
    exception,
  };
  const active = pendingCommand === null ? null : pendingCommand.action;
  const activeMutation = active === null ? undefined : mutations[active];
  const transportUnknown =
    active !== null &&
    activeMutation !== undefined &&
    activeMutation.isError &&
    !isDefinitiveRejection(activeMutation.error);

  const keyRotations: Record<Action, () => void> = {
    claim: () => {},
    renew: () => setRenewKey(newIdempotencyKey()),
    release: () => setReleaseKey(newIdempotencyKey()),
    submit: () => setSubmitKey(newIdempotencyKey()),
    reveal: () => setRevealKey(newIdempotencyKey()),
    correct: () => setCorrectionKey(newIdempotencyKey()),
    membership: () => setMembershipKey(newIdempotencyKey()),
    supplement: () => setSupplementKey(newIdempotencyKey()),
    exception: () => {},
  };

  const accepted = (action: Action) => () => {
    setPendingCommand(null);
    setLastAccepted(action);
    setRejectedAction(null);
    setRequiresReload(false);
    setConflictReason(null);
    // A command key is bound to one logical command (its fingerprint covers
    // the fence); the next logical command needs a fresh key, otherwise a
    // later fence change would collide with the retained fingerprint.
    keyRotations[action]();
  };

  /** A definitive rejection proves the semantic key was never accepted, so
   * the key rotates once at that proof and the rejected action stays latched
   * until the owning work-item refetch succeeds (FIX-1).  It is also a
   * definitive-error boundary: every restricted surface is scrubbed.  The
   * exception request runs on the S05 command surface, so it classifies its
   * structured S05 rejections (including ``S05_STOPPED``/``S05_UNAVAILABLE``
   * 503s) instead of the S03 set. */
  const rejected =
    (
      action: Action,
      classifier: (error: unknown) => error is HttpError = isDefinitiveRejection,
    ) =>
    (error: Error) => {
      if (!classifier(error)) return;
      keyRotations[action]();
      setPendingCommand(null);
      setRejectedAction(action);
      setRequiresReload(true);
      setConflictReason(error.reasonCode ?? "conflict");
      invalidateRestricted();
    };

  const handleClaim = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    invalidateRestricted();
    const command: ClaimCommand = {
      expected_context: work.data.command_context,
    };
    setPendingCommand({ action: "claim", command });
    setLastAccepted(null);
    setRejectedAction(null);
    claim.mutate(command, {
      onSuccess: accepted("claim"),
      onError: rejected("claim"),
    });
  };

  const handleRenew = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    invalidateRestricted();
    const command: FencedCommand = {
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: renewKey,
    };
    setPendingCommand({ action: "renew", command });
    setLastAccepted(null);
    setRejectedAction(null);
    renew.mutate(command, {
      onSuccess: accepted("renew"),
      onError: rejected("renew"),
    });
  };

  const handleRelease = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    invalidateRestricted();
    const command: FencedCommand = {
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: releaseKey,
    };
    setPendingCommand({ action: "release", command });
    setLastAccepted(null);
    setRejectedAction(null);
    release.mutate(command, {
      onSuccess: accepted("release"),
      onError: rejected("release"),
    });
  };

  const handleSubmit = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    invalidateRestricted();
    const command: SubmitCommand = {
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: submitKey,
      verification: buildVerification(work.data, outcome),
    };
    setPendingCommand({ action: "submit", command });
    setLastAccepted(null);
    setRejectedAction(null);
    submit.mutate(command, {
      onSuccess: accepted("submit"),
      onError: rejected("submit"),
    });
  };

  /** The eligible server authority signals for a supplement request: the
   * claimed work item carries the R_VIN_CROSS finding on the manual-review
   * route.  The server remains the eligibility authority; this only gates
   * the control on the same facts the command will present. */
  const supplementEligibleFinding = (): ReviewWorkResponse["automatic_findings"][number] | undefined => {
    if (work.data === undefined) return undefined;
    return work.data.automatic_findings.find(
      (finding) =>
        finding.rule_id === "R_VIN_CROSS" &&
        finding.verdict === "uncertain" &&
        finding.reason_code === "MISSING_DOCS",
    );
  };

  const handleSupplementAccepted = (result: SupplementRequestResult) => {
    setAcceptedSupplement({
      applicationId: result.application_id,
      requestId: result.request_id,
    });
    accepted("supplement")();
  };

  const handleSupplement = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    if (gate.data?.route !== "manual_review") return;
    const finding = supplementEligibleFinding();
    if (finding === undefined) return;
    invalidateRestricted();
    const command: SupplementRequestCommand = {
      finding_id: finding.finding_id,
      reason_code: "MISSING_REQUIRED_MATERIAL",
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: supplementKey,
      predecessor_request_id: null,
    };
    setPendingCommand({ action: "supplement", command });
    setLastAccepted(null);
    setRejectedAction(null);
    supplement.mutate(command, {
      onSuccess: handleSupplementAccepted,
      onError: rejected("supplement"),
    });
  };

  /** The server-owned request eligibility for the workspace's selected
   * finding; the DTO is the sole gate.  Absent DTO data means no request
   * surface at all (the browser never infers eligibility). */
  const exceptionEligibility =
    workspace.data?.business_exception_eligibility ?? null;
  const exceptionEligibleFinding =
    workspace.data?.selected_finding ?? null;

  const handleExceptionAccepted = (result: BusinessExceptionRequestResult) => {
    setAcceptedException({
      applicationId: result.application_id,
      requestId: result.request_id,
    });
    accepted("exception")();
  };

  const handleRequestException = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    if (gate.data?.route !== "manual_review") return;
    if (exceptionEligibility === null || exceptionEligibility.eligible !== true) {
      return;
    }
    if (exceptionEligibleFinding === null) return;
    if (
      exceptionEligibility.request_reason === null ||
      exceptionEligibility.request_reason === undefined
    ) {
      return;
    }
    invalidateRestricted();
    const command: ExceptionRequestCommand = {
      finding_id: exceptionEligibleFinding.finding_id,
      reason_code: exceptionEligibility.request_reason,
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: newIdempotencyKey(),
      predecessor_request_id: exceptionEligibility.predecessor_request_id,
    };
    setPendingCommand({ action: "exception", command });
    setLastAccepted(null);
    setRejectedAction(null);
    exception.mutate(command, {
      onSuccess: handleExceptionAccepted,
      onError: rejected("exception", isDefinitiveS05Rejection),
    });
  };

  /** Store a reveal response only when the exact issuance that issued it is
   * still the live issuance and its token is still current and unexpired;
   * discard it before storage otherwise (context change, expiry, access
   * loss, or a newer command). */
  const storeRevealIfIssued = (issuance: IssuedCommand, result: RevealResult) => {
    if (issuedRef.current !== issuance) return;
    const expectedKey = liveTokenKey(result.observation_id);
    if (expectedKey === null || issuance.tokenKey !== expectedKey) {
      invalidateRestricted();
      return;
    }
    issuedRef.current = null;
    accepted("reveal")();
    setRevealed({
      tokenKey: issuance.tokenKey,
      observationId: result.observation_id,
      sourceText: result.source_text,
    });
    reveal.reset();
  };

  const handleReveal = (link: S01EvidenceLink) => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    const token = liveToken(link.observation_id);
    if (token === null) return;
    const command: RevealCommand = {
      application_id: work.data.application_id,
      observation_id: link.observation_id,
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: revealKey,
    };
    // A new restricted command scrubs every previous restricted holder.
    invalidateRestricted();
    const issuance = {
      tokenKey: contextTokenKey(token),
      observationId: link.observation_id,
      command,
    };
    issuedRef.current = issuance;
    setPendingCommand({ action: "reveal", command });
    setLastAccepted(null);
    setRejectedAction(null);
    reveal.mutate(command, {
      onSuccess: (result: RevealResult) =>
        storeRevealIfIssued(issuance, result),
      onError: rejected("reveal"),
    });
  };

  const handleStartCorrection = (
    link: S01EvidenceLink,
    findingId: string,
  ) => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    if (
      typeof link.source_sha256 !== "string" ||
      typeof link.source_page !== "number" ||
      typeof link.source_region !== "string"
    ) {
      return;
    }
    const token = liveToken(link.observation_id);
    if (token === null) return;
    // A new restricted draft scrubs every previous restricted holder.
    invalidateRestricted();
    setCorrectionTarget({
      tokenKey: contextTokenKey(token),
      findingId,
      observationId: link.observation_id,
      documentId: link.document_id,
      documentRole: link.document_role,
      field: link.field,
      sourceSha256: link.source_sha256,
      sourcePage: link.source_page,
      sourceRegion: link.source_region,
    });
    setCorrectionRaw("");
    setCorrectionReason("SOURCE_VALUE_MISREAD");
  };

  const handleCancelCorrection = () => {
    if (correct.isPending) return;
    setCorrectionTarget(null);
    setCorrectionRaw("");
  };

  /** Store a correction acceptance only when the exact issuance that issued
   * it is still the live issuance and its token is still current and
   * unexpired; discard it before storage otherwise.  The response's
   * ``observation_id`` is the successor observation the rerun created, so
   * the authorization check is keyed to the superseded observation the
   * command was issued against. */
  const storeCorrectionIfIssued = (
    issuance: IssuedCommand,
    result: CorrectionResult,
  ) => {
    if (issuedRef.current !== issuance) return;
    const expectedKey = liveTokenKey(issuance.observationId);
    if (expectedKey === null || issuance.tokenKey !== expectedKey) {
      invalidateRestricted();
      return;
    }
    issuedRef.current = null;
    accepted("correct")();
    setAcceptedCorrection({
      evidenceRevision: result.evidence_revision,
      applicationId: result.application_id,
    });
    correct.reset();
  };

  const handleCorrectionSubmit = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    if (correctionTarget === null || correctionRaw.length === 0) return;
    const expectedKey = liveTokenKey(correctionTarget.observationId);
    if (expectedKey === null || expectedKey !== correctionTarget.tokenKey) {
      // The draft outlived its authorization: scrub without submitting.
      invalidateRestricted();
      return;
    }
    // The raw evidence crosses the boundary byte-for-byte as entered; only
    // length zero counts as missing.
    const command: CorrectionCommand = {
      application_id: work.data.application_id,
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: correctionKey,
      correction: {
        schema_version: "field-observation-correction/1",
        finding_id: correctionTarget.findingId,
        observation_id: correctionTarget.observationId,
        document_id: correctionTarget.documentId,
        document_role: correctionTarget.documentRole,
        field: correctionTarget.field,
        raw: correctionRaw,
        source_location: {
          source_sha256: correctionTarget.sourceSha256,
          source_page: correctionTarget.sourcePage,
          source_region: correctionTarget.sourceRegion,
        },
        reason_code: correctionReason,
      },
    };
    const issuance = {
      tokenKey: correctionTarget.tokenKey,
      observationId: correctionTarget.observationId,
      command,
    };
    // Submission is a restricted boundary: clear the reveal, draft, and old
    // mutation state before installing the one command allowed for replay.
    invalidateRestricted();
    issuedRef.current = issuance;
    setPendingCommand({ action: "correct", command });
    setLastAccepted(null);
    setRejectedAction(null);
    correct.mutate(command, {
      onSuccess: (result: CorrectionResult) =>
        storeCorrectionIfIssued(issuance, result),
      onError: rejected("correct"),
    });
  };

  /** S10 page-membership decisions: the Reviewer explicitly accepts one
   * coexisting candidate document instance and role, or explicitly unassigns
   * the page, with a registered reason.  The draft is the server-owned finding
   * data only; eligibility comes exclusively from explicit accepted facts. */
  const handleStartMembership = (target: MembershipTarget) => {
    const authorityKey = membershipAuthorityKey();
    if (authorityKey === null) return;
    setMembershipDraft({
      target,
      decision: "accept",
      // The browser never selects a candidate by array order, confidence,
      // or any heuristic: the Reviewer must explicitly choose one claim.
      claimId: null,
      reason: "MEMBERSHIP_SOURCE_VERIFIED",
      authorityKey,
    });
    setMembershipKey(newIdempotencyKey());
    setLastAccepted(null);
    setRejectedAction(null);
  };

  const handleMembershipDecision = (
    decision: "accept" | "unassign",
  ) => {
    setMembershipDraft((draft) =>
      draft === null
        ? draft
        : { ...draft, decision, reason: "MEMBERSHIP_SOURCE_VERIFIED" },
    );
  };

  const handleMembershipCandidate = (claimId: string) => {
    setMembershipDraft((draft) =>
      draft === null ? draft : { ...draft, claimId },
    );
  };

  const handleMembershipReasonChange = (reason: MembershipReason) => {
    setMembershipDraft((draft) =>
      draft === null ? draft : { ...draft, reason },
    );
  };

  const handleMembershipSubmit = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    if (membershipDraft === null) return;
    // Event-time authority check: the draft is only valid while the rendered
    // work item is claimed and unexpired.  Expired authority enters the
    // explicit reload-required state; a missing or changed authority key
    // closes the draft.  Either way no POST leaves the browser.
    if (
      work.data.status !== "claimed" ||
      Date.now() / 1000 >= work.data.claim_expires_at
    ) {
      expireMembershipAuthority();
      return;
    }
    const liveKey = membershipAuthorityKey();
    if (liveKey === null || liveKey !== membershipDraft.authorityKey) {
      setMembershipDraft(null);
      return;
    }
    const member = membershipDraft.target.membership;
    const candidate = member.candidates.find(
      (item) => item.claim_id === membershipDraft.claimId,
    );
    const eventId = member.source_evidence.event_id;
    const sourceEvidenceRevision = member.source_evidence.evidence_revision;
    if (
      candidate === undefined ||
      typeof eventId !== "string" ||
      typeof sourceEvidenceRevision !== "number"
    ) return;
    const sourceEvidence = {
      event_id: eventId,
      evidence_revision: sourceEvidenceRevision,
    };
    const common = {
      schema_version: "page-membership-correction/2" as const,
      finding_id: membershipDraft.target.findingId,
      candidate_claim_id: candidate.claim_id,
      attachment_id: member.attachment_id,
      page_source_sha256: member.page_source_sha256,
      page_ordinal: member.page_ordinal,
      source_evidence: sourceEvidence,
      expected_active_decision_ids: member.active_decision_ids ?? [],
    };
    const membership: MembershipPayload =
      membershipDraft.decision === "accept"
        ? {
            ...common,
            decision: "accept",
            document_instance_id: candidate.document_instance_id,
            document_role: candidate.document_role,
            reason_code: membershipDraft.reason as MembershipAccept["reason_code"],
          }
        : {
            ...common,
            decision: "unassign",
            reason_code: membershipDraft.reason as MembershipUnassign["reason_code"],
          };
    const command: MembershipCommand = {
      application_id: work.data.application_id,
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: membershipKey,
      membership,
    };
    setPendingCommand({ action: "membership", command });
    setLastAccepted(null);
    setRejectedAction(null);
    membershipCorrect.mutate(command, {
      onSuccess: (result: MembershipResult) => {
        // Like the field correction, the accepted membership successor
        // invalidates the work item and advances the Evidence revision; the
        // latch keeps the authoritative route/history reads alive while the
        // replacement run converges.
        setAcceptedCorrection({
          applicationId: result.application_id,
          evidenceRevision: result.evidence_revision,
        });
        accepted("membership")();
      },
      onError: rejected("membership"),
    });
  };

  /** Reconciling retry: a claim has no idempotency key, so its unknown
   * outcome is resolved by an authoritative refetch that identifies the live
   * lease instead of blindly issuing a second business effect. */
  const reconcileClaim = async () => {
    try {
      await queryClient.refetchQueries(
        { queryKey: MANUAL_WORK_KEY(workId) },
        { throwOnError: true },
      );
    } catch {
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
    const reconciled = queryClient.getQueryData<ReviewWorkResponse>(
      MANUAL_WORK_KEY(workId),
    );
    setPendingCommand(null);
    setRejectedAction(null);
    setRequiresReload(false);
    setConflictReason(null);
    if (reconciled?.status === "claimed") {
      setLastAccepted("claim");
    }
  };

  const handleRetry = () => {
    if (pendingCommand === null || anyPending) return;
    if (pendingCommand.action === "claim") {
      void reconcileClaim();
      return;
    }
    if (pendingCommand.action === "renew") {
      renew.mutate(pendingCommand.command, {
        onSuccess: accepted("renew"),
        onError: rejected("renew"),
      });
    } else if (pendingCommand.action === "release") {
      release.mutate(pendingCommand.command, {
        onSuccess: accepted("release"),
        onError: rejected("release"),
      });
    } else if (pendingCommand.action === "reveal") {
      // Unknown outcome: replay the exact issued command and key captured at
      // issuance, but only while that exact issuance is still live; a
      // context mismatch already ended the replay.
      const issuance = issuedRef.current;
      if (issuance === null) return;
      reveal.mutate(pendingCommand.command, {
        onSuccess: (result: RevealResult) =>
          storeRevealIfIssued(issuance, result),
        onError: rejected("reveal"),
      });
    } else if (pendingCommand.action === "correct") {
      const issuance = issuedRef.current;
      if (issuance === null) return;
      correct.mutate(pendingCommand.command, {
        onSuccess: (result: CorrectionResult) =>
          storeCorrectionIfIssued(issuance, result),
        onError: rejected("correct"),
      });
    } else if (pendingCommand.action === "supplement") {
      supplement.mutate(pendingCommand.command, {
        onSuccess: handleSupplementAccepted,
        onError: rejected("supplement"),
      });
    } else if (pendingCommand.action === "exception") {
      exception.mutate(pendingCommand.command, {
        onSuccess: handleExceptionAccepted,
        onError: rejected("exception", isDefinitiveS05Rejection),
      });
    } else if (pendingCommand.action === "membership") {
      membershipCorrect.mutate(pendingCommand.command, {
        onSuccess: (result: MembershipResult) => {
          setAcceptedCorrection({
            applicationId: result.application_id,
            evidenceRevision: result.evidence_revision,
          });
          accepted("membership")();
        },
        onError: rejected("membership"),
      });
    } else {
      submit.mutate(pendingCommand.command, {
        onSuccess: accepted("submit"),
        onError: rejected("submit"),
      });
    }
  };

  const handleReload = async () => {
    // A command outcome that is still pending must never be reset or given a
    // fresh semantic key, and an accepted outcome must never be cleared by a
    // reload.
    if (anyPending) return;
    // Authoritative reload boundary: scrub every restricted surface up front
    // (issued token, saved reveal, correction draft, restricted mutations) so
    // nothing can outlive the interaction even while the refetch is in
    // flight; the next explicit reveal re-issues from the live context.
    invalidateRestricted();
    // Authoritative reload.  A failed refetch must never clear the conflict
    // fence nor rotate the semantic idempotency keys, so the refetch throws
    // and the fence is kept on failure.  The rejection latch clears only
    // after the owning work-item refetch succeeds.
    try {
      await queryClient.refetchQueries(
        { queryKey: MANUAL_WORK_KEY(workId) },
        { throwOnError: true },
      );
    } catch {
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
    setRejectedAction(null);
    setRequiresReload(false);
    setConflictReason(null);
  };

  /** Authoritative refetch while an accepted supplement request keeps the
   * shell alive: the request view is refetched first, then the server-owned
   * S01 reads (current-route, history) reconverge; the old invalidated work
   * item existence-hides and is never required again. */
  const handleSupplementReload = async () => {
    if (anyPending || acceptedSupplement === null) return;
    try {
      await queryClient.refetchQueries(
        { queryKey: SUPPLEMENT_REQUEST_KEY(acceptedSupplement.requestId) },
        { throwOnError: true },
      );
    } catch {
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
  };

  /** Authoritative refetch while an accepted exception request keeps the
   * shell alive: current-route and history are refetched first (the old
   * workspace existence-hides), then the server-owned S01 reads reconverge.
   * A failed refetch is surfaced as unavailable/stale and cached route and
   * history facts are never labeled current; the state clears only when
   * both owning queries refetch successfully. */
  const [exceptionReloadFailed, setExceptionReloadFailed] = useState(false);
  const handleExceptionReload = async () => {
    if (anyPending || acceptedException === null) return;
    try {
      await queryClient.refetchQueries(
        { queryKey: ROUTE_KEY(acceptedException.applicationId) },
        { throwOnError: true },
      );
      await queryClient.refetchQueries(
        { queryKey: HISTORY_KEY(acceptedException.applicationId) },
        { throwOnError: true },
      );
    } catch {
      setExceptionReloadFailed(true);
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
    setExceptionReloadFailed(false);
  };

  let statusText = "等待操作";
  if (anyPending && active !== null) {
    statusText = `${ACTION_LABELS[active]}提交中…`;
  } else if (
    requiresReload &&
    conflictReason !== null &&
    rejectedAction !== null
  ) {
    statusText = `${ACTION_LABELS[rejectedAction]}未接受（${conflictReason}）：请重新加载权威上下文后再试`;
  } else if (requiresReload && conflictReason === "CLAIM_EXPIRED") {
    statusText = "认领已过期：请重新加载权威上下文后再继续";
  } else if (transportUnknown) {
    statusText = "结果未知：网络未确认，重试将使用同一幂等键";
  } else if (lastAccepted !== null) {
    statusText = `${ACTION_LABELS[lastAccepted]}已接受`;
  } else if (acceptedCorrection !== null) {
    statusText =
      convergence === "converged"
        ? "更正已收敛"
        : convergence === "timed_out"
          ? "收敛超时：请重新加载查看权威状态"
          : convergence === "terminal"
            ? "收敛终止：权威读取失败"
            : "等待服务端后续运行";
  }

  const correctionAccepted = acceptedCorrection !== null;
  const supplementAccepted = acceptedSupplement !== null;
  const exceptionAccepted = acceptedException !== null;
  // Both accepted evidence flows invalidate the old work item; the shell,
  // authoritative reads and history stay usable in either state.  An accepted
  // exception request likewise leaves Manual Review, so the shell keeps the
  // route/history alive instead of degrading into the workspace 404.
  const acceptedEvidenceFlow =
    correctionAccepted || supplementAccepted || exceptionAccepted;
  const workspaceRequired =
    work.data !== undefined && work.data.status !== "completed";
  // After an accepted correction the invalidated old workspace existence-hides
  // (404) while the successor converges; that dependent read must not take
  // down the review shell, current-route, or history.
  const dependentError =
    (workspaceRequired && workspace.isError && !acceptedEvidenceFlow) ||
    history.isError ||
    gate.isError;

  if (work.isPending || work.isError || work.data === undefined || dependentError) {
    if (work.isPending) {
      return (
        <section
          className="panel"
          data-testid="review-panel"
          aria-labelledby="review-title"
        >
          <h2 id="review-title" tabIndex={-1} ref={headingRef}>
            人工核验
          </h2>
          <p data-testid="review-loading">工作项加载中…</p>
        </section>
      );
    }
    if (acceptedEvidenceFlow) {
      // The invalidated work item: keep the review shell, the accepted-flow
      // progress, the authoritative current route, and history usable while
      // the successor converges.
      return (
        <section
          className="panel"
          data-testid="review-panel"
          aria-labelledby="review-title"
        >
          <h2 id="review-title" tabIndex={-1} ref={headingRef}>
            人工核验
          </h2>
          {supplementAccepted && (
            <SupplementAcceptedBlock
              requestView={requestView}
              accepted={acceptedSupplement}
              convergence={supplementConvergence}
              currentRunId={gate.data?.current_run_id ?? null}
              route={gate.data?.route ?? null}
              onReload={handleSupplementReload}
              reloadDisabled={anyPending}
            />
          )}
          {exceptionAccepted && (
            <ExceptionAcceptedBlock
              accepted={acceptedException}
              history={history}
              gate={gate}
              onReload={handleExceptionReload}
              reloadDisabled={anyPending}
              reloadFailed={exceptionReloadFailed}
            />
          )}
          {correctionAccepted && (
            <CorrectionProgressBanner
              accepted={acceptedCorrection}
              convergence={convergence}
              currentRunId={gate.data?.current_run_id ?? null}
              route={gate.data?.route ?? null}
            />
          )}
          <GateSection applicationId={applicationId ?? ""} />
          <HistorySection history={history} />
        </section>
      );
    }
    const notFound = [
      work.error,
      workspaceRequired ? workspace.error : null,
      history.error,
      gate.error,
    ].some((error) => error instanceof HttpError && error.status === 404);
    return (
      <section
        className="panel"
        data-testid="review-panel"
        aria-labelledby="review-title"
      >
        <h2 id="review-title" tabIndex={-1} ref={headingRef}>
          人工核验
        </h2>
        <p data-testid="review-error">
          {notFound
            ? "未找到或无权访问"
            : work.isError
              ? "工作项不可用"
              : "相关权威不可用"}
        </p>
      </section>
    );
  }

  const data = work.data;
  const claimed = data.status === "claimed";
  // The claim expiry is part of the issued authorization: an expired claim
  // disables every restricted control until an authoritative renewal.
  const claimExpired =
    claimed && Date.now() / 1000 >= data.claim_expires_at;
  const commandBlocked =
    pendingCommand !== null || transportUnknown || requiresReload;
  const canClaim =
    data.status !== "claimed" &&
    data.status !== "completed" &&
    !acceptedEvidenceFlow &&
    !commandBlocked &&
    !work.isError &&
    owningReadsCurrent;
  const canFenced =
    claimed && !acceptedEvidenceFlow && !commandBlocked && !work.isError &&
    owningReadsCurrent;
  const canSubmit =
    claimed &&
    data.automatic_findings.length > 0 &&
    !acceptedEvidenceFlow &&
    !commandBlocked &&
    !work.isError &&
    owningReadsCurrent;
  const canSupplement =
    claimed &&
    !claimExpired &&
    !acceptedEvidenceFlow &&
    !commandBlocked &&
    !work.isError &&
    owningReadsCurrent &&
    gate.data?.route === "manual_review" &&
    supplementEligibleFinding() !== undefined;
  // The request gate is the server-owned eligibility DTO: the button exists
  // only when the workspace carries the projection, and acts only when the
  // server says the exact current finding is eligible.
  const exceptionEligible =
    exceptionEligibility !== null &&
    exceptionEligibility.eligible === true &&
    exceptionEligibility.request_reason !== null &&
    exceptionEligibleFinding !== null;
  const canRequestException =
    claimed &&
    !claimExpired &&
    !acceptedEvidenceFlow &&
    !commandBlocked &&
    !work.isError &&
    owningReadsCurrent &&
    gate.data?.route === "manual_review" &&
    exceptionEligible;

  return (
    <section
      className="panel"
      data-testid="review-panel"
      aria-labelledby="review-title"
    >
      <h2 id="review-title" tabIndex={-1} ref={headingRef}>
        人工核验
      </h2>
      {correctionAccepted && (
        <CorrectionProgressBanner
          accepted={acceptedCorrection}
          convergence={convergence}
          currentRunId={gate.data?.current_run_id ?? null}
          route={gate.data?.route ?? null}
        />
      )}
      {supplementAccepted && (
        <SupplementAcceptedBlock
          requestView={requestView}
          accepted={acceptedSupplement}
          convergence={supplementConvergence}
          currentRunId={gate.data?.current_run_id ?? null}
          route={gate.data?.route ?? null}
          onReload={handleSupplementReload}
          reloadDisabled={anyPending}
        />
      )}
      {exceptionAccepted && (
        <ExceptionAcceptedBlock
          accepted={acceptedException}
          history={history}
          gate={gate}
          onReload={handleExceptionReload}
          reloadDisabled={anyPending}
          reloadFailed={exceptionReloadFailed}
        />
      )}
      <WorkFacts work={data} />
      <WorkspaceSection
        work={data}
        workspace={workspace}
        claimed={claimed && !acceptedEvidenceFlow}
        claimExpired={claimExpired}
        liveTokenKey={liveTokenKey}
        revealed={revealed}
        correctionTarget={correctionTarget}
        correctionRaw={correctionRaw}
        correctionReason={correctionReason}
        correctionReasons={CORRECTION_REASONS}
        revealPending={active === "reveal"}
        correctPending={active === "correct"}
        controlsDisabled={
          !claimed ||
          claimExpired ||
          acceptedEvidenceFlow ||
          commandBlocked ||
          !owningReadsCurrent
        }
        onReveal={handleReveal}
        onStartCorrection={handleStartCorrection}
        onCorrectionRawChange={setCorrectionRaw}
        onCorrectionReasonChange={setCorrectionReason}
        onSubmitCorrection={handleCorrectionSubmit}
        onCancelCorrection={handleCancelCorrection}
        membershipDraft={membershipDraft}
        membershipPending={membershipCorrect.isPending}
        onStartMembership={handleStartMembership}
        onMembershipDecision={handleMembershipDecision}
        onMembershipCandidate={handleMembershipCandidate}
        onMembershipReasonChange={handleMembershipReasonChange}
        onSubmitMembership={handleMembershipSubmit}
      />
      <GateSection applicationId={data.application_id} />
      <HistorySection history={history} />
      {!acceptedEvidenceFlow && (
        <div className="recovery-actions" data-testid="review-actions">
          <Button
            variant="secondary"
            onClick={handleReload}
            disabled={anyPending}
            data-testid="reload-button"
          >
            重新加载
          </Button>
          <Button
            onClick={handleClaim}
            disabled={!canClaim || anyPending}
            data-testid="claim-button"
          >
            认领
          </Button>
          <Button
            onClick={handleRenew}
            disabled={!canFenced || anyPending}
            data-testid="renew-button"
          >
            续期
          </Button>
          <Button
            onClick={handleRelease}
            disabled={!canFenced || anyPending}
            data-testid="release-button"
          >
            释放
          </Button>
          {claimed && supplementEligibleFinding() !== undefined && (
            <Button
              onClick={handleSupplement}
              disabled={!canSupplement || anyPending}
              data-testid="supplement-button"
            >
              请求补充材料
            </Button>
          )}
          {exceptionEligibility !== null && !acceptedEvidenceFlow && (
            <>
              <Button
                onClick={handleRequestException}
                disabled={!canRequestException || anyPending}
                data-testid="exception-request-button"
              >
                请求业务例外
              </Button>
              {exceptionEligibility.eligible !== true && (
                <p
                  className="text-sm text-muted-foreground"
                  data-testid="exception-ineligible"
                >
                  当前发现不可申请业务例外（{exceptionEligibility.ineligible_reason_code ?? "unknown"}）
                </p>
              )}
            </>
          )}
          {claimed && (
            <label className="text-sm">
              核验结论
              <select
                data-testid="review-outcome"
                value={outcome}
                onChange={(event) => setOutcome(event.target.value as Outcome)}
                disabled={!canSubmit || anyPending}
              >
                {OUTCOMES.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          )}
          <Button
            onClick={handleSubmit}
            disabled={!canSubmit || anyPending}
            data-testid="submit-button"
          >
            提交人工核验
          </Button>
          {transportUnknown && (
            <Button variant="outline" onClick={handleRetry} data-testid="retry-button">
              重试
            </Button>
          )}
        </div>
      )}
      <p role="status" aria-live="polite" data-testid="review-command-status">
        {statusText}
      </p>
      {requiresReload && (
        <p className="text-sm text-muted-foreground" data-testid="review-reload-note">
          请重新加载权威上下文后再试
        </p>
      )}
    </section>
  );
}
