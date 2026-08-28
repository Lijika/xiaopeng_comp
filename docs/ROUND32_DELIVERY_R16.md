# ROUND32 R16 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R16 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R16.md` + `docs/ROUND32_FIX_BRIEF_R16.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`9781d0a`（R14 交付，与 R16 review 一致）。
- R16 起始 HEAD（R15 交付）：`37350ee`。
- R16 实现 HEAD：`fc633d3`。
- R16 交付记录：`docs(s16): record R16 delivery HEAD fc633d3`。

## R16 finding 修复状态

| # | Finding | 级别 | 状态 | 测试 / 固定操作 / 注入点 / 状态转移 |
|---|---|---|---|---|
| 1 | 迟到旧 fence `verify_absent()` 可重完成旧 binding | P1-1 | 完成 | `s16.py`：`verify_absent` 在 binding proof 前读 `high_water`/`active_fence`（`:3352`），`fence < high_water` 或 `fence != active_fence` → `S16_OWNER_STALE_FENCE`，不调用 prove/mark。`_mark_binding_complete`/`_mark_binding_unverified` 增加 active-fence CAS（`:1883`-`:1946`）。 |
| 2 | 缺少迟到旧 fence verification 生产回归 | P2-1 | 完成 | `test_backup_worker_lease_takeover_completes_prior_fence_quarantine`：fence 2 complete 后 `delete(fence=1)=stale`，再 `verify_absent(fence=1)` → `S16_OWNER_STALE_FENCE`，bookkeeping 零变化。`test_backup_worker_before_return_shared_tamper_invalidates_binding`：`before_return` 篡改后 fence 2 complete、fence 1 `unverified`；迟到 `verify_absent(fence=1)` stale，high_water=active=2、source_fence=1、fence 1 仍 `unverified`、fence 2 仍 `complete`。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**105 passed**（controlled 91 + http 11 + t17_app 3）；耗时 49.86s；失败：无。

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

- 退出码 0；**9 passed**；耗时 22.95s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R16.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R15 review/brief/delivery / GitHub issue：未修改；未 push。
