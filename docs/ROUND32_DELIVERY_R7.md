# ROUND32 R7 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex`（R7 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R7.md` + `docs/ROUND32_FIX_BRIEF_R7.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R7 review 一致）。
- R7 起始 HEAD（R6 交付）：`4dd7809`。
- R7 最终 HEAD：见文末 commit 记录（本文件提交后 `docs(s16): record R7 delivery HEAD …`）。

## R7 finding 修复状态（逐项，对应 brief 3 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | backup manifest relocation 绕过 staged content/scope 校验并删除错误 scope | P1-1 | 完成 | `s16.py` `_backup_delete_commit`：对账索引改为**全部**当前 manifest（`by_id` 不再按请求 scope 过滤）；新增 snapshot 完备性检查（`snapshot_ids == staged_manifest_ids`，部分快照 → fail-closed）；逐 staged ID 校验存在性（缺失仅在 `resume` 且属崩溃 pass 已删时容忍，fresh 路径 out-of-band 缺失 → 稳定失败）、scope、entries_digest、`_digest(manifest)`、identity 集合与 snapshot 完全一致；scope relocation/内容/identity 变更 → 稳定 `S16_VERIFY_FAILED`（retryable=False，worker 记 repair_required）、零文件/manifest/registry/ref 变化。回归：`test_backup_staged_manifest_relocation_conflicts_and_preserves_scopes`（staged manifest 改到另一 scope → 稳定失败 + 原/他 scope 文件、manifest、registry、ref 全存活 + owner 可诊断 unavailable + 还原 tamper 后同操作 resume 完成且 healthy）、`test_backup_staged_intent_missing_snapshot_fails_closed`（staged ID 与快照集不完全对应 → fail-closed 零 unlink） |
| 2 | S02 默认 absence ledger 绑定 S01 目录而非 S16 目录 | P1-2 | 完成 | `app.py`：默认 absence 路径改为 `Path(TASK4_S16_STATE_PATH).parent / "s02_object_absence.sqlite3"`（S16 配置时）；`TASK4_S16_OBJECT_ABSENCE_PATH` 显式值保持优先并继续拒绝相对路径；S16 未配置时回退 S01 parent（该状态下 S16 不运行，语义不变）；S01 boundary / S02 owner / S16 factory 共享同一解析结果。回归：`test_s02_default_absence_ledger_binds_to_s16_state_parent`（S01 与 S16 父目录分离 → absence 路径 == S16 parent / `s02_object_absence.sqlite3` 且 ≠ S01 parent）；R6 的默认接线回归（仅设 S01 state）保持通过（回退分支） |
| 3 | R6 回归未覆盖 relocation/部分快照/跨恢复目录 | P2-S1 | 完成 | 新增 3 个受控回归（见上）；每项固定 scope、operation、fence、digest，断言稳定错误、零 unlink、repair_required 语义、owner health 可诊断与 no-value 结果；R6 的 resurrection、same-ID entries digest、shared-copy、竞态回归全部保持通过。前端/生成物未修改，集合 C 按 brief 条件未运行 |

## 精确验证命令执行记录（唯一执行集合；R7 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**71 passed**（controlled 57 + http 11 + t17_app 3）；耗时 38.27s；失败详情：无。

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

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 18.99s；失败详情：无。

### 集合 C

本轮未修改 React 或任何生成物（变更仅限 `s16.py`/`app.py`/`tests/test_s16_controlled.py`），按 brief 条件**未运行**集合 C。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes、单独 `npm run typecheck` 及其他完整项目门禁：全部未执行（不在允许集合内）。
- 本轮无越界命令；未运行任何 review/brief 之外的可执行门禁。

## 变更文件总览

- `task4_consistency/controlled/s16.py`：`_backup_delete_commit` 全 ID 对账（全部 manifest 索引、snapshot 完备性、逐 staged ID scope/内容/identity 校验、resume 容忍边界）。
- `task4_consistency/web/app.py`：默认 absence ledger 绑定 S16 state parent（显式环境变量优先、绝对路径校验、S16 未配置回退）。
- `tests/test_s16_controlled.py`：新增 3 个 R7 回归（relocation 冲突与修复、部分快照 fail-closed、S01/S16 分离父目录默认接线）。

## 备注

- 测试计数（R7 后）：controlled 57、http 11、t17_app 3（Set A 总计 71）；前端 unit/playwright 未运行（无前端变更）。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1-R7 review 与 fix brief / R1-R6 delivery / GitHub issue 状态：均未修改；未 push；issue 未关闭。
