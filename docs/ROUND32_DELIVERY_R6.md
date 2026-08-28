# ROUND32 R6 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex`（R6 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R6.md` + `docs/ROUND32_FIX_BRIEF_R6.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R6 review 一致）。
- R6 起始 HEAD（R5 交付）：`442ec8d`。
- R6 最终 HEAD：见文末 commit 记录（本文件提交后 `docs(s16): record R6 delivery HEAD …`）。

## R6 finding 修复状态（逐项，对应 brief 4 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | binding verify 以 ledger binding 代替当前 absence 验证 | P1-1 | 完成 | `s02.py`：`s02_verify_absent` binding 匹配后重新读取持久 absence rows + live object mappings 才返回 verified；`_absence_transaction` binding-replay 分支对复现值重新 `INSERT OR REPLACE` absence rows 并移除 live mappings（幂等 repair-forward）。`s12.py`：`s16_verify_absent` binding 匹配后 reload 并 scope 过滤检查目标 rows；`delete_scope_with_binding` binding-replay 分支对复现 rows 重新 DELETE。`s16.py` backup：verify binding 匹配后运行 reconciliation + scope manifest 扫描才 verified；delete binding-replay 分支发现复现 capture 时走 `_resume_or_fresh_delete` 重新删除（binding/intent 改 `INSERT OR REPLACE` 支持同一 operation 重执行）。错误 digest/scope 仍 conflict、低 fence 仍 stale、未知 operation 仍 missing；scope-only readiness probe 边界不变。回归：`test_s02_post_binding_resurrection_replay_repairs_forward`、`test_s12_post_binding_resurrection_replay_repairs_forward`、`test_backup_post_binding_resurrection_replay_repairs_forward`（binding 完成后注入 S02 对象/S12 row/backup capture → binding 证明失败、readiness 关闭、同 binding replay 重新执行删除效果、verify 恢复 verified） |
| 2 | 生产 S02 默认 absence store 未接入 boundary | P1-2 | 完成 | `app.py`：S01_SERVICE 构造的 `controlled_object_absence_store` 改为环境变量 `TASK4_S16_OBJECT_ABSENCE_PATH`（非空且绝对）或稳定默认 `Path(_s01_state_path).parent / "s02_object_absence.sqlite3"`；`_s16_service_factory` 删除未使用的局部 `absence_path` 计算与 `_s16_object_absence_path` 死代码，S02 owner 直接使用 boundary 已配置的路径。回归：`test_s02_default_absence_store_is_wired_in_production`（子进程导入 app、不设环境变量 → boundary absence_store_path 非空且指向默认文件）；Set A 全量 S02 删除流程证明 worker 不再因 `S02 absence store is not configured` 进入 repair_required |
| 3 | backup staged 只固定 manifest ID 不固定内容 | P1-3 | 完成 | `s16.py`：`backup_deletion_intents` 增加 `manifests_json` 列（迁移默认 '[]'）；staging 保存每个 manifest 的 value-free 快照（manifest_id、scope、entries_digest、manifest_digest、identities）；`_backup_delete_commit` 在任何 unlink 前逐 manifest 对账当前内容与快照（同 ID 的 JSON 原地改写 → 稳定 `S16_VERIFY_FAILED`、零 unlink）；staged 分支同样检测内容变更 → 替换 stale intent 重新 stage（repair-forward）；`manifest_ids` 非空而快照为空（老 schema 无法证明完整性）→ fail-closed 稳定失败。回归：`test_backup_staged_manifest_content_mutation_conflicts_and_repairs`（同 manifest ID 改写 entries_digest → 稳定失败 + 文件/manifest 零删除 + 同操作 repair-forward 完成 + healthy）；R4/R5 竞态与 shared-copy 回归保持通过 |
| 4 | R5 回归未覆盖复现与内容竞态 | P2-S1 | 完成 | 新增 5 个受控回归（S02/S12/backup 三 owner post-binding resurrection replay、backup same-ID manifest 内容变更、生产默认 absence store 接线），全部绑定固定 scope/operation/fence/digest 与 no-value 结果；断言 repair_required 语义（稳定错误、零 unlink）、readiness fail-closed、repair-forward 后 healthy。前端/生成物未修改，集合 C 按 brief 条件未运行 |

## 精确验证命令执行记录（唯一执行集合；R6 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**68 passed**（controlled 54 + http 11 + t17_app 3）；耗时 40.04s；失败详情：无。

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

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 20.57s；失败详情：无。

### 集合 C

本轮未修改 React 或任何生成物（变更仅限 `s02.py`/`s12.py`/`s16.py`/`app.py`/`tests/test_s16_controlled.py`），按 brief 条件**未运行**集合 C。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes、单独 `npm run typecheck` 及其他完整项目门禁：全部未执行（不在允许集合内）。
- 本轮无越界命令；未运行任何 review/brief 之外的可执行门禁。

## 变更文件总览

- `task4_consistency/controlled/s02.py`：verify binding 后当前状态复核；binding-replay 分支对复现值幂等重持久化。
- `task4_consistency/controlled/s12.py`：verify binding 后 reload 复核；binding-replay 分支对复现 rows 幂等重删除。
- `task4_consistency/controlled/s16.py`：backup verify/delete binding 后当前状态复核与重删除；`manifests_json` 列 + staging 内容快照 + commit 逐 manifest 内容对账 + staged 分支内容变更 repair-forward + intent/binding `INSERT OR REPLACE`。
- `task4_consistency/web/app.py`：S01 构造默认 absence store 接线；删除未使用路径计算与死代码。
- `tests/test_s16_controlled.py`：新增 5 个 R6 回归（三 owner resurrection replay、same-ID manifest 内容变更、生产默认 absence store）。

## 备注

- 测试计数（R6 后）：controlled 54、http 11、t17_app 3（Set A 总计 68）；前端 unit/playwright 未运行（无前端变更）。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1-R6 review 与 fix brief / R1-R5 delivery / GitHub issue 状态：均未修改；未 push；issue 未关闭。
