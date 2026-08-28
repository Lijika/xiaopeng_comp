# ROUND32 R14 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R14 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R14.md` + `docs/ROUND32_FIX_BRIEF_R14.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`5513a25`（与 R14 review 一致）。
- R14 起始 HEAD（R13 交付）：`adbe9e2`。
- R14 实现 HEAD：`ec63dfc`。
- R14 交付记录：`docs(s16): record R14 delivery HEAD ec63dfc`。

## R14 finding 修复状态

| # | Finding | 级别 | 状态 | 测试 / 固定操作 / 注入点 / 状态转移 |
|---|---|---|---|---|
| 1 | binding replay 未按 operation/fence 证明 shared source | P1-1 | 完成 | `s16.py` `_assert_completed_binding_proof`（`:1785`）校验 identity 集合、handle、digest、跨 scope refs、quarantine。`delete()` already-absent（`:2345`）与 `verify_absent`（`:3250`）共用。`_owner_copies_present` 将 `S16Unavailable` 视为未证明（`:5743`）。 |
| 2 | 缺少 worker retry / restore / replay 组合回归 | P2-S1 | 完成 | `test_backup_binding_replay_requires_shared_source_digest`（`:5977`）`op-replay-proof` fence=1 source_fence=1 scope A=`a*64`/B=`b*64`：complete 后 rewrite/unlink/OSError → `S16_VERIFY_FAILED`，binding 仍 complete，恢复后 `already_absent`。`test_backup_worker_restore_readiness_after_shared_source_corruption`（`:6069`）：job complete 后篡改 → `ready()=False`、`process_next=idle`、`replay_restore_if_needed` fail-closed；恢复后 `ready()=True`，refs A=0/B=1，active_fence=1 source_fence=1。`test_backup_worker_retry_exhaustion_then_repair_forward`（`:6166`）`before_final_commit` 篡改，max_attempts=2：pending → repair_required、bindings=0；restore+`backup-repair-verified` 后 fence 3 complete、fence 1 `superseded`、source_fence=1。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**103 passed**（controlled 89 + http 11 + t17_app 3）；耗时 40.88s；失败：无。

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

- 退出码 0；**9 passed**；耗时 19.37s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R14.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R13 review/brief/delivery / GitHub issue：未修改；未 push。
