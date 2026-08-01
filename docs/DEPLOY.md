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

```bash
bash scripts/run_web.sh
# 等价：
# .venv/bin/python -m uvicorn task4_consistency.web.app:app --host 127.0.0.1 --port 8765
```

| 变量 | 默认 | 说明 |
|------|------|------|
| `HOST` | `127.0.0.1` | 监听地址；对外可 `0.0.0.0` |
| `PORT` | `8765` | 端口 |
| `TASK4_WEB_TOKEN` | *(空)* | **可选鉴权**。未设置 → 开放 demo；设置后除 `/`、`/api/health`、`/static/*` 外 API 需 `Authorization: Bearer <token>` 或 `X-Task4-Token: <token>` |
| `TASK4_AUDIT_LOG` | `out/audit.log` | 审计 JSONL 路径（规则保存 / KB 变更 / 鉴权拒绝 / runtime 自愈） |

浏览器：

- 首页 `http://127.0.0.1:8765/` — 校验演示 / 批量·evaluate(suite) / 规则维护 / 知识库
- 健康检查 `GET /api/health`（返回 `auth_required` 是否启用 token）

**批量评估：** 全量带标签指标请用 **CLI** `evaluate --suite main`（或 `bash scripts/ci_gate.sh`）。Web 仅提供 `GET /api/evaluate/summary` 与 `POST /api/check/batch`（check 上限 **50** 笔、同步、无 job 队列；**无** `/api/evaluate/batch`）。反向代理超时建议 ≥30s。

**带鉴权启动示例：**

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
