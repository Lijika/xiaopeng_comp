# ROUND32 R5 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex`（R5 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R5.md` + `docs/ROUND32_FIX_BRIEF_R5.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R5 review 一致）。
- R5 起始 HEAD（R4 交付）：`22f873d`。
- R5 最终 HEAD：见文末 commit 记录（本文件提交后 `docs(s16): record R5 delivery HEAD …`）。

## R5 finding 修复状态（逐项，对应 brief 5 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | S02/S12 absence verify 不校验 operation/fence binding | P1-1 | 完成 | `s02.py` `s02_verify_absent` 与 `s12.py` `s16_verify_absent` 增加 `operation_id`/`fence` 参数：带 binding 时读取各自 `*_deletion_bindings` 校验 scope + fingerprints digest → 匹配 verified；错误 digest/scope → `conflict`；低 fence → `stale`；无 binding 的 operation → `missing`（不证明 absent）；scope-only 调用保留为 readiness probe（S02 走 absence store 扫描、S12 走 scope 过滤行匹配）。adapter（`s16.py` S02/S12 owner）把 binding 结果映射为稳定 `S16_OWNER_BINDING_CONFLICT`/`S16_OWNER_STALE_FENCE`/可重试 `S16_VERIFY_FAILED`。回归：`test_s02_verify_binds_operation_fence_and_digest`、`test_s12_verify_binds_operation_fence_and_digest`（同 binding 重放 verified、未知 operation missing、stale fence、wrong digest conflict、scope-only probe 仍 absent）、`test_s12_single_owner_restore_replay_reopens_readiness`（单 owner S12 恢复 → 门禁关闭 → replay binding 重删 → readiness 重开） |
| 2 | Backup identity 无跨 scope 共享表达，删除破坏他 scope | P1-2 | 完成 | `s16.py` backup owner：新增 `backup_registry_refs`（identity+scope+manifest_id，PK identity+manifest_id）记录 manifest→identity 引用；`capture` 改为 `INSERT OR IGNORE` registry（永不覆盖仍被引用的行）+ 写 refs；schema 打开时对老 capture 回填 refs（自愈迁移）。`inventory` 计算 identity 的跨 scope 引用集：任一 identity 被 >1 个 scope 的 manifest 引用 → replica entry `shared` + `planned_action=S16_SHARED_COPY_REQUIRES_REPACK`（commit 经既有 `S16_SHARED_COPY_REQUIRES_REPACK` 门禁阻断）。`_backup_delete_commit` 引用感知清理：文件与 registry 行仅当 staged manifest 之外无引用时才删除；refs 行随 staged manifest 删除。回归：`test_backup_cross_scope_shared_copy_blocks_commit_and_survives`（双 scope 同 handle/digest → 双 inventory shared → commit `S16_SHARED_COPY_REQUIRES_REPACK` → scope A 删除后文件/registry/scope B manifest 存活且 reconciliation healthy） |
| 3 | Backup 删除阶段吞掉提交后新增 manifest | P1-3 | 完成 | staged intent 固定 `manifest_ids_json` + `identities_json` + scope + digest；`_backup_delete_commit` 在任何 unlink 前对账当前 scope manifest 集：出现 staged 集之外的新 manifest → 稳定 `S16_VERIFY_FAILED`（retryable=False，worker 记录 repair_required）、零 unlink、新 capture 保留；staged manifest 缺失视为崩溃 pass 自身 unlink（resume 容忍）。同 (op,fence) 重试发现 stale intent + 新 capture → 替换 intent 并重新 stage 当前全集（repair-forward 同一 job 完成）。回归：`test_backup_delete_conflicts_on_capture_after_stage_and_repairs_forward`（commit-window 竞态注入 → 稳定失败 + 双文件/双 manifest 完好 → 同操作重试 re-stage → complete + healthy） |
| 4 | Readiness gate 在身份校验前返回配置状态 | P2-1 | 完成 | `s16_http.py`：`_s16_governance_principal`/`_s16_approver_principal` 先做 credential 校验（未授权 → 稳定 403），再 `_s16_service` readiness（授权 + 不可用 → 503）；`app.py` `S16RestoreReadinessGate` 跳过 `/controlled/s16` 前缀（S16 路由自持身份-先-就绪顺序，其他受限 controlled 读保留共享门禁）；shell `_s16_require_governance` 同样 credential 先于 plane 状态。回归：`test_s16_readiness_identity_matrix_three_states`（ready / configured-but-unavailable / restore-closed 三态 × governance/普通用户/operator/S08 Admin/单一审批人/匿名 → 403/503 精确映射 + no-store）；`test_s16_routes_fail_closed_without_configuration` 更新为 R5 语义（未配置 + 无授权主体 → 403，不再泄露 503 状态） |
| 5 | R4 回归未覆盖上述边界 | P2-S1 | 完成 | 新增 5 个受控回归 + 1 个 HTTP 三态矩阵测试（见上各 finding 证据列）；每个案例固定 scope、operation、fence、digest、身份与 no-value 响应；R4 的 complete cache、expired hold、audit request、no-value binding 回归全部保留并通过 |

## 精确验证命令执行记录（唯一执行集合；R5 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**63 passed**（controlled 49 + http 11 + t17_app 3）；耗时 37.01s；失败详情：无。

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

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 19.86s；失败详情：无。

### 集合 C

```bash
npm run generate:api
npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx
npx playwright test tests/test_t17_react.spec.js --workers=1
npm run build
```

- `generate:api`：退出码 0（openapi-typescript 7.13.0，810ms；本轮无 schema 变更，产物一致）。
- `test:unit`：退出码 0；**35 passed**（panel 24 + hooks 11）。
- `playwright`：退出码 0；**2 passed**（desktop 1280x800 + mobile 390x844）；26.1s。
- `build`：退出码 0。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes、单独 `npm run typecheck` 及其他完整项目门禁：全部未执行（不在允许集合内）。
- 本轮无越界命令；未运行任何 review/brief 之外的可执行门禁。

## 变更文件总览

- `task4_consistency/controlled/s02.py`：`s02_verify_absent` operation/fence binding proof。
- `task4_consistency/controlled/s12.py`：`s16_verify_absent` operation/fence binding proof。
- `task4_consistency/controlled/s16.py`：S02/S12 adapter binding 结果映射；backup `backup_registry_refs` 表 + 老 capture refs 回填、capture `INSERT OR IGNORE` + refs 写入、inventory 跨 scope shared 检测、delete staged intent 固定 manifest 集、`_resume_or_fresh_delete`（fresh stage 全集 + stale intent 替换 repair-forward）、`_backup_delete_commit` 新 manifest 对账 + 引用感知文件/registry/refs 清理。
- `task4_consistency/web/app.py`：`S16RestoreReadinessGate` 跳过 S16 前缀；shell `_s16_require_governance` credential 先于 plane 状态。
- `task4_consistency/web/s16_http.py`：governance/approver principal 身份先于 readiness。
- `tests/test_s16_controlled.py`：新增 5 个 R5 回归 + R4 crash/repair 测试更新为含 manifest_ids_json 的忠实 staging。
- `tests/test_s16_http.py`：三态身份矩阵测试 + 未配置测试更新为 403 语义。

## 备注

- 测试计数（R5 后）：controlled 49、http 11、t17_app 3、unit 35、playwright 2。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1/R2/R3/R4/R5 review 与 fix brief / GitHub issue 状态：均未修改；未 push；issue 未关闭。
