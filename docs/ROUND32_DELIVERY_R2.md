# ROUND32 R2 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex` 与本 OMP 会话；无任何 scout/subagent/额外 session。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R2.md` + `docs/ROUND32_FIX_BRIEF_R2.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R2 review 一致）。
- R2 起始 HEAD（R1 交付）：`bba05c0`。
- R2 最终 HEAD：`21664ea5671a793a743cc10373989fce3da7e6d5`（`fix(s16): R2 review — per-owner readiness, binding uniqueness, audit wiring`，12 files changed；R1 交付 `bba05c0` 之上）。

## R2 finding 修复状态（逐项，对应 brief 17 项任务）

| # | Finding | 级别 | 状态 | 文件与定向证据 |
|---|---|---|---|---|
| 1 | 恢复 readiness 只观察 S01 scope | P0-1 | 完成 | `s16.py` `ready()`/`replay_restore_if_needed()`/`_replay_restore()` 改为遍历 completed job 的每个 owner fingerprint 集合（`_owner_copies_present`/`_replay_job_owners`）；replay fact 仅在全部 owner 验证后追加。测试：`test_single_owner_restore_closes_readiness_until_replayed`（backup 单 owner 恢复关闭门禁）、`test_receipt_append_only_and_replay_facts_immutable`（无恢复不追加 fact，恢复后追加并 verified） |
| 2 | S16 构造失败时共享门禁被绕过 | P0-2 | 完成 | `app.py` 新增静态 `S16_CONFIGURED`；`S16RestoreReadinessGate` 在 `configured and service is None` 时同样 fail-closed（`S16_RESTORE_READINESS_UNAVAILABLE`）；S16 路由保持 scoped 503。测试：`test_s16_routes_fail_closed_without_configuration`（未配置=仅 S16 关闭）+ 门禁中间件路径 |
| 3 | backup manifest 持久化可定位 handle | P1-1 | 完成 | `s16.py` BackupDeletionOwner：manifest 只保存 `connector_identity`（handle+digest 的不可反查摘要）与内容摘要；handle→对象映射移入 owner 内部 `backup_owner.sqlite3` registry。测试：`test_backup_capture_path_boundary_and_delete_verification`（断言 manifest 无 handle/path） |
| 4 | backup owner 丢弃 scope/operation/fence | P1-2 | 完成 | backup 增加 `backup_deletion_bindings`（op+fence PK，scope+digest 同存）；binding 检查先于 manifest 查找；scope/digest 不匹配→`S16_OWNER_BINDING_CONFLICT`；低 fence→stale；replay 接收派生 operation。测试：`test_backup_binding_rejects_scope_or_digest_mismatch` |
| 5 | S02/S12 binding 重放不比较 scope/digest | P1-3 | 完成 | `s02.py`/`s12.py` 既有 binding 逐项比较 scope+fingerprints_digest，不一致返回 `conflict`（adapter 转 `S16_OWNER_BINDING_CONFLICT`）。测试：S02/S12 binding 冲突分支（`test_s02_absence_persistence_failure_keeps_memory_intact_and_retryable` 扩展 + S12 部分） |
| 6 | S02/S12 restore replay 共享固定 binding | P1-4 | 完成 | `_replay_operation_id(job, owner, scope)` 派生每 scope 每 owner 独立 operation；S01/S02/S12/backup replay 均接收 operation_id/fence；replay fact 全部 owner 验证后才写。测试：`test_single_owner_restore_...` + `test_receipt_append_only_...` |
| 7 | 默认 Security Audit writer 未接通 | P1-5 | 完成 | `s01.py` 公开 `audit_writer` property；`app.py` factory 的 availability = writer 配置且可调用；writer 缺失时 seam unavailable → 受保护命令写前 fail-closed。测试：`test_s16_factory_wires_real_audit_seam_and_fails_closed_without_writer`（HTTP） |
| 8 | preflight 缺 audit gate | P1-6 | 完成 | `preflight()` 在输入/身份校验后、任何 ledger 写入前调用 `_require_security_audit()`。测试：`test_security_audit_facts_and_outage_zero_state_change`（audit outage 下 preflight 零事件零 binding） |
| 9 | Legal Hold release 无独立 request/generation | P1-7 | 完成 | `_hold_generation` 对 impose+release 单调推进；release 生成独立 request_id、generation 并在响应返回；commit 在同一 ledger transaction 比较完整 generation+hold union。测试：`test_hold_release_has_own_request_and_generation` |
| 10 | terminal replay 分支未绑定当前 key | P1-8 | 完成 | approve/cancel/commit 的 terminal 分支在事务内先 `_bind_or_replay` 记录当前 key 再返回 replayed；同 key 异内容 conflict。测试：`test_terminal_approval_replay_binds_current_key` |
| 11 | worker claim 无条件 upsert | P1-9 | 完成 | `s16_jobs` 增加 status/lease_owner/lease_expires_at/fence/attempt 列；`_claim_job_cas` 条件 UPDATE（原 status/lease/fence/attempt）；rowcount=0 放弃 claim；publish CAS 同步列。测试：`test_cross_instance_claim_cas_grants_one_lease` |
| 12 | ledger binding 无唯一约束 | P1-10 | 完成 | 新增 `s16_bindings` 表（binding_key PK）；所有命令在事务内 `_bind_or_replay`（insert-if-absent + read-back）；同键同内容 replay、异内容 conflict，跨进程由唯一约束仲裁。测试：既有幂等测试 + terminal key 测试 |
| 13 | S02/S12 verify 丢弃 scope | P1-11 | 完成 | S02 absence 行携带 scope_fingerprint；`s02_verify_absent(fingerprints, scope)` 校验行归属 scope；S12 verify 校验 binding scope；scope 不匹配→稳定 failure。测试：S02 scope-aware verify 断言 |
| 14 | S01 tombstone 未绑定 op/fence + domain 读取不统一 | P1-12 | 完成 | `s16_tombstone_verified(scope, operation_id, fence)` 支持 binding 校验；`s16_require_read_ready` 注入 `s16_read_gate`（factory 装配），`_reviewer_application_authority`/`audit_timeline`/`workspace_view`/`delivery_view` 统一存在性隐藏；HTTP middleware 保留为外层。测试：`test_s16_restore_readiness_gate_closes_all_restricted_reads`（HTTP + domain） |
| 15 | complete 后 React 仍展示 preflight 数据 | P1-13 | 完成 | `S16GovernedDeletionPanel` 以 complete 为顶层分支，仅渲染 receipt 与摘要（`s16-complete-only`）；ManifestTable/LegalHold/Approval/Commit/Job/输入全部卸载。测试：`unloads every preflight surface after completion` |
| 16 | React 403 只清 query cache | P1-14 | 完成 | governance 403（query/preflight/commit/cancel/repair/process）触发 `identityDenied`：清空 reference/preflight/requestId/cancelled/approvedCount + S16 request/receipt cache + application-scoped cache；页面只渲染授权错误；approver 403 不触发治理清理。测试：`clears local S16 state on a governance 403` |
| 17 | 残余路径无定向断言 | P2-1 | 完成 | 新增 5 个 domain 测试（claim CAS/单 owner 恢复/release generation/terminal key/backup binding）+ 1 个 HTTP 工厂审计测试 + 2 个前端状态测试（见上） |

## 精确验证命令执行记录（唯一执行集合；R2 review 保持只读）

### 集合 1
```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```
- 退出码 0；**38 passed**（controlled 25 + http 10 + t17_app 3）；耗时 23.75s；失败详情：无。

### 集合 2
- 退出码 0；**9 passed**；耗时 17.40s；失败详情：无。

### 集合 3
- `npm run generate:api`：退出码 0。
- `npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx`：退出码 0；**30 passed**（30）；Duration 3.87s。
- `npx playwright test tests/test_t17_react.spec.js --workers=1`：退出码 0；**2 passed**（desktop 1280x800 + mobile 390x844）；21.1s。
- `npm run build`：退出码 0（含 typecheck + check:generated + vite build 541ms；新 hash assets `index-Y5BNPmIj.js` + css 组）。

## 已执行但不在 brief 清单内的命令（如实记录）

- R2 实现过程中运行了若干次 `npm run typecheck`（brief 允许集合不含单独 typecheck；`npm run build` 内部同样执行 typecheck 并最终通过）。仅用于迭代期编译校验，未改变任何产物之外的文件。

## 未执行项

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes 及其他完整门禁：全部未执行。
- 未编辑/删除 `docs/ROUND32_REVIEW_R2.md`、`docs/ROUND32_FIX_BRIEF_R2.md`、R1 review/brief、`docs/ROUND32_PLAN.md` 的 Arch Gate 结论、`GOAL.md`、`STATUS.md`、GitHub issue 状态。
- 未关闭 issue、未推送；等待 ticket32_codex 只读复审。

## 生成物

- `frontend/src/generated/openapi.json` + `api.ts`（重新生成）。
- React production bundle（`task4_consistency/web/static/react/index.html` + 新 hash assets；R1 assets 已删）。
- 测试：`tests/test_s16_controlled.py`（25 用例）、`tests/test_s16_http.py`（10 用例）、`tests/test_t17_react_app.py`（3 用例 + 工厂）、前端 unit（30）、T17 Playwright（2 viewport）。
- 本交付记录本体。

## 语义调整记录（R2）

- `ready()` 语义进一步收紧为“每个 completed job 的每个 owner 均验证 absence”：正常完成（所有 owner absent）→ open；任意 owner 恢复副本 → 关闭；replay fact 仅在真实恢复并全部 owner 验证后追加（R1 的“重启即 verified”行为改为“无恢复不追加”）。
- approve 的 manifest-digest 校验移入事务内（binding 之后）：同 key 异内容优先 conflict，新 key 错 digest 仍 manifest stale。
- S16_CONFIGURED 静态标志区分“未启用”与“已配置但不可用”；前者仅 S16 路由关闭，后者共享门禁 fail-closed。
- backup capture 输入仍为 (handle, digest)（connector 侧），manifest 仅持久化不可反查 identity；owner 内部 registry 保存映射。

## 剩余机构前置项（G4）

- 机构 IdP/KMS 身份；真实 retention/legal-hold authority；对象与备份 connector（含独立恢复域的生产验证）；独立 audit/WORM 与可观测性、恢复演练证据；独立账本备份策略的生产验证；S17 export 保持 disabled。
- 性能声明保持 `SMOKE_ONLY/INSUFFICIENT`。
