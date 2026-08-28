# ROUND32 R4 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex`（R4 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R4.md` + `docs/ROUND32_FIX_BRIEF_R4.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R4 review 一致）。
- R4 起始 HEAD（R3 交付）：`892a514`。
- R4 最终 HEAD：见文末 commit 记录（本文件提交后 `docs(s16): record R4 delivery HEAD …`）。

## R4 finding 修复状态（逐项，对应 brief 10 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | preflight binding 持久化 upstream reference | P1-1 | 完成 | `s16.py`：持久 binding 改存 `_preflight_response(..., application_reference=None)` 的**无原值快照**（scope fingerprint、manifest/entries digest、owner revisions、policy、hold facts、request id；不包含 `application_reference` 键）；授权内存响应仍回显 reference；同键 replay 从快照重建无原值结果并保留删除后 existence-hiding。回归：`test_preflight_binding_never_persists_application_reference`（直读 `s16_bindings.result`，断言 reference/application id/path/raw/credential/caller key 均不在序列化内容中，同键 replay 返回相同 manifest_digest 且内存响应含 reference） |
| 2 | release audit 绑定 impose request id | P1-2 | 完成 | `s16.py` release 的 `_write_security_audit(request_id=release_request_id)`；release event、audit fact、binding result 与 HTTP response 全部使用同一 release request id，与 impose request id 独立。回归：`test_release_audit_fact_reconciles_with_release_request`（audit fact 的 `request_id_fingerprint` == digest(release request id) != digest(impose request id)；WORM 复制包保持无 request 指纹） |
| 3 | backup verify_absent 丢弃 operation/fence | P1-3 | 完成 | `s16.py`：`BackupDeletionOwner.verify_absent` 在收到 operation/fence 时读取 `backup_deletion_bindings` 校验 scope + fingerprints digest：匹配 → verified；digest/scope 不匹配 → 稳定 `S16_OWNER_BINDING_CONFLICT`；低 fence → 稳定 `S16_OWNER_STALE_FENCE`；staged intent → 可重试 VERIFY_FAILED；无 binding 的 operation → 不证明 absent。already-absent 删除路径也写 binding，使 worker/replay verify 恒有 binding 可对账；scope-only 调用保留为 readiness probe（先跑共享对账再扫描 manifest）。回归：`test_backup_verify_binds_operation_fence_and_digest`（同 binding 双次 verified、wrong digest conflict、stale fence、无 binding operation、already-absent binding） |
| 4 | backup health 不核对双向完整性 | P1-4 | 完成 | `s16.py`：新增 `_backup_reconciliation()` — manifest 与 registry 双向存在、handle 在 root 内、文件存在性与 content digest、entries_digest 一致性、无 orphan registry 行；`inventory()`、`owner_healthy()`、preflight（经 inventory）与 readiness probe（经 verify scope 分支）共享该结果，任何损坏 → 稳定 `S16Unavailable` + 门禁关闭。回归：`test_backup_reconciliation_fails_closed_and_repairs_forward`（parametrize：manifest 删/registry 留、registry 删/manifest 留、摘要篡改、文件缺失 → owner_healthy False + inventory/preflight S16Unavailable，修复后 healthy True） |
| 5 | complete effect 回填 request cache 并展示 query 状态 | P1-5 | 完成 | `S16GovernedDeletionPanel` 重构为三表面：`ActiveSurface`（持有 `useS16Query`/preflight/审批/commit/job）在检测到 complete 时 `clearApplicationScopedCache()`（仅移除、绝不 fetchQuery 回填）后回调 `onComplete` 并卸载；`CompletedSurface` 只挂载独立 value-free receipt key，job 状态为固定 receipt-safe 摘要（不读任何 application-scoped query）；身份 403 走统一 `handleIdentityDenied`（清缓存 + 只渲染授权错误）。回归：`removes every S16 request/hold query after completion (receipt only)`（完成态 QueryClient 无 `["s16","deletions",<id>]` 精确键、无 `["s16","legal-holds"]` 键，receipt 键存在且有数据） |
| 6 | Approver 403 未重置 index | P2-1 | 完成 | `ApprovalSection` 403 分支增加 `setApproverIndex(approved + 1)`（连同 token、unknown、`approve.reset()`）；新主体不再继承旧序号/idempotency key。回归：`clears only the approver state on an approver 403` 扩展 — 按钮标签回到 `以第 1 名审批人批准`，APPROVE 请求恰好 1 次 |
| 7 | Expired hold 在 query/UI 仍显示生效 | P2-2 | 完成 | `s16.py` `query()` 为每个 hold 生成显式 `state`（`active`/`released`/`expired`）：expired 由 `legal_hold_expired` 事件或时钟越过 expiry 判定，绝不映射回 released/active；`s16_http.py` `S16LegalHoldSummary.state` 加入响应模型（generated api.ts 重新生成）；React 对 expired 显示“已过期”并隐藏 release 按钮；commit 的 active union 逻辑不变（已排除 expiry）。回归：`test_expired_hold_query_state_and_active_union`（固定 clock 越过 expiry → state expired、released False、active union 排除；release 后 released）+ HTTP `state` 断言 + UI `shows an expired legal hold as terminal without a release action` |
| 8 | Receipt 403 在 render 阶段更新父状态 | P2-3 | 完成 | `ReceiptSection` 403 清理移入 `useEffect`（带 ref 一次性 guard），render 保持纯函数；父组件 `handleIdentityDenied` 统一清缓存并渲染唯一授权错误。回归：receipt 403 测试扩展 — reference/preflight-button 全清、RECEIPT 请求恰好 1 次（重复 render 不触发额外 mutation） |
| 9 | R3 回归未覆盖上述边界 | P2-4 | 完成 | 新增 7 个受控回归 + 1 个 HTTP 断言 + 3 个 panel 测试扩展/新增（见上各 finding 证据列）：binding 泄漏扫描、release audit 对账、backup verify stale/conflict/replay、backup 双向损坏×4、expired query state、complete 后 request cache 移除、approver index reset、receipt render 403 一次性清理 |
| 10 | Diff trailing whitespace | P2-S1 | 完成 | `s16.py` 两处行尾空白删除（T4 对账函数上方空行、backup_deletion_intents DDL 后空行）；提交前 `git diff --check HEAD` 退出码 0；`git diff --check e9e9cbb...HEAD` 在 R4 提交后复跑（见下） |

## 精确验证命令执行记录（唯一执行集合；R4 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**57 passed**（controlled 44 + http 10 + t17_app 3）；耗时 35.33s；失败详情：无。

### 集合 B

```bash
.venv/bin/pytest -q \
  tests/test_s01_controlled.py::test_public_demo_scope_is_governed_deleted_at_24_hour_boundary \
  tests/test_s01_controlled.py::test_background_runtime_purges_due_public_demo_without_client_traffic \
  tests/test_s02_controlled.py::test_registered_detection_returns_atomic_accepted_receipt \
  tests/test_s02_controlled.py::test_observed_finding_traces_immutable_snapshot_to_source_receipt \
  tests/test_s12_controlled.py::test_frozen_run_is_isolated_insufficient_replayable_and_rerunnable \
  tests/test_s12_controlled.py::test_bundle_resolves_complete_frozen_replay_package \
  tests/test_s13_http.py::test_s13_http_query_shows_verification_completed_pending_and_received_distinct \
  tests/test_s15_policy_owner.py::test_governed_c19_release_authorizes_registered_reveal_for_tenant_resource \
  tests/test_s15_policy_owner.py::test_registered_session_cannot_reach_legacy_raw_routes
```

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 21.57s；失败详情：无。

### 集合 C

```bash
npm run generate:api
npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx
npx playwright test tests/test_t17_react.spec.js --workers=1
npm run build
```

- `generate:api`：退出码 0（openapi-typescript 7.13.0；`S16LegalHoldSummary.state` 进入 generated api.ts）。
- `test:unit`：退出码 0；**35 passed**（panel 24 + hooks 11）；耗时 5.54s。
- `playwright`：退出码 0；**2 passed**（desktop 1280x800 + mobile 390x844）；27.4s。
- `build`：退出码 0；静态产物更新（`index-DUyE5hjl.js` / `index-29T5SYwC.css`）。

### 只读 diff 检查

- 提交前：`git diff --check HEAD` 退出码 0（工作树无行尾空白）。
- 提交后：`git diff --check e9e9cbb27326b99b38edc81e4da3d1a793db7a43...HEAD` 退出码 0（该命令只检查 diff 格式，不执行项目代码）。R4 review 报告的 `s16.py:1481` 行尾空白位于 R3 历史提交内，已在 R4 提交中删除，因此最终 HEAD 范围的检查通过。

### 此前越界命令事实（按 R4 brief 要求记录）

- 本会话 R4 轮未执行任何越界命令。上一轮（R3）交付记录已声明：未运行完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes 或单独 typecheck。R4 轮同样未执行这些门禁。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes、单独 `npm run typecheck` 及其他完整项目门禁：全部未执行（不在允许集合内）。

## 变更文件总览

- `task4_consistency/controlled/s16.py`：T1 无原值 binding 快照、T2 release audit request id、T3 backup verify binding 证明 + already-absent binding 写入、T4 `_backup_reconciliation`（inventory/owner_healthy/readiness 共享）、T7 query hold `state`、T10 行尾空白清理。
- `task4_consistency/web/s16_http.py`：`S16LegalHoldSummary.state` 响应模型。
- `frontend/src/components/S16GovernedDeletionPanel.tsx`：T5 三表面重构（ActiveSurface/CompletedSurface/父级身份失效）、T6 approver index 重置、T7 expired hold UI、T8 receipt 403 移入 effect。
- `frontend/src/generated/api.ts`、`frontend/src/generated/openapi.json`：generate:api 产物。
- `task4_consistency/web/static/react/`：build 产物。
- `tests/test_s16_controlled.py`：新增 7 个 R4 回归（binding 泄漏、audit 对账、backup verify binding、backup 双向损坏×4、expired hold state、preflight binding no-value）+ 既有 tamper 测试更新（inventory 先于 delete fail-closed）。
- `tests/test_s16_http.py`：release 后 query `state` 断言。
- `frontend/src/components/S16GovernedDeletionPanel.test.tsx`：新增 P1-5 完成态缓存移除测试、P2-2 expired UI 测试，扩展 approver/receipt 403 测试（index 重置、一次性清理）。

## 备注

- 测试计数（R4 后）：controlled 44、http 10、t17_app 3、unit 35、playwright 2。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1/R2/R3/R4 review 与 fix brief / GitHub issue 状态：均未修改；未 push；issue 未关闭。
