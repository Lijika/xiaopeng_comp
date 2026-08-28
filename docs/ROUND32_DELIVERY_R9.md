# ROUND32 R9 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-29。实现会话：`ticket32_omp`（本会话，中断后由新进程接管同一工作树）。协作边界：仅 `ticket32_codex`（R9 review，保持只读）与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R9.md` + `docs/ROUND32_FIX_BRIEF_R9.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`64eb189`（与 R9 review 一致）。
- R9 起始 HEAD（R8 交付）：`c469c4f`。
- R9 实现 HEAD：`f214314`。
- R9 交付记录提交：本文件 `docs(s16): record R9 delivery HEAD f214314`。

## R9 finding 修复状态（逐项，对应 brief 3 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | 受控 unlink 与 durable marker 之间不可恢复 | P1-1 | 完成 | `s16.py`：owner-private `.s16_deletion` quarantine。exclusive capture 与 staged manifest 在 `_backup_delete_commit` 内先 `rename` 到 `operation_id+fence+pass+identity/manifest_id` 绑定 token（`:1605`-`:1650`，`:2533`-`:2682`），再独立事务写入 `unlinked_manifest_ids_json`（`:2682`）。四个生产边界 hook：`before_transition` / `after_transition` / `before_marker_commit` / `before_final_commit`。缺失文件仅当对应 quarantine 存在才视为本 pass 转换；`resume=True` 或 marker 不能单独证明 FS 转换。未知缺失、错 operation/fence、旧 schema、跨 pass quarantine → `S16_VERIFY_FAILED`、零 registry/ref/binding 变化。合法 crash-resume 同 operation/fence 完成且不重复 exclusive 删除。 |
| 2 | missing-unproven 只查首个 identity，漏 registry | P1-2 | 完成 | `s16.py`：`_staged_identities_have_residue`（`:1692`）遍历 staged identities 全集，任一 `backup_registry_refs` 或 `backup_registry` 行（含共享/跨 scope refs）或 JSON/SQL 解析失败均 fail-closed。`delete()` missing-unproven（`:2180`）在 complete/re-stage 前使用该全量门控。 |
| 3 | 回归预置 marker，未覆盖生产时序与多 identity | P2-S1 | 完成 | `tests/test_s16_controlled.py`：成功 crash-resume 改为生产 `delete()` + `_crash_hook`，在真实 rename 后、marker COMMIT 前注入（`:2761`，`:4584` 四边界）。新增错 fence/operation、旧 schema、多 identity residue、shared-ref crash-resume、corrupt identities；保留 R8 oob/错 marker。断言 receipt 无 backup root / `.s16_deletion` / 文件名。 |

## 精确验证命令执行记录（唯一执行集合；R9 review 保持只读）

### 集合 A

```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```

- 退出码 0；**83 passed**（controlled 69 + http 11 + t17_app 3）；耗时 45.91s；失败详情：无。

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

- 退出码 0；**9 passed**（S01 24h ×2、S02 ×2、S12 ×2、S13 ×1、S15 ×2）；耗时 25.40s；失败详情：无。

### 集合 C

本轮未修改 React 或任何生成物（变更仅限 `s16.py` / `tests/test_s16_controlled.py`），按 brief 条件**未运行**集合 C。

### 未执行（明确排除）

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、evaluate、attack_probes、构建、生成、lint、typecheck 及其他完整项目门禁：全部未执行（不在允许集合内）。

## 变更文件总览

- `task4_consistency/controlled/s16.py`：owner-private quarantine 状态机（capture + manifest）；四边界 fault injection；missing-unproven 全 identity refs/registry 门控。
- `tests/test_s16_controlled.py`：生产 helper crash-resume；四边界 / 错绑定 / 旧 schema / 多 identity residue / shared-ref / 解析失败回归。

## 备注

- 测试计数（R9 后）：controlled 69、http 11、t17_app 3（Set A 总计 83）；前端 unit/playwright 未运行（无前端变更）。
- GOAL.md / STATUS.md / docs/ROUND32_PLAN.md / R1-R9 review 与 fix brief / R1-R8 delivery / GitHub issue 状态：均未修改；未 push；issue 未关闭。
