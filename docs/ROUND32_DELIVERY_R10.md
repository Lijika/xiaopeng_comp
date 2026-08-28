# ROUND32 R10 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R10 review，只读）与本 OMP 会话；无 scout/subagent/额外 session。任务来源：`docs/ROUND32_REVIEW_R10.md` + `docs/ROUND32_FIX_BRIEF_R10.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`c469c4f`（与 R10 review 一致）。
- R10 起始 HEAD（R9 交付）：`1e8993c`。
- R10 实现 HEAD：`9c55317`。
- R10 交付记录：`docs(s16): record R10 delivery HEAD 9c55317`。

## R10 finding 修复状态

| # | Finding | 级别 | 状态 | 文件与行号 |
|---|---|---|---|---|
| 1 | complete binding 早于 quarantine 清除，OSError 被吞 | P1-1 | 完成 | `s16.py`：`_cleanup_quarantine` 不再吞 `OSError`（`:1691`）。`_purge_quarantine`（`:1715`）在 marker 事务提交后、refs/complete 前提交实体清除并验证 source+quarantine 双缺失（`:3001`）。unlink 失败 → 可重试 `S16_VERIFY_FAILED`，无 complete binding。`verify_absent` / `owner_healthy` 检测 quarantine residue。Hook：`before_purge` / `during_purge` / `after_purge` / `before_final_commit` / `before_return`。 |
| 2 | 更高 fence 无法接管旧 fence quarantine | P1-2 | 完成 | `backup_operation_fences` high-water（`:1590`，`:1770`）。`delete()` 入口 `fence < high_water` → stale（`:2179`）。同 operation/scope/digest 的 staged/transitioned intent 原子复制到新 fence，保留 `source_fence` 定位原 quarantine。迟到低 fence 无副作用。`process_next_deletion_job` 覆盖 lease 到期接管（测试 `:5110`）。 |
| 3 | fresh unknown-missing 写入 complete binding | P1-3 | 完成 | 无当前 fence intent 且 manifest 为空时先 `_backup_reconciliation`、quarantine residue、本 scope refs 与未完成 intent；任一异常/residue → `S16_VERIFY_FAILED` 且 ROLLBACK。仅可证明空 owner 才 already-absent binding。 |
| 4 | 回归未覆盖 cleanup 与生产 fence 接管 | P2-S1 | 完成 | 生产 hook 扩到 purge/return；OSError 阻断 complete（`:5025`）；fresh no-intent 零变化（`:5080`）；worker crash + lease expiry + fence 2 完成 + fence 1 stale + 唯一 receipt（`:5110`）；commit 后 stage 前外部缺失（`:5190`）。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**91 passed**（controlled 77 + http 11 + t17_app 3）；耗时 40.06s；失败：无。

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

- 退出码 0；**9 passed**；耗时 19.22s；失败：无。

### 集合 C 与未执行

- 集合 C 未运行（无 React/生成物变更）。
- 完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R10.md`（本文件）

## 备注

- complete 出现时该 operation 的 capture/manifest quarantine 已空。
- GOAL.md / STATUS.md / ROUND32_PLAN / 既有 review/brief/delivery / GitHub issue：未修改；未 push。
