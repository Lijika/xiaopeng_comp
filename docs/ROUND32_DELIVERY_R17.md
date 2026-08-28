# ROUND32 R17 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R17 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R17.md` + `docs/ROUND32_FIX_BRIEF_R17.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`37350ee`（R15 交付，与 R17 review 一致）。
- R17 起始 HEAD（R16 交付）：`7b583a4`。
- R17 实现 HEAD：`c0805b2`。
- R17 交付记录：`docs(s16): record R17 delivery HEAD c0805b2`。

## R17 finding 修复状态

| # | Finding | 级别 | 状态 | 测试 / 迁移输入 / 注入点 / 状态转移 |
|---|---|---|---|---|
| 1 | 旧 schema 缺 operation-fence 时 stale gate fail-open | P1-1 | 完成 | `_backfill_operation_fences`（`:1891`）在 schema 事务中按 operation 聚合 binding/intent：high_water=max fence；若 max 已 complete 则 active=high_water，否则最高 staged/transitioned/unverified；source_fence 绑定该尝试。冲突 scope/digest 写入 `backup_fence_migration_failures`（`:1614`）。缺失 fence+失败迁移 → stale；CAS 要求 `fence = active_fence`。`owner_healthy` 见失败表则 False（`:3638`）。 |
| 2 | bookkeeping 未覆盖 operation-fence 零变化 | P2-1 | 完成 | `_backup_bookkeeping` 增加 fences 全字段。`test_backup_old_schema_backfills_operation_fences_and_rejects_stale`（`:6603`）`op-old-mig` fence1 unverified + fence2 complete + source_fence=1 → 迁移 high_water=active=2 source_fence=1；`verify_absent(fence=1)` 与 `delete(fence=1)` stale，全量 bookkeeping 不变。`test_backup_ambiguous_fence_migration_fails_closed`（`:6691`）跨 scope/digest → health False。takeover/`before_return` 迟到 `verify_absent(fence=1)` 比较含 fences 的快照。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**107 passed**（controlled 93 + http 11 + t17_app 3）；耗时 50.11s；失败：无。

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

- 退出码 0；**9 passed**；耗时 21.54s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R17.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R16 review/brief/delivery / GitHub issue：未修改；未 push。
