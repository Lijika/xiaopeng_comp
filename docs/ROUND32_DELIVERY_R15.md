# ROUND32 R15 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R15 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R15.md` + `docs/ROUND32_FIX_BRIEF_R15.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`adbe9e2`（与 R15 review 一致）。
- R15 起始 HEAD（R14 交付）：`9781d0a`。
- R15 实现 HEAD：`ffbb55a`。
- R15 交付记录：`docs(s16): record R15 delivery HEAD ffbb55a`。

## R15 finding 修复状态

| # | Finding | 级别 | 状态 | 测试 / 固定操作 / 注入点 / 状态转移 |
|---|---|---|---|---|
| 1 | completed binding 在 verification 失败后仍被解释为 complete | P1-1 | 完成 | binding 持久化 identities/manifest_ids/source_fence（`:2697`）。proof 优先读 binding（`:1811`）。COMMIT 后 `before_return` 再证明，失败标 `unverified`（`:1883`，`:3270`）。replay/`verify_absent` 经 `_prove_or_invalidate_binding`（`:1903`）；成功后 `unverified→complete`。worker verify 失败同样 invalidate。 |
| 2 | 缺少 binding+worker+restore 组合回归 | P2-S1 | 完成 | `test_backup_completed_binding_unverified_until_shared_source_restored`（`:6303`）：job complete 后篡改 → `ready()=False`、fence 1 `unverified`、refs A=0/B=1、restore replay fail-closed；恢复后同 op/fence `already_absent`、status `complete`、active_fence=1 source_fence=1、重复 replay 幂等。`test_backup_worker_before_return_shared_tamper_invalidates_binding`（`:6421`）：`before_return` 篡改 → pending、fence 1 `unverified`；恢复后 fence 2 complete、source_fence=1、B refs=1。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**105 passed**（controlled 91 + http 11 + t17_app 3）；耗时 41.70s；失败：无。

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

- 退出码 0；**9 passed**；耗时 18.12s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R15.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R14 review/brief/delivery / GitHub issue：未修改；未 push。
