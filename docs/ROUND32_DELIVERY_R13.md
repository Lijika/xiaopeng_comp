# ROUND32 R13 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R13 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R13.md` + `docs/ROUND32_FIX_BRIEF_R13.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`182be27`（与 R13 review 一致）。
- R13 起始 HEAD（R12 交付）：`5513a25`。
- R13 实现 HEAD：`b4ff774`。
- R13 交付记录：`docs(s16): record R13 delivery HEAD b4ff774`。

## R13 finding 修复状态

| # | Finding | 级别 | 状态 | 文件与行号 / 固定操作 |
|---|---|---|---|---|
| 1 | shared source 在最终校验后仍可改写并 complete | P1-1 | 完成 | `s16.py`：`before_final_commit` 移到 refs/binding 写入前（`:3072`），hook 返回后再次 `_assert_converted_absence`。失败时该事务未写 refs/binding。binding replay 先 `_backup_reconciliation()`（`:2269`）。worker `verify_absent` 失败时 `owner_results.pop`（`:5106`）。 |
| 2 | 回归未覆盖 final-boundary 共享 source 改写 | P2-S1 | 完成 | `op-final-rewrite` fence=1 source_fence=1 scope A=`a*64`/B=`b*64`，`before_final_commit` 改写，bindings=0，修复后 complete，损坏 replay fail-closed（`:5654`）。`op-final-unlink`（`:5745`）。`op-final-oserror`（`:5796`）。worker `before_final_commit` 篡改，fence 1 `transitioned`/refs A=B=1，恢复后 fence 2 complete、source_fence=1、fence 1 `superseded`（`:5854`）。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**100 passed**（controlled 86 + http 11 + t17_app 3）；耗时 39.38s；失败：无。

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

- 退出码 0；**9 passed**；耗时 17.80s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R13.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R12 review/brief/delivery / GitHub issue：未修改；未 push。
