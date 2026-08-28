# ROUND32 交付记录 — Ticket #32 / S16 合规删除

日期：2026-08-28。实现会话：`ticket32_omp`（本会话）。协作边界：仅 `ticket32_codex` 与本 OMP 会话；无任何 scout/subagent 派发。

## 固定基线与最终 HEAD

- 固定静态分析基线：`e9e9cbb27326b99b38edc81e4da3d1a793db7a43`（实现前 `git rev-parse HEAD` 确认一致）。
- 最终 HEAD：`17c49effb5cc245aca6b3b08ae102b757eaff6d4`（`feat(s16): governed deletion ledger, workers, HTTP and React panel`，直接提交于固定基线之上；28 files changed, 16161 insertions(+), 4337 deletions(-)）。
- 工作树：仅保留 25 项既有未跟踪条目（AGENTS.md、docs/、out/、data/、fixtures/ 等），无任何 S16 相关未提交改动。

## 流程调用

- 本会话实际发送并执行了 `/skill:implement`（技能全文作为本任务系统指令载入：计划优先、常规 typecheck、单测试文件渐进验证、结尾完整套件一次、完成后 /code-review、提交当前分支）。
- 未创建、未派发任何 scout / subagent / 并行 worker / 额外会话；全部调研、实现与测试在当前 OMP 会话内完成。

## Arch Gate 选择核对（静态确认，结论 NO 沿用）

1. S16 使用独立删除账本与编排模块 `task4_consistency/controlled/s16.py`，独立 SQLite 路径（`TASK4_S16_STATE_PATH`），与业务备份分离。✅
2. S08 Policy Safety Hold 与 S16 Legal Hold 为并列权威：S16 在法律保全事件中单独保存 scope fingerprint、授权人、理由码、生效时间、期限与 generation；未触碰 S08 表。✅
3. S16 通过窄 owner 接口调用 S01/S02/S12/backup owner；编排器不直接写其他 owner 的表：S01 经 `s16_resolve_application`/`s16_inventory`/`s16_apply_deletion` 等公开 seam，S02 经 boundary 的 `s02_inventory`/`s02_delete`/`s02_replay`，S12 经 `s16_enumerate_scope`/`s16_delete_scope`。✅
4. S01 `governed_delete()` 继续服务公开演示 24h 清理（两个既有测试原样通过），并为 S16 增加可选的 value-free tombstone 参数（同一事务写入 `s16_governed_deletions`）；S16 使用独立计划构造器 `s16_build_deletion_plan`，保留 Lifecycle 终止历史与最小化系统 Audit（`controlled_cohort_stop`/`runtime_recovery`）。✅
5. 共享评估包/共享对象采用稳定阻断 `S16_SHARED_COPY_REQUIRES_REPACK`：S12 计划引用多申请、S02 对象被其他申请引用、S01 会话 scope 被其他申请共享均触发。✅
6. 恢复清单仅保存 owner、类别、内容摘要与不可反查 identity fingerprint；S16 ledger 事件/receipt 不保存 application id、object ref、文件路径、原值或凭据（提交后由测试 12 验证）。✅
7. `/controlled/s16` 使用独立数据治理身份（`TASK4_S16_GOVERNANCE_*`）、两名提前删除审批身份（`TASK4_S16_APPROVER1_*`/`TASK4_S16_APPROVER2_*`）与系统 worker 身份；平台管理员/Reviewer/S08 Admin/S15 reveal 凭据均无 S16 删除权限（身份互斥配置门）。✅

## 实现范围（相对基线的完整 diff 路径）

生产文件：
- `task4_consistency/controlled/s16.py`（新增）
- `task4_consistency/controlled/s01_store.py`
- `task4_consistency/controlled/s01.py`
- `task4_consistency/controlled/s02.py`
- `task4_consistency/controlled/s12.py`
- `task4_consistency/web/s16_http.py`（新增）
- `task4_consistency/web/app.py`
- `frontend/src/App.tsx`
- `frontend/src/api/client.ts`
- `frontend/src/api/hooks.ts`
- `frontend/src/components/S16GovernedDeletionPanel.tsx`（新增）
- `frontend/src/generated/openapi.json`、`frontend/src/generated/api.ts`（重新生成）
- `task4_consistency/web/static/react/index.html`、`assets/index-DD3Vi1eW.js`、`assets/index-CgJmzXTM.css`（构建组；旧 hash asset 已删除）
- `README.md`、`docs/DEPLOY.md`
- `playwright.config.js`（testMatch 增加 `test_t17_react.spec.js`）

测试文件：
- `tests/test_s16_controlled.py`（12 用例）
- `tests/test_s16_http.py`（6 用例）
- `tests/test_t17_react_app.py`（3 用例 + `create_t17_react_test_app` 工厂）
- `tests/test_t17_react.spec.js`（desktop + mobile 两个 viewport）
- `frontend/src/components/S16GovernedDeletionPanel.test.tsx`、`frontend/src/api/hooks.s16.test.tsx`

（diff 路径已在上方完整列出。）

## 临时诊断记录（非计划验证集合，仅供 Codex 只读复核）

实现早期用 `/tmp/s16_smoke.py`（仓库外临时脚本，不入库）做端到端冒烟，暴露并按序修复以下问题：

1. `ModuleNotFoundError: No module named 'tests'` — 脚本直接运行未携带仓库根 PYTHONPATH；`PYTHONPATH=/home/lhjysyx/xiaopeng_comp` 后解决。**首次失败原因：运行环境缺 PYTHONPATH，非代码缺陷。**
2. `FileNotFoundError: baseline release not found: /configs/rules_auto_lease.yaml` — 脚本位于 /tmp，`Path(__file__).parents[1]` 解析到 `/`；将 ROOT 固定为仓库绝对路径后解决。**失败原因：脚本自定位错误，非代码缺陷。**
3. `S16NotFound: app_r53_bad_engine` — application reference 使用场景文件名，实际 upstream reference 是 fixture 内 `application_id`（`APP-R53-BAD-ENGINE`）。**失败原因：测试输入错误；解析行为符合预期（existence-hiding）。**
4. `sqlite3.IntegrityError: UNIQUE constraint failed: s16_receipts.receipt_id` — 重启 restore replay 对同一 receipt 重新封存 replay 状态时 `INSERT` 冲突；修复为 `INSERT OR REPLACE`（receipt 按 id 重封存 replay 事实）。**唯一代码缺陷，已修复。**

修复后 `/tmp/s16_smoke.py` 全流程通过：terminated 申请 → preflight（9 类 entries）→ 双审批 → commit → worker complete → receipt（owner_counts={'s01': 4}）→ 重启 replay verified → ready → 删除后 S01 查询 QueryNotFound。

## 精确验证命令执行记录

（按 docs/ROUND32_PLAN.md 仅执行以下集合；禁止项目完整 pytest / 完整 Playwright / ci_gate / test_installed_web_release / evaluate / attack_probes。）

### 集合 1（S16 领域 + HTTP + React app Python 侧）
```bash
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
```
- 退出码：0
- 测试数：21（12 + 6 + 3）
- 耗时：15.58s（最终执行）
- 失败详情：无（最终执行；实现中途失败均为新测试自身断言/夹具问题，逐一修复，见下）

### 集合 2（受影响的既有测试回归）
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
- 首次执行（实现中段回归检查）：`9 passed in 18.23s`，退出码 0（记录于 2026-08-28）。
- 最终执行：`9 passed in 17.59s`，退出码 0。失败详情：无。

### 集合 3（前端）
```bash
npm run generate:api
npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx
npx playwright test tests/test_t17_react.spec.js --workers=1
npm run build
```
- `npm run generate:api`：退出码 0；openapi-typescript 成功重新生成 `frontend/src/generated/openapi.json` + `api.ts`。
- `npm run test:unit -- ...`：退出码 0；23 passed（23），Duration 3.29s。
- `npx playwright test tests/test_t17_react.spec.js --workers=1`：退出码 0；2 passed（desktop 1280x800 + mobile 390x844），18.3s。
- `npm run build`：退出码 0（typecheck + check:generated + vite build 529ms）；产物 `index-DD3Vi1eW.js` + `index-CgJmzXTM.css`。

## 未执行项

- 项目完整 pytest、完整 Playwright、`scripts/ci_gate.sh`、`scripts/test_installed_web_release.sh`、evaluate、attack_probes 及其他完整门禁：按 ticket 约束全部禁止，未执行。
- 未编辑/删除 `GOAL.md`、`STATUS.md`、GitHub issue 状态、`docs/ROUND32_PLAN.md` 的 Arch Gate 结论；工作树中其他既有未跟踪文件（含 `docs/ROUND32_PLAN.md`、`AGENTS.md`、`out/`、`data/` 等）保持未提交。
- 未创建任何未来占位模块（云厂商/KMS/broker/S17）；`export_or_temp` 由 `ExportTempOwner` 返回带证明的零条目。

## 生成物清单

- `frontend/src/generated/openapi.json`、`frontend/src/generated/api.ts`（重新生成，含全部 S16 路径与 DTO schema）
- React production bundle：`task4_consistency/web/static/react/index.html` + `assets/index-DD3Vi1eW.js` + `assets/index-CgJmzXTM.css`（旧 hash asset `index-BJTNHRZg.css`/`index-DwR5zzmb.js` 已删除；index.html 引用同步）
- 测试生成物：`test-results/`（playwright 通过，无失败 artifact 保留）
- 本交付记录本体

## 已知失败与修复记录（实现中）

1. `/tmp/s16_smoke.py` 诊断（临时脚本，不入库）四轮问题已在文首记录；唯一代码缺陷（receipt 重封存 UNIQUE 冲突）已修复。
2. 新测试自身问题逐一修复：receipts 内存值为 AdmissionResult dataclass（两处过滤改为 getattr）；idempotency 绑定按结果内容提取；S02 引用摘要收集缺失 key（source_sha256/sha256）与 dataclass 遍历；S12 row digest 用 `content_digest`；S12 scope 指纹改为 s16 形式；backup 文件比对用原始 sha256；preflight 幂等重放需重建完整响应；S14 cancel 授权扩展（registered 申请由 admission-bound 上游身份取消，使 L14 对 S02 路径可达）；fault injector 移入 owner try 块内；max_owner_attempts 参数补入构造器。
3. **Playwright 首轮超时（30s/90s）与 403**：根因有二。(a) spec 内 `reservePort` 在 listen 回调外同步调用 `server.close()` 抛 `ERR_SERVER_NOT_RUNNING`，server 未启动即超时——按既有 spec 模式修复（close 在回调内）。(b) 浏览器 context 级 `extraHTTPHeaders`（governance 凭据）覆盖了 panel 请求头中的 approver 凭据，approve 收到 403——生产修复：approve 请求改用专用 `X-S16-Approver-Token` 请求头，服务端 `_s16_approver_principal` 优先读取该头（回退 Authorization），前端 bundle 重建后 403 消除；未放宽任何断言。另修复 mobile 390px 清单表横向溢出（`overflow-x-auto` 容器）。
4. 诊断用临时日志（server.log 写入、`--log-level info`、`S16DBG` print、/tmp 临时目录）已全部清理；`reservePort` 修复与 `playwright.config.js` testMatch 增项保留。

## 最终状态与机构前置项

- 全部三组精确验证通过（见上）；无未决失败。
- 实现中架构决策（记录于 Arch Gate 核对节）：S14 cancel 授权对 registered 申请扩展 admission-bound 上游身份；`X-S16-Approver-Token` 专用头；S01 侧持久 `s16_governed_deletions` tombstone 与 S16 账本同域仲裁。
- 机构 G4 前置项（本 ticket 不交付，文档中声明）：
  - 机构 IdP/KMS 身份；真实 retention/legal-hold authority；
  - 对象与备份 connector（当前为 SQLite/本地 owner 与临时 backup owner，仅提供可执行合同证据）；
  - 独立 audit/WORM、可观测性与恢复演练证据；
  - 独立删除账本备份策略与 restore replay 顺序的生产部署验证；
  - S17 export 保持 disabled。
- 性能声明保持 `SMOKE_ONLY/INSUFFICIENT`；S16 不产生性能结论。

## 架构冲突记录

暂无。若实现或测试中遇到架构冲突，将证据与建议追加到本节并暂停等待 Codex 只读复核。
