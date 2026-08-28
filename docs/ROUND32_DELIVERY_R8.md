# ROUND32 R8 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex`（R8 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R8.md` + `docs/ROUND32_FIX_BRIEF_R8.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R8 review 一致）。
- R8 起始 HEAD（R7 交付）：`64eb189`。
- R8 最终 HEAD：见文末 commit 记录（本文件提交后 `docs(s16): record R8 delivery HEAD …`）。

## R8 finding 修复状态（逐项，对应 brief 2 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | `resume=True` 把任意缺失 manifest 当作本次 crash pass 的删除结果 | P1-1 | 完成 | `s16.py`：`backup_deletion_intents` 新增 `unlinked_manifest_ids_json`（crash marker，schema 迁移默认 '[]'）；staging 写入空 marker，unlink 循环后在同一事务外以独立 COMMIT 持久化本次 pass 实际 unlink 的 manifest ID 集（绑定 operation_id+fence）。`_backup_delete_commit` 对缺失 staged manifest 仅容忍「`resume=True` 且该 ID 在 marker 内」；外部删除、marker 缺失/错绑、旧 intent、跨 scope 缺失 → 稳定 `S16_VERIFY_FAILED`（retryable=False）、事务回滚、零文件/manifest/registry/ref 变化、worker 进 repair_required。staged 分支对 missing-unproven 先做 residue 检查：当前 scope 仍有 manifest 或 staged identity 仍有 refs → 稳定失败；操作者修复清空全部 residue 后同 operation/fence 重试才 re-stage（已无残留的 already-absent 诚实完成）。合法 unlink-crash-resume：marker 证明 → 同 operation/fence 恢复完成且 `_backup_reconciliation()` healthy。回归：`test_backup_resume_rejects_out_of_band_manifest_deletion`（外部删除、无 marker、file/registry/ref 仍在 → 稳定失败 + 零 registry/ref + 无 binding + owner 可诊断 unavailable + 修复后 repair-forward 完成 healthy）、`test_backup_resume_rejects_mismatched_unlink_marker`（marker 指向不同 manifest → 稳定失败 + 零变化）、`test_backup_delete_resumes_after_crash_between_unlink_and_commit`（R4 既有，更新为携带 pass marker 的忠实 staging → 恢复完成） |
| 2 | R7 回归未覆盖 out-of-band deletion | P2-S1 | 完成 | 新增 2 个受控回归（见上）+ 复用既有 legal unlink-crash-resume 成功案例；每项固定 scope/operation/fence/digest，断言稳定 reason code、零 registry/ref/binding 变化、owner health 可诊断与 repair-forward 后 reconciliation healthy；R7 的 relocation/partial-snapshot、R6 的 resurrection、R5 的 shared-copy 与竞态回归全部保持通过。前端/生成物未修改，集合 C 按 brief 条件未运行 |

## 精确验证命令执行记录（唯一执行集合；R8 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**73 passed**（controlled 59 + http 11 + t17_app 3）；耗时 39.81s；失败详情：无。

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

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 20.76s；失败详情：无。

### 集合 C

本轮未修改 React 或任何生成物（变更仅限 `s16.py`/`tests/test_s16_controlled.py`），按 brief 条件**未运行**集合 C。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes、单独 `npm run typecheck` 及其他完整项目门禁：全部未执行（不在允许集合内）。
- 本轮无越界命令；未运行任何 review/brief 之外的可执行门禁。

## 变更文件总览

- `task4_consistency/controlled/s16.py`：`unlinked_manifest_ids_json` crash marker（schema + 迁移 + staging + 独立事务持久化）；`_backup_delete_commit` 缺失容忍仅限 marker 证明；staged 分支 missing-unproven residue 门控（稳定失败 vs 修复后 re-stage）。
- `tests/test_s16_controlled.py`：新增 2 个 R8 回归（out-of-band 删除拒绝、marker 错绑拒绝）+ 既有 crash-resume 测试更新为携带 pass marker 的忠实 staging。

## 备注

- 测试计数（R8 后）：controlled 59、http 11、t17_app 3（Set A 总计 73）；前端 unit/playwright 未运行（无前端变更）。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1-R8 review 与 fix brief / R1-R7 delivery / GitHub issue 状态：均未修改；未 push；issue 未关闭。
