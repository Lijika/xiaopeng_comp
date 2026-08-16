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

发布部署运行已安装 wheel 内的 FastAPI 应用：**无 Node、无 `--reload`**。构建与安装：

```bash
# 1) production React build（关闭 sourcemap，写入 task4_consistency/web/static/react/）
npm run build

# 2) PEP 517：生成一个 sdist 与一个 wheel（默认隔离流程，
#    构建依赖按 pyproject.toml [build-system].requires 自动准备）
.venv/bin/python -m build --outdir dist/

# 3) 安装 wheel 到独立安装根（--no-deps：依赖由宿主环境提供）
.venv/bin/pip install --no-deps --target /opt/task4/site dist/task4_consistency-0.1.0-py3-none-any.whl

# 4) 把 operator inputs 复制到安装根：installed 应用按 __file__ 推导 ROOT=安装根，
#    configs/fixtures/data 必须与包同根
cp -a configs fixtures data /opt/task4/site/
mkdir -p /opt/task4/site/var
```

启动（FastAPI-only；`-P` / `PYTHONSAFEPATH=1` 防止当前目录混入包路径，`PYTHONPATH` 指向安装根）：

```bash
cd /opt/task4
export TASK4_S01_STATE_PATH=/opt/task4/site/var/s01.sqlite3
PYTHONSAFEPATH=1 PYTHONPATH=/opt/task4/site \
  .venv/bin/python -P -m uvicorn task4_consistency.web.app:app \
  --host 127.0.0.1 --port 8765
```

发布资格门禁（安装包导入来源、wheel 内容、聚焦 release 测试、真实 uvicorn 健康检查、完整浏览器矩阵）由 `scripts/test_installed_web_release.sh` 执行。

### 5.3 React 路由与 legacy 回退矩阵

| React 路由 | 用途 | React build 缺失/不完整时 |
|------|------|------|
| `/controlled/s01/react` | 审核工作台 | `503 S01_REACT_UNAVAILABLE`；legacy `/controlled/s01` 可用 |
| `/controlled/s02/react` | 集成工作台 | `503 S02_REACT_UNAVAILABLE`；legacy `/controlled/s02` 可用 |
| `/controlled/s05/react` | 例外审批 | `503 S05_REACT_UNAVAILABLE`；legacy `/controlled/s01` 可用 |
| `/controlled/s08/react` | 策略管理 | `503 S08_REACT_UNAVAILABLE`；legacy `/controlled/s01` 可用 |
| `/controlled/s09/react` | 治理工作区 | `503 S09_REACT_UNAVAILABLE`；legacy `/controlled/s01` 可用 |
| `/demo/react` | 根 demo 的 React 壳 | `503 DEMO_REACT_UNAVAILABLE`；legacy `/` 可用 |

- 每个 React shell 与 `/static/react/index.html` 均 `Cache-Control: no-store` + `Pragma: no-cache`；content-hashed assets（`/static/react/assets/*`）为 `Cache-Control: public, max-age=31536000, immutable`；production 构建不产出 sourcemap。
- **503 含义与排查**：React build 缺失/不完整（引用 asset 缺失、外部 URL、query/fragment、路径穿越、重复属性、缺 module entry、类型错配）一律返回稳定 503，body 最小化、不泄露内部路径，legacy 路由独立可访问。排查顺序：检查安装根 `task4_consistency/web/static/react/index.html` 与 `assets/` 是否完整 → 确认 index.html 引用的每个 hash asset 都在 → 重跑 `npm run build` → 重启 uvicorn。
- 浏览器矩阵（S01–S09 流程、retained demo、两个 viewport、键盘/语义/overflow/security）由 Playwright specs 覆盖：`npm run test:e2e`。

### 5.4 静态制品回滚（prior static artifact rollback）

accepted backend facts 由服务端 SQLite authority（`TASK4_S01_STATE_PATH`）与 Governance Ledger 持有；浏览器与静态制品不拥有 lifecycle/decision/policy/audit facts。回滚**只替换静态制品并重启进程**，accepted facts 不受影响：

1. 停止 uvicorn；
2. 备份并替换安装根 `task4_consistency/web/static/react/` 为先前的完整 build（整目录替换，index.html 与 assets 必须一致）；
3. 以相同环境（同一 `TASK4_S01_STATE_PATH`）重启 uvicorn；
4. 验证恢复：React 路由与 hash assets 返回 200；用注册身份查询受控 API（如 S01 `GET /controlled/s01/api/queries/applications/{application_id}/history` 与 `current-route`、S08/S09 governance workspace），已接受的 fact、revision 与 history 与回滚前一致；`TASK4_AUDIT_LOG` 无异常变更。

### 5.5 鉴权与环境变量

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

**安全提示：** 无 `TASK4_WEB_TOKEN` 时服务为**开放 demo**，仅内网/本机；生产务必设置 token、HTTPS、限制监听地址。规则保存失败会回滚；损坏的 `runtime_rules.yaml` 会被隔离为 `*.yaml.bad` 并回退默认包。

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
