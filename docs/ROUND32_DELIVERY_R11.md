# ROUND32 R11 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`。协作边界：仅 `ticket32_codex`（R11 review，只读）与本 OMP 会话。任务来源：`docs/ROUND32_REVIEW_R11.md` + `docs/ROUND32_FIX_BRIEF_R11.md`（未编辑）。

## 固定基线与 HEAD

- 固定基线：`1e8993c`（与 R11 review 一致）。
- R11 起始 HEAD（R10 交付）：`9f7dce5`。
- R11 实现 HEAD：`c655723`。
- R11 交付记录：`docs(s16): record R11 delivery HEAD c655723`。

## R11 finding 修复状态

| # | Finding | 级别 | 状态 | 文件与行号 |
|---|---|---|---|---|
| 1 | purge 未证明 capture 原路径缺失 | P1-1 | 完成 | `s16.py`：`_assert_converted_absence`（`:1724`）对每个 identity 读 registry handle，解析 `_capture_target`；exclusive 要求 source 与 source-fence quarantine 双缺失；handle/path 异常 → 稳定可重试 `S16_VERIFY_FAILED`，无 locator。`_purge_quarantine`（`:1803`）与 final txn 前（`:3056`）各执行一次。source 重建测试：`:5279`。 |
| 2 | fence 接管留下旧 staged/transitioned intent | P1-2 | 完成 | 接管同一事务将其他 fence 的 staged/transitioned 标为 `superseded`（`:2488`）。`open_intents` 仍只计 staged/transitioned。worker takeover 断言 fence 1=`superseded`、open_intents=0、active_fence=2/source_fence=1、同 scope already-absent 与 replay（`:5110`/`:5190`）。迟到 fence 1 stale。 |
| 3 | 回归未覆盖 source absence 与旧 intent 收束 | P2-S1 | 完成 | source recreation 阻断 complete（`:5279`）；purge OSError、四边界 crash、worker lease takeover、fresh unknown-missing、shared-ref、locator-free 保留并加强。 |

## 精确验证

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**92 passed**（controlled 78 + http 11 + t17_app 3）；耗时 40.65s；失败：无。

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

- 退出码 0；**9 passed**；耗时 19.59s；失败：无。

### 未执行

- 集合 C、完整 pytest、完整 Playwright、`ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck：均未执行。

## 变更文件

- `task4_consistency/controlled/s16.py`
- `tests/test_s16_controlled.py`
- `docs/ROUND32_DELIVERY_R11.md`（本文件）

## 备注

- GOAL.md / STATUS.md / ROUND32_PLAN / R1-R11 既有 review/brief/delivery / GitHub issue：未修改；未 push。
