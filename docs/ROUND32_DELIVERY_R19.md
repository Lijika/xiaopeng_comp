# ROUND32 R19 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R19 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R19.md` + `docs/ROUND32_FIX_BRIEF_R19.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`89177b4`（R17 交付，与 R19 review 一致）。
- R19 起始 HEAD（R18 交付）：`bfe6071`。
- R19 实现 HEAD：`0fd6af8`。
- R19 交付记录：`docs(s16): record R19 delivery HEAD 0fd6af8`。

## R19 finding 修复状态

| # | Finding | 级别 | 状态 | 测试 / 迁移输入 / 注入点 / 状态转移 |
|---|---|---|---|---|
| 1 | orphan operation-fence 绕过校验 | P1-1 | 完成 | `_backfill_operation_fences_inner` 对 `present - grouped` 写 `backup_fence_migration_failures`，不改 orphan 行。`_stale` 对已有 fence 且无 binding/intent 同样记录失败。输入：`op-orphan` 仅 fence (1,1,1)。结果：health False，inventory `S16_OWNER_INTEGRITY`，verify/delete/replay stale，fence 字段不变。 |
| 2 | lazy migration `record_failure=False` 吞失败 | P1-2 | 完成 | 移除静默路径。缺 fence 时 `_migrate_one_operation_fence` 失败即写入 failure 并 stale。全新 operation（无 history）仍允许首次 stage。损坏 history 首次 lazy：`op-lazy-bad` 空 scope binding、无 fence → verify/delete/replay stale，failure=`op-lazy-bad`，不插 fence。合法 staged intent 无 fence 仍可 derive 后 resume。损坏 `identities_json='{'` 的 in-flight 现记 migration failure 并 stale（零 effect）。 |
| 3 | 跨表状态关系不完整 | P1-3 | 完成 | 拒绝 binding `{complete,committed}` × intent `{staged,transitioned}` 及其反向 `{staged,transitioned}` × `{complete,committed}`。同表 incomplete∩complete 仍失败。参数化 6 组：complete/staged、complete/transitioned、committed/staged、committed/transitioned、staged/committed、transitioned/complete。一致 complete/committed 与 unverified+transitioned 历史保持原语义。 |
| 4 | numeric 解析让启动抛异常 | P2-1 | 完成 | `_parse_fence_int` 拒绝 bool/NULL/非数字/≤0；history 逐行捕获后走 derive 失败。existing 非数字在 `_validate_existing` 记 failure。backfill 外层异常记全部可见 operation。live `_stale` 用 `_coerce_live_fence_int` 允许 restore `fence=0`。输入：`op-num-text` fence=`abc`、`op-num-zero` 0、`op-num-neg` source=-1、`op-num-exist` high_water=`bad`。结果：均 failure，不插入新 fence，existing 文本字段保留，构造不抛。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**121 passed**（controlled 107 + http 11 + t17_app 3）；耗时 64.41s；失败：无。

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

- 退出码 0；**9 passed**；耗时 29.78s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R19.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R18 review/brief/delivery / R19 review/brief / GitHub issue：未修改；未 push。
- live stale 比较允许整数 0，以免 restore replay 的 `fence=0` 被迁移范围规则误杀。
