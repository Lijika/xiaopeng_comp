# ROUND32 R3 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex`（R3 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R3.md` + `docs/ROUND32_FIX_BRIEF_R3.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`601ea6e`（R2 交付 HEAD）。
- R3 最终 HEAD：见文末 commit 记录（本文件提交后 `docs(s16): record R3 delivery HEAD …`）。

## R3 finding 修复状态（逐项，对应 brief 18 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | S02/S12 replay 无显式 op/fence/scope 契约 | P0-1 | 完成 | `s16.py` `DeletionOwner` Protocol `replay` 显式声明 `operation_id`/`fence`/`scope_fingerprint`；S02/S12 adapter replay 转发真实 op/fence（不再伪造 `s16-replay`/`fence=0`），binding 冲突→`S16_OWNER_BINDING_CONFLICT`；S01 adapter `verify_absent` 接受 op/fence；worker/readiness/replay 的 verify 全部携带 job 或 replay 的 op/fence。测试：`test_s02_single_owner_restore_replays_with_operation_fence_binding`（S02 单 owner 恢复 → 门禁关闭 → 按 replay binding 重删 → 门禁重开）。R3 前遗留：backup verify 缺 kwargs 修复（`BackupDeletionOwner.verify_absent` 接受 op/fence） |
| 2 | 部分配置时 fail-open | P0-2 | 完成 | `app.py`：`S16_CONFIGURED` 改为三态 OR（identities **或** state path **或** backup root），alias-controlled 仍 AND 排除；`_s16_domain_read_gate` 与 `_install_s16_read_gates()` 移到模块级，factory 失败也把门禁装到 S01/boundary/S12（fail-closed）。测试：`test_partial_s16_configuration_fails_closed`（子进程导入 app，仅 state path+backup 配置、identities 空 → `S16_CONFIGURED=True`、`S16_SERVICE=None`、门禁关闭） |
| 3 | S12 verify 扫描全部 bindings | P1-1 | 完成 | `s12.py` `s16_verify_absent` 按目标 `fingerprints_digest` 过滤 `s12_deletion_bindings`；无关 scope 的绑定不再触发 mismatch；行匹配同时按 scope 过滤。测试：`test_s12_delete_and_verify_are_scope_aware_across_equal_digests`（B scope 同 digest 行存在时 verify A 无 mismatch） |
| 4 | S12 delete 全局指纹扫描 | P1-2 | 完成 | `s12.py` `_s16_rows_by_fingerprint(fingerprints, scope_fingerprint=…)`：仅 plan 引用集合 == {目标 scope} 的 plan 及其 job/bundle 可删；同 digest 但归属他 scope 的行绝不删除。测试：同跨 scope 测试（job-b 与 plan-a 同 digest，scope-A 删除后 job-b 幸存） |
| 5 | S01 tombstone 未携带 op/fence | P1-3 | 完成 | `s01.py` `s16_apply_deletion` already-absent 分支与 `s16_verify_absent` 均按 `operation_id`/`fence` 校验 tombstone（`s16_tombstone_verified` 的 fence=0 不再被 `or -1` 吞掉）；worker/replay 传递当前 job 或 replay 的 binding。测试：`test_stale_tombstone_binding_never_proves_absence`（错误 op/fence → 不 absent；正确 binding → absent） |
| 6 | reveal/settlement/S02/S12 读取缺统一门禁 | P1-4 | 完成 | `s01.py`：`reveal_field_observation`、`settlement_view` 入口 `s16_require_read_ready(application_id)`；`s02.py` `RegisteredSourceBoundary.read_object` 增加 `s16_read_gate` 检查（关闭→LookupError）；`s12.py` `EvaluationService.query_job/query_bundle` 增加 `s16_require_read_ready()`（关闭→ValueError）；`app.py` 模块级 `_install_s16_read_gates()` 无条件装配。测试：`test_restore_readiness_gate_closes_every_domain_retrieval`（关闭时 reveal/settlement/read_object/query_job/query_bundle 全部拒绝，打开后恢复） |
| 7 | Security Audit availability 未绑 callable writer | P1-5 | 完成 | `s16.py` 构造器：available 而 writer 非 callable → ValueError；writer 非 None 而 available=False → ValueError；最终 availability = available 且 callable。`app.py` factory：availability 需 `callable(S01_SERVICE.audit_writer)`。测试：`test_non_callable_audit_writer_fails_closed_at_construction` + 既有 `test_s16_factory_wires_real_audit_seam_and_fails_closed_without_writer` |
| 8 | Legal Hold generation 未包含过期转换 | P1-6 | 完成 | `s16.py`：`_HOLD_TRANSITION_TYPES`（impose/released/expired）；`_expire_holds_in_transaction(connection, scope)` 在 preflight/impose/release/commit 事务内追加 `legal_hold_expired`（generation 从 DB 行派生）；release 的 already-released 分支绑定**真实** prior release 的 generation/request_id（绝不伪造下一世代）；HTTP release 响应返回 `request_id` + `generation`。测试：`test_hold_release_has_own_request_and_generation`（扩展响应断言）+ `test_s16_legal_hold_http_command_surface` |
| 9 | repair 先判 job 状态再绑 key | P1-9 | 完成 | `repair()`：`_bind_or_replay` 在事务内先于 `job.status != "repair_required"` 判断；同 key 重试在 job 回到 pending 后仍重放原结果；新 key + 非 repair_required → `S16_BLOCKED(S16_REPAIR_REQUIRED)`。测试：`test_repair_same_key_replays_after_job_resumed` |
| 10 | preflight replay 重新盘点 | P1-8 | 完成 | preflight 把完整无值响应（不含 application_reference）持久化到 `s16_bindings`；同 key 重试直接返回首个 manifest 快照（不重新 inventory、不重新 `_build_manifest`）；重放前校验 reference 仍可解析（删除后同 key → 404 存在性隐藏）。测试：`test_preflight_replay_returns_original_manifest_after_hold_impose`（两次 preflight 之间 impose hold，重放仍是首版 manifest_digest）+ 既有 t17 删除后同 key 404 |
| 11 | 提交事务内无完整性验证 | P1-11 | 完成 | `_ledger_facts_in_transaction` 读 `event_id/payload/integrity_sha256` 并逐行重算 `_integrity_digest("s16_events", …)`；不匹配 → `S16Unavailable`（回滚、零状态变化）。测试：`test_audit_or_deletion_ledger_outage_has_zero_commit_effect`（既有，保持通过） |
| 12 | 老 schema 迁移列无回填 | P1-12 | 完成 | `S16Ledger._ensure_schema`：5 条 `ALTER TABLE s16_jobs ADD COLUMN` 后按 payload 回填 `status/lease_owner/lease_expires_at/fence/attempt`（`WHERE job_id=? AND (status IS NULL OR status='')`），并写入 `s16_meta_facts`（`s16_jobs_schema_migration`）。测试：`test_pre_r2_job_schema_backfills_columns_and_records_migration_fact` |
| 13 | commit 只检查 s01/s12 健康与 revision | P1-13 | 完成 | `BackupDeletionOwner.store_revision()`（registry identities digest）/`owner_healthy()`（registry 可读）；`ExportTempOwner.store_revision()`（固定零证摘要）/`owner_healthy()`（恒真）；manifest 与 preflight event 增加 `backup_revision`/`s17_revision`；`_commit_block_reason` 检查全部 5 个 required owner 的 revision 与健康。测试：既有 commit 链路全部保持（revision drift → `S16_REVISION_CHANGED`） |
| 14 | backup 删除崩溃不可恢复 | P1-14 | 完成 | `BackupDeletionOwner.delete`：先在 `backup_deletion_intents`（op+fence PK，含 `identities_json`）持久化 staged intent，再 unlink；完成阶段 `_backup_delete_commit` 幂等收尾（resume 容忍已删文件、补齐未删文件、registry 清理 + binding + intent committed 单事务提交）。崩溃后同 (op,fence) 重试直接恢复。测试：`test_backup_delete_resumes_after_crash_between_unlink_and_commit`（手工 staged intent + 已 unlink → delete 恢复完成 + verified） |
| 15 | complete 后 S16 缓存仅 invalidate | P1-7 | 完成 | `hooks.ts` `clearApplicationScopedCache`：`removeQueries(["s16","deletions"])` + `["s16","legal-holds"]`（不再 invalidate）；`hooks.s16.test.tsx` 断言改为 S16 request/hold 数据被移除。面板完成分支：remove 后 `fetchQuery` 重新填充 request query + `key` 重挂载 receipt（规避 v5 removeQueries 对 enabled-gated observer 不自动 refetch 的行为），once-guard 防止重复清理循环。测试：`clearApplicationScopedCache` 更新断言 + 既有完成流程测试 |
| 16 | React 身份失效未覆盖 receipt/process/approver | P1-15 | 完成 | `S16GovernedDeletionPanel`：receipt query 403 → `onIdentityDenied`（完整治理失效）；process mutation 403 → `onIdentityDenied`；approver 403 → 仅清 approver token/index/错误（`approve.reset()`），治理面保留。测试：`invalidates the governance identity on a receipt query 403` / `…on a process mutation 403` / `clears only the approver state on an approver 403` |
| 17 | job CAS 未验证五个原值 | P1-10 | 完成 | `_claim_job_cas` 条件 UPDATE 比较 status（精确）+ lease_expires_at（`IS ? OR = ?`）+ fence + attempt，job_id 不变；失败 → 放弃 claim。测试：`test_claim_cas_rejects_stale_five_originals`（B 实例持旧快照 → CAS 拒绝）+ 既有 `test_cross_instance_claim_cas_grants_one_lease` |
| 18 | 残余路径无定向回归 | P2-1 | 完成 | 新增 11 个受控回归 + 2 个前端 403 测试 + HTTP release 响应断言（见上各 finding 的测试列） |

## 精确验证命令执行记录（唯一执行集合；R3 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**49 passed**（controlled 36 + http 10 + t17_app 3）；耗时 32.27s（最终全量复跑）；失败详情：无。

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

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 21.60s；失败详情：无。

### 集合 C

```bash
npm run generate:api
npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx
npx playwright test tests/test_t17_react.spec.js --workers=1
npm run build
```

- `generate:api`：退出码 0（openapi-typescript 7.13.0，606ms）。
- `test:unit`：退出码 0；**33 passed**（panel 22 + hooks 11）；耗时 4.45s。
- `playwright`：退出码 0；**2 passed**（desktop 1280x800 + mobile 390x844）；21.9s。
- `build`：退出码 0；`frontend/src/components/S16GovernedDeletionPanel.tsx` 等编译通过，静态产物更新（`task4_consistency/web/static/react/assets/index-BMuCo2oZ.js`）。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes 及其他完整门禁：全部未执行（不在允许集合内）。

## 变更文件总览

- `task4_consistency/controlled/s16.py`：T1/T5/T7/T8/T9/T10/T11/T13/T14/T15/T16 的全部 ledger/orchestrator/owner 改动（Protocol replay 契约、hold 世代与过期、repair/preflight 幂等、claim CAS、迁移回填、audit callable、backup crash-recovery、全部 owner 健康/revision、readiness verify 语义）。
- `task4_consistency/controlled/s01.py`：tombstone op/fence 绑定（fence=0 修复）、`s16_verify_absent` 扩展、reveal/settlement 读取门禁。
- `task4_consistency/controlled/s02.py`：boundary `read_object` 读取门禁。
- `task4_consistency/controlled/s12.py`：scope-aware 行匹配/删除、按 digest 过滤 binding 的 verify、query 门禁。
- `task4_consistency/web/app.py`：三态 `S16_CONFIGURED`、模块级 `_s16_domain_read_gate` + `_install_s16_read_gates()`、audit writer callable 检查。
- `task4_consistency/web/s16_http.py`：release 响应返回 `request_id` + `generation`。
- `frontend/src/api/hooks.ts`：`clearApplicationScopedCache` 移除 S16 缓存、`s16RequestQueryFn`/`s16ReceiptQueryFn` 导出。
- `frontend/src/components/S16GovernedDeletionPanel.tsx`：receipt/process 403 → identity denied、approver 403 局部清理、完成分支保留终态 job 状态、缓存清理 once-guard + receipt remount。
- `tests/test_s16_controlled.py`：新增 11 个 R3 回归（S02 单 owner 恢复、S12 跨 scope、stale tombstone、preflight replay、repair replay、claim CAS、老 schema、backup crash、读取门禁、audit writer 构造、部分配置 fail-closed）。
- `tests/test_s16_http.py`：release 响应断言扩展。
- `frontend/src/api/hooks.s16.test.tsx`：cache 移除断言更新。
- `frontend/src/components/S16GovernedDeletionPanel.test.tsx`：新增 3 个 403 状态测试。
- `frontend/src/generated/api.ts`、`frontend/src/generated/openapi.json`：`npm run generate:api` 产物（s16_http release 字段变更后重新生成）。
- `task4_consistency/web/static/react/`：`npm run build` 产物。

## 备注

- 测试计数（R3 后）：controlled 36、http 10、t17_app 3、unit 33、playwright 2。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1/R2/R3 review 与 fix brief / GitHub issue 状态：均未修改；未 push；issue 未关闭。
