# ROUND32 R18 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R18 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R18.md` + `docs/ROUND32_FIX_BRIEF_R18.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`7b583a4`（R16 交付，与 R18 review 一致）。
- R18 起始 HEAD（R17 交付）：`89177b4`。
- R18 实现 HEAD：`5a4eca8`。
- R18 交付记录：`docs(s16): record R18 delivery HEAD 5a4eca8`。

## R18 finding 修复状态

| # | Finding | 级别 | 状态 | 测试 / 迁移输入 / 注入点 / 状态转移 |
|---|---|---|---|---|
| 1 | 已有 operation-fence 行被信任 | P1-1 | 完成 | `_backfill_operation_fences` 对已有行调用 `_validate_existing_operation_fence`：与 binding/intent 推导的 high_water/active/source/scope/digest 不一致则写入 `backup_fence_migration_failures`，不改写 fence 行。`owner_healthy` False；inventory 抛 `S16_OWNER_INTEGRITY`；verify/delete/replay 走 stale。`_stale_operation_fence` 与 CAS 共用同一 registry 事务中已验证的 fence 快照（已有行不在热路径重推 in-flight 状态）。输入：`op-exist-bad` fence 表 high_water=active=1，binding/intent fence 2 complete。结果：failure=`op-exist-bad`，fence 仍 (1,1)，bookkeeping 零变化。 |
| 2 | 迁移 ambiguity 判定不完整 | P1-2 | 完成 | `_derive_operation_fence` 逐行要求非空 scope/digest、已知 status、source_fence 范围；按 fence 比较 scope/digest/source/identity/manifest 集合；同表 incomplete∩complete、binding `complete`+intent `staged`、非 active 同 fence source 冲突均失败。禁止插入 fence。输入：空 scope (`op-empty-fields`)、空 digest (`op-empty-digest`)、同 fence complete+staged (`op-status-conflict`)、fence1 binding source=1/intent source=2 且 fence2 complete (`op-source-conflict`)、status=`bogus` (`op-unknown-status`)。结果：health False，fence 计数 0，stale/zero-change。跨 scope/digest 与旧 schema 缺 fence 回归仍通过。 |
| 3 | 回归未覆盖已有 fence 损坏 | P2-1 | 完成 | `_backup_bookkeeping` 仍含 fences 全字段。新增 5 个测试 + helper `_assert_fence_migration_fail_closed`（inventory/verify/delete/replay + 全量 bookkeeping）。保留 `process_next_deletion_job`、binding replay、restore readiness、repair-forward、cross-scope refs 既有断言。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**112 passed**（controlled 98 + http 11 + t17_app 3）；耗时 48.61s；失败：无。

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

- 退出码 0；**9 passed**；耗时 22.58s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R18.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R17 review/brief/delivery / GitHub issue：未修改；未 push。
- lazy `_stale` 缺 fence 行时尝试迁移但不把 JSON 解析失败写成 durable migration failure，避免 in-flight staged intent 的 R9 解析路径被 stale 短路。
