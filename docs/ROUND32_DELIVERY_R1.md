# ROUND32 R1 交付记录 — Ticket #32 / S16 修复 loop

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex` 与本 OMP 会话；无任何 scout/subagent/额外会话。本轮 brief 唯一来源：`docs/ROUND32_REVIEW_R1.md` + `docs/ROUND32_FIX_BRIEF_R1.md`（Codex 生成，未编辑）。

## 固定基线与最终 HEAD

- 固定基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（与 R1 review 一致）。
- R1 起始 HEAD（R0 交付）：`6fd13fb6aacb52b9f6341e3d0c9f0972ceec83f7`。
- R1 最终 HEAD：`3f5302f7e70bcc76fa90e030f77aa10b80b3b071`（`fix(s16): R1 review — readiness gate, owner fencing, audit seam, holds`，19 files changed；R0 交付 `6fd13fb` 之上）。

## Review finding 修复状态（逐项）

| Finding | 级别 | 状态 | 修复位置 |
|---|---|---|---|
| 恢复重放未成为所有受限查询的服务门禁 | P0 | 完成 | `S16RestoreReadinessGate` 中间件（app.py）+ `_s16_service` ready() 检查；`ready()` 语义改为“任一已完成 scope 在业务 owner 重新可见即关闭”，运行期 `replay_restore_if_needed()` 由 S01 后台循环调用；测试 `test_s16_restore_readiness_gate_closes_all_restricted_reads`（HTTP）+ `test_runtime_restore_replay_reopens_readiness` |
| Backup owner 删除失败仍报 complete / 越界删除 | P0 | 完成 | `_validate_capture_handle`（拒绝绝对路径/分隔符/`..`/`.`/隐藏文件）；`_capture_target` root 边界校验；delete 逐文件摘要校验→unlink→存在性验证→最后删 manifest，任一失败保留 manifest 并抛 `S16OwnerFailure`；verify 基于 manifest+文件；测试 `test_backup_capture_path_boundary_and_delete_verification` |
| Commit 未重核对 registry/policy/hold generation | P1 | 完成 | preflight 事件固化 `owner_registry_digest`/`policy_id`/`policy_version`/`policy_digest`/`hold_generation`；commit 在 ledger 事务内经 `_ledger_facts_in_transaction` 重读事实并逐项比较（`S16_OWNER_REGISTRY_STALE`/`S16_POLICY_STALE`/`S16_HOLD_GENERATION_CHANGED`）；测试 `test_commit_rejects_registry_policy_and_hold_generation_changes` |
| Worker publish 无 lease/fence CAS | P1 | 完成 | `_cas_publish_job`（job_id + lease_owner + fence + attempt 条件更新）；CAS 失败只追加 `stale_worker` 事件，不改 job/receipt；测试 `test_stale_worker_publish_cas_never_overwrites_newer_state` |
| S02 absence 持久化顺序 | P1 | 完成 | `_absence_transaction`：单 SQLite 事务先持久化 absence+binding，成功后才改内存；异常保留内存并可重试；`s02_verify_absent` 改读持久 store；测试 `test_s02_absence_persistence_failure_keeps_memory_intact_and_retryable` |
| Domain 授权只验证 subject | P1 | 完成 | `_principal_identity` 校验 subject+role+scope+source+expiry；`_require_governance`/`_require_approver` 全字段绑定；幂等绑定指纹纳入 role/scope/source；构造器拒绝 worker 与治理/审批主体别名 |
| S16 错误响应无 no-store | P1 | 完成 | `S16NoStorePolicy` 中间件覆盖全部 `/controlled/s16` 响应（成功/domain 错误/422/503）+ 既有 `_S16_NO_STORE_HEADERS`；测试 `test_s16_every_domain_and_validation_error_carries_no_store` |
| 独立 Security Audit owner seam | P1 | 完成 | `security_audit_available` + `security_audit_writer` 注入；受保护命令同事务写 value-free `security_audit` fact；提交后完整复制（`security_audit_replication` status replicated/failed/not_configured）；seam 不可用→零状态变化；测试 `test_security_audit_facts_and_outage_zero_state_change` |
| Legal Hold 缺少命令契约/HTTP/UI | P1 | 完成 | 封闭 reason 词表（litigation/regulatory/internal_investigation）+ 封闭 owner 词表；request id + 幂等绑定 + scope 存在性门；HTTP `POST /legal-holds/impose` + `/{hold_id}/release`；hooks `useS16ImposeHold`/`useS16ReleaseHold`；panel `LegalHoldSection`；测试 `test_s16_legal_hold_http_command_surface` + 前端 unit |
| `s16_receipts` INSERT OR REPLACE 改写 | P1 | 完成 | `_append_receipt` 恢复纯 INSERT；新增 append-only `s16_replays` 表；readiness/`restore_replay_status` 从不可变 replay facts 派生；篡改 receipt 触发完整性失败；测试 `test_receipt_append_only_and_replay_facts_immutable` |
| owner 丢弃 operation/fence | P1 | 完成 | S02 `s02_deletion_bindings` + S12 `s12_deletion_bindings`（operation_id+fence PK，同事务）；同 binding 重放原结果、低 fence 稳定 `S16_OWNER_STALE_FENCE`；scope+fingerprints 同校验；测试 `test_owner_level_fencing_rejects_stale_fence_on_s12` + S02 部分 |
| Backup capture 秒级 manifest 覆盖 | P2 | 完成 | manifest_id 追加随机唯一后缀；重复 identity 拒绝覆盖；测试见 backup 测试（两次 capture 均保留） |
| Backup 恢复域未分离 | P2 | 完成 | `TASK4_S16_BACKUP_ROOT` 改为**必填**；启动校验与 S16 账本/业务库/父恢复根不重合不嵌套；`docs/DEPLOY.md` 更新 |
| 编排器穿透 `._service` | P2 | 完成 | `DeletionOwner` Protocol 新增 value-free 事实方法（resolve/scope_exists/is_terminated/terminated_at/store_revision/owner_healthy/retained_scan/referenced_object_digests/all_scope_fingerprints）；编排器所有 `self._owners[...]._service` 穿透移除；S02 owner 改为接收 S01 owner |
| 关键失败场景无回归断言 | P2 | 完成 | 上述 10 个新 domain 测试 + 3 个新 HTTP 测试 + 2 个新前端 unit 测试（见测试清单） |

## 精确验证命令执行记录（唯一执行集合）

### 集合 1
```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```
- 退出码 0；**32 passed**（controlled 20 + http 9 + t17_app 3）；耗时 21.05s；失败详情：无（最终执行；中途修复见 R0 记录 + 本轮新断言调整：hold 词表、scope 绑定、ready 语义、backup 测试夹具）。

### 集合 2
- 退出码 0；**9 passed**；耗时 17.88s；失败详情：无。

### 集合 3
- `npm run generate:api`：退出码 0（openapi-typescript 重新生成，含 legal-hold 路由与 `S16CommandResponse.generation`）。
- `npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx`：退出码 0；**28 passed**（28）；Duration 4.38s。
- `npx playwright test tests/test_t17_react.spec.js --workers=1`：退出码 0；**2 passed**（desktop 1280x800 + mobile 390x844）；18.5s。
- `npm run build`：退出码 0（typecheck + check:generated + vite build 616ms）。

## 未执行项

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes 及其他完整门禁：全部未执行（ticket 约束）。
- 未编辑/删除 `docs/ROUND32_REVIEW_R1.md`、`docs/ROUND32_FIX_BRIEF_R1.md`、`docs/ROUND32_PLAN.md` 的 Arch Gate 结论、`GOAL.md`、`STATUS.md`、GitHub issue 状态。
- 未关闭 issue、未推送；等待 ticket32_codex 只读复审。
- 工作树其他既有未跟踪文件保持未提交。

## 生成物

- `frontend/src/generated/openapi.json` + `api.ts`（重新生成，含 legal-hold 端点）。
- React production bundle：`task4_consistency/web/static/react/index.html` + 新 hash assets（本轮构建组；旧 R0 assets 已删）。
- `docs/ROUND32_DELIVERY_R1.md` 本体。
- 测试：`tests/test_s16_controlled.py`（20 用例）、`tests/test_s16_http.py`（9 用例）、`tests/test_t17_react_app.py`（3 用例 + 工厂）、`frontend` unit（28）。

## 已知失败与语义调整记录

- R0 遗留：`ready()` 原语义（未验证即关闭）与正常完成后的 receipt 读取冲突；本轮改为“已完成 scope 在业务 owner 重新可见即关闭”，恢复窗口由共享门禁 + 运行期 replay 覆盖（见 finding 1 记录）。
- Legal Hold 词表收紧为封闭集合后，R0 测试的 `LITIGATION_HOLD` 等自由文本改为 `litigation` 等受控值（未放宽断言）。
- hold 变更后 manifest 失效（generation pin）：impose/release 之后必须重新 preflight 才能 commit（符合 R1 要求）。
- Playwright/HTTP 调试均无遗留日志；`S16NoStorePolicy`/`S16RestoreReadinessGate` 注册于中间件类定义之后（修复了一次注册顺序问题）。

## 剩余机构前置项（G4）

- 机构 IdP/KMS 身份；真实 retention/legal-hold authority；对象与备份 connector；独立 audit/WORM 与可观测性、恢复演练证据；独立账本备份策略的生产验证；S17 export 保持 disabled。
- 性能声明保持 `SMOKE_ONLY/INSUFFICIENT`。
