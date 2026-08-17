# 部署手册 — task4_consistency

## 1. 环境要求

| 项 | 要求 |
|----|------|
| OS | Linux / WSL2 / macOS |
| Python | ≥ 3.10 |
| 内存 | 建议 ≥ 512MB（CLI）；Web 演示 ≥ 1GB |
| 端口 | Web 默认 `8765`（可改） |

**不需要 GPU。**

## 2. 安装

```bash
cd /home/lhjysyx/xiaopeng_comp   # 或你的克隆路径
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e ".[dev,web]"
# 或
pip install -r requirements.txt
```

验证：

```bash
.venv/bin/python -c "import task4_consistency, fastapi; print(task4_consistency.__version__ if hasattr(task4_consistency,'__version__') else 'ok')"
.venv/bin/pytest -q
```

## 3. 配置路径

| 路径 | 用途 |
|------|------|
| `configs/rules_auto_lease.yaml` | **默认规则包**（版本化，勿在生产热改后不备份） |
| `configs/runtime_rules.yaml` | Web UI 保存的运行时规则（优先于默认包） |
| `configs/rules_auto_lease.yaml.bak` | 首次写 runtime 时可能生成的备份 |
| `configs/kb/entity_kb.json` | 实体知识库（地址/机构/车牌别名） |
| `fixtures/applications/` | 评估与演示样例 |
| `fixtures/labels/expected_verdicts.json` | 评估标签索引 |
| `out/` | 运行产物（metrics、报告、bench） |
| `out/audit.log` | Web/CLI 可变操作审计 JSONL（可用 `TASK4_AUDIT_LOG` 改路径） |
| `configs/runtime_rules.yaml.bad` | 损坏 runtime 被自愈隔离后的残骸 |

### 3.1 备份建议

```bash
cp configs/rules_auto_lease.yaml "configs/backup/rules_$(date +%Y%m%d).yaml"
cp configs/kb/entity_kb.json "configs/backup/kb_$(date +%Y%m%d).json"
# 若存在 runtime：
test -f configs/runtime_rules.yaml && cp configs/runtime_rules.yaml "configs/backup/runtime_$(date +%Y%m%d).yaml"
```

恢复默认规则（丢弃 Web 改动）：

```bash
rm -f configs/runtime_rules.yaml
# 或 Web UI「恢复默认包」/ POST /api/rules/reset
```

## 4. CLI 用法

```bash
# 单申请校验
.venv/bin/python -m task4_consistency.cli check \
  fixtures/applications/app_consistent_01.json \
  -c configs/rules_auto_lease.yaml \
  -o out/report.json --html out/report.html --markdown out/report.md

# 强制 VIN ISO 校验位（生产可选）
.venv/bin/python -m task4_consistency.cli check ... -c configs/rules_auto_lease.yaml --strict-vin

# 批量评估（写 metrics.json + 旁路 metrics.html）
.venv/bin/python -m task4_consistency.cli evaluate \
  fixtures/applications \
  -c configs/rules_auto_lease.yaml \
  -o out/metrics.json

# 对抗探针
.venv/bin/python scripts/attack_probes.py

# 性能基线
.venv/bin/python scripts/bench.py

# 精确串基线对比
.venv/bin/python scripts/baseline_exact_only.py -o out/baseline_compare.json
```

## 5. Web 服务

### 5.1 源码检出（demo / 开发）

```bash
bash scripts/run_web.sh
```

`scripts/run_web.sh` 是**源码检出 demo 命令**：始终以 `--reload` 启动 uvicorn（开发热重载），不用于发布部署。等价命令：

```bash
.venv/bin/python -m uvicorn task4_consistency.web.app:app --host 127.0.0.1 --port 8765 --reload
```

### 5.2 已安装发布版（production）

发布部署运行已安装 wheel 内的 FastAPI 应用：**无 Node、无 `--reload`**。前置条件：`/opt/task4` 必须是**完整源码检出**（含 `package.json`、`pyproject.toml`、`task4_consistency/`、`configs/`、`fixtures/`、`data/`）。release 解释器在 `cd` 之前定义并供给一次，构建 / 安装 / 验证 / 启动全程使用同一绝对路径：

```bash
# 1) 供给 release 解释器（一次性）：在 cd 之前完成；
#    RELEASE_PY 为绝对路径，之后不再依赖当前目录
mkdir -p /opt/task4
python3 -m venv /opt/task4/.venv
export RELEASE_PY=/opt/task4/.venv/bin/python
"$RELEASE_PY" -m pip install build

cd /opt/task4
# 2) production React build（关闭 sourcemap，写入 task4_consistency/web/static/react/）；
#    先按 package-lock.json 精确重建依赖。npm run build 内的 check:generated
#    用 .venv/bin/python 重新导出 OpenAPI（import task4_consistency.web.app），
#    构建期因此需要源码可导入 + Web 依赖（editable 安装）；运行期验证仍以
#    /opt/task4/site 优先（PYTHONPATH），不导入检出包
npm ci
"$RELEASE_PY" -m pip install -e ".[web]"
npm run build

# 3) PEP 517：生成一个 sdist 与一个 wheel（默认隔离流程，
#    构建依赖按 pyproject.toml [build-system].requires 自动准备）
"$RELEASE_PY" -m build --outdir dist/

# 4) 安装 wheel 与其 Web 运行时依赖到独立安装根：extras 语法使
#    fastapi / uvicorn[standard] / python-multipart / httpx 与基础
#    PyYAML 一并落入 /opt/task4/site，import 验证时不导入检出包
"$RELEASE_PY" -m pip install --target /opt/task4/site \
  "task4_consistency[web] @ file:///opt/task4/dist/task4_consistency-0.1.0-py3-none-any.whl"

# 5) 把 operator inputs 复制到安装根：installed 应用按 __file__ 推导 ROOT=安装根，
#    configs/fixtures/data 必须与包同根
cp -a configs fixtures data /opt/task4/site/
mkdir -p /opt/task4/site/var

# 6) 验证（copy-paste）：imports 必须从 /opt/task4/site 解析，
#    且包含 task4_consistency / fastapi / uvicorn / yaml
PYTHONSAFEPATH=1 PYTHONPATH=/opt/task4/site \
  "$RELEASE_PY" -c "import task4_consistency, fastapi, uvicorn, yaml; print('imports OK:', task4_consistency.__file__)"
```

启动（FastAPI-only；`-P` / `PYTHONSAFEPATH=1` 防止当前目录混入包路径，`PYTHONPATH` 指向安装根）：

```bash
cd /opt/task4
export RELEASE_PY=/opt/task4/.venv/bin/python
export TASK4_S01_STATE_PATH=/opt/task4/site/var/s01.sqlite3
PYTHONSAFEPATH=1 PYTHONPATH=/opt/task4/site \
  "$RELEASE_PY" -P -m uvicorn task4_consistency.web.app:app \
  --host 127.0.0.1 --port 8765
# 健康检查（另开终端）：
curl -s http://127.0.0.1:8765/api/health
```

`RELEASE_PY` 指向 `.venv/bin/python` 绝对路径，与 release harness 的 qualified interpreter 契约一致（`$ROOT/.venv/bin/python`）。发布资格门禁（安装包导入来源、wheel 内容、聚焦 release 测试、真实 uvicorn 健康检查、完整浏览器矩阵）由 `scripts/test_installed_web_release.sh` 执行。

### 5.3 React 路由与 canonical 切流矩阵（Issue #54）

当前制品（#54 之后）在 canonical 路由直接服务已认证 React build；legacy 模板/静态/变更接口仍打包在 wheel 内，仅作为**部署回滚路径**存在（#45 负责物理删除）。

| 路由 | 用途 | React build 缺失/不完整时 |
|------|------|------|
| `/` | canonical 根（React demo 壳，别名 `/demo/react`） | `503 DEMO_REACT_UNAVAILABLE` |
| `/controlled/s01` | canonical 审核工作台（别名 `/controlled/s01/react`） | `503 S01_REACT_UNAVAILABLE` |
| `/controlled/s02` | canonical 集成工作台（别名 `/controlled/s02/react`） | `503 S02_REACT_UNAVAILABLE` |
| `/controlled/s05/react` | 例外审批 | `503 S05_REACT_UNAVAILABLE` |
| `/controlled/s08/react` | 策略管理 | `503 S08_REACT_UNAVAILABLE` |
| `/controlled/s09/react` | 治理工作区 | `503 S09_REACT_UNAVAILABLE` |

- 每个 React shell 与 `/static/react/index.html` 均 `Cache-Control: no-store` + `Pragma: no-cache`；content-hashed assets（`/static/react/assets/*`）为 `Cache-Control: public, max-age=31536000, immutable`；production 构建不产出 sourcemap。
- **503 含义与排查**：React build 缺失/不完整（引用 asset 缺失、外部 URL、query/fragment、路径穿越、重复属性、缺 module entry、类型错配）一律返回稳定 503，body 最小化、不泄露内部路径，canonical 路由与别名同闭。排查顺序：检查安装根 `task4_consistency/web/static/react/index.html` 与 `assets/` 是否完整 → 确认 index.html 引用的每个 hash asset 都在 → 重跑 `npm run build` → 重启 uvicorn。
- **Legacy 表面契约**：`task4_consistency/web/legacy_catalog.py` 是唯一权威目录（10 个稳定条目：root/S01/S02 页面、`/static/app.js`、`/static/style.css`、`PUT /api/rules`、`POST /api/rules/reset`、`POST /api/kb`、`DELETE /api/kb/{section}/{key}`、`POST /api/kb/reload`）。源扫描与运行时观察都消费该目录；canonical 源边必须为零。
- 浏览器矩阵（S05/S06 + T01/T02/T03/T06/T07/T08 production React 流程、两个 viewport、键盘/语义/overflow/security）由 Playwright specs 覆盖：`npm run test:e2e`。受控窗口内该矩阵作为 `operator-simulated` 队列运行，legacy 模板控制台浏览器 spec（S01/S02/S03/S07）已在 #54 按等接口证据退役。

### 5.4 部署回滚（deployment-only rollback，Issue #54）

回滚是**制品级**的：停止当前 wheel 进程 → 启动**上一合格 wheel**（同一 SQLite authority `TASK4_S01_STATE_PATH`）→ 验证 legacy 所有者（`/`、`/controlled/s01`、`/controlled/s02` 由 prior 制品解析为 legacy 模板）与 accepted facts 不变 → 停止 → 重启当前 wheel（canonical React 恢复）。浏览器与静态制品不拥有 lifecycle/decision/policy/audit facts；回滚只替换可执行制品与进程身份。发布资格门禁内的一次 current → prior → current 演练由 `scripts/test_installed_web_release.sh` 执行（prior wheel 从 fixed base `git archive` 构建，字节不变）。

### 5.5 观察遥测（Issue #54）

应用入口观察模块（`task4_consistency/web/observation.py`）在 canonical FastAPI 适配器内消费 legacy 目录并输出确定性证据记录；它不是 security-audit、Lifecycle 或 Policy Governance 所有者。**不设置任何观察变量时完全无捕获**（零磁盘、零行为差异）。

| 变量 | 说明 |
|------|------|
| `TASK4_OBS_LOG_DIR` | 观察日志目录（`requests.jsonl`、`process-lifecycle.jsonl`、`sequence` sidecar） |
| `TASK4_OBS_WINDOW_ID` | 受控窗口标识（同一窗口所有进程共享） |
| `TASK4_OBS_ARTIFACT_SHA256` | 被观察制品（当前 wheel）SHA256 |
| `TASK4_OBS_ARTIFACT_STAGE` | 制品阶段 `current` 或 `prior`；运行时所有权由该阶段和已解析 route owner 决定 |
| `TASK4_OBS_PROCESS_CLASS` | `operator-simulated` / `release` / `health` / `playwright-probe` / `rollback-probe`；缺失或非法 → `unknown` 并使窗口无效 |
| `TASK4_OBS_PROCESS_ID` | 进程身份（默认 `os.getpid()`） |

观察 bundle 使用 schema v2。`requests.jsonl` 保持固定十二字段，`process-lifecycle.jsonl` 记录每个进程的 start/end；`window-manifest.json` 以 `process_artifacts` 保存每个进程的 artifact SHA、`current`/`prior` stage 和 traffic class。prior stage 必须同时提供 prior wheel identity、reviewed commit、clean tracked-tree、accepted-fact digest、Node/npm/package identity、loopback route table、冻结 Playwright node/spec digest、两个 viewport 以及窗口起止时间和 elapsed seconds。验证器独立校验固定五类流量词汇、十个 catalog ID、route family、每进程制品身份、原始日志 digest 和 release evidence contract。

证据验证：

```bash
.venv/bin/python -m task4_consistency.web.observation verify --manifest <window-manifest.json>
```

有效窗口 = 非空 operator 分母、连续序列、精确摘要、干净进程生命周期、零 unknown 类别、零 canonical 源边、零 operator-simulated legacy 命中；`rollback-probe` 的 legacy 命中单独计入 manifest。窗口 raw 日志与其 SHA256 一并封存。

### 5.6 鉴权与环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `HOST` | `127.0.0.1` | 监听地址；对外可 `0.0.0.0` |
| `PORT` | `8765` | 端口 |
| `TASK4_WEB_TOKEN` | *(空)* | **仅限 open demo 的 API**。未设置 → 开放 demo；设置后 `/`、`/api/health`、`/static/*` 保持开放，其余 open demo API 需 `Authorization: Bearer <token>` 或 `X-Task4-Token: <token>`；`/controlled/s01|s02|s05|s08|s09/*` 不受其影响，继续使用各自注册身份 |
| `TASK4_AUDIT_LOG` | `out/audit.log`（源码检出 / 安装根下） | 审计 JSONL 路径（规则保存 / KB 变更 / 鉴权拒绝 / runtime 自愈） |

浏览器：

- 首页 `http://127.0.0.1:8765/` — 校验演示 / 批量·evaluate(suite) / 规则维护 / 知识库
- 健康检查 `GET /api/health`（返回 `auth_required` 是否启用 token）

**批量评估：** 全量带标签指标请用 **CLI** `evaluate --suite main`（或 `bash scripts/ci_gate.sh`）。Web 仅提供 `GET /api/evaluate/summary` 与 `POST /api/check/batch`（check 上限 **50** 笔、同步、无 job 队列；**无** `/api/evaluate/batch`）。反向代理超时建议 ≥30s。

**带鉴权启动示例（源码检出 demo）：**

```bash
export TASK4_WEB_TOKEN='change-me'
bash scripts/run_web.sh
# 调用 API：
curl -s -H "Authorization: Bearer change-me" http://127.0.0.1:8765/api/fixtures
```

**审计日志：**

- 默认追加写入 `out/audit.log`（JSONL，每行一事件）
- 事件含：`rules_save` / `rules_reset` / `kb_add` / `kb_delete` / `auth_denied` / `rules_auto_heal`
- 生产建议轮转该文件并限制目录权限

**安全提示：** 无 `TASK4_WEB_TOKEN` 时服务为**开放 demo**，仅内网/本机；`TASK4_WEB_TOKEN` 仅控制 open demo API。生产发布请使用受控路由（`/controlled/s01|s02|s05|s08|s09/*`），其访问由各场景注册身份与受控部署前置保障，不受 `TASK4_WEB_TOKEN` 影响；同时启用 HTTPS、限制监听地址。规则保存失败会回滚；损坏的 `runtime_rules.yaml` 会被隔离为 `*.yaml.bad` 并回退默认包。

## 6. 健康检查清单

1. `pytest -q` 全绿  
2. `evaluate` 输出 `THRESHOLD PASS`  
3. `attack_probes.py` `release_open=0`  
4. Web 能加载 fixture 并完成一次校验  
5. 规则保存后 `configs/runtime_rules.yaml` 存在且再次校验生效  

## 7. 卸载 / 清理

```bash
deactivate
rm -rf .venv out/* configs/runtime_rules.yaml
# 源码与 configs 默认包保留
```
