# ROUND32 R12 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R12 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R12.md` + `docs/ROUND32_FIX_BRIEF_R12.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`9f7dce5`（与 R12 review 一致）。
- R12 起始 HEAD（R11 交付）：`182be27`。
- R12 实现 HEAD：`dce8463`。
- R12 交付记录：`docs(s16): record R12 delivery HEAD dce8463`。

## R12 finding 修复状态

| # | Finding | 级别 | 状态 | 文件与行号 / 固定操作 |
|---|---|---|---|---|
| 1 | shared identity source 缺失或改写仍可通过 purge | P1-1 | 完成 | `s16.py` `_assert_converted_absence`（`:1724`）：`still_referenced` 时读 `handle, content_sha256`（`:1751`），要求 source 存在、digest 匹配、quarantine 不存在（`:1771`-`:1776`）。缺失/改写/OSError/越界 → `S16_VERIFY_FAILED`。检查点：`_purge_quarantine` 后（`:1809`）与 refs 删除前（`:3062`）。exclusive 仍双缺失。 |
| 2 | 回归未覆盖共享 source 完整性 | P2-S1 | 完成 | `op-shared-unlink` fence=1 source_fence=1 scope A=`a*64` / B=`b*64`，`before_purge` unlink（`:5340`）；`op-shared-rewrite` `after_purge` 改写（`:5421`）；`op-shared-oserror` `before_purge` 读失败（`:5480`）；worker job 在 commit 后补 scope B，`before_purge` 篡改，fence 1 `transitioned` / bindings=0，恢复后 fence 2 complete、source_fence=1、fence 1 `superseded`、B refs=1（`:5538`）。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**96 passed**（controlled 82 + http 11 + t17_app 3）；耗时 38.03s；失败：无。

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

- 退出码 0；**9 passed**；耗时 17.79s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R12.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R12 既有 review/brief/delivery / GitHub issue：未修改；未 push。
