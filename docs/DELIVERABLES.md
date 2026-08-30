# 赛题任务4 交付物索引（Round33 核对 · fixtures≥100）

> 对照 `GOAL.md` §1–§2 与赛题「跨单据一致性校验」交付清单。  
> **路径为仓库内相对路径（均已存在，Round25/33 交叉核对）。**  
> 指标以 `evaluate --suite main` 为准；`field_source=synthetic` 不得宣传为真实 OCR 评估。
---

## 1. 成果交付形式对照（GOAL §1.4）

| 赛题交付物 | 仓库路径 / 入口 | 证据 / 备注 |
|------------|-----------------|-------------|
| **模块源码** | `task4_consistency/` | `normalize/` `match/` `rules/` `kb/` `web/` `adapters/` `evaluate.py` `cli.py` `report.py` · `pyproject.toml` / `setup` 可 `pip install -e ".[dev,web]"` |
| **配置方案** | `configs/rules_auto_lease.yaml` · `configs/kb/entity_kb.json` · [`docs/CONFIG_GUIDE.md`](CONFIG_GUIDE.md) | 规则包 package/version/changelog；热更副本 `configs/runtime_rules.yaml`（可选） |
| **集成演示（含 Web）** | `task4_consistency/web/` · [`scripts/run_web.sh`](../scripts/run_web.sh) · [`scripts/smoke_web.py`](../scripts/smoke_web.py) | 导入/选 fixture → 校验 → 三态+diff；规则 UI；KB CRUD；suite 选择 |
| **完整技术文档** | 见 §3 | 设计 / 接口 / 配置 / 部署 / 评估 / 交付索引 |
| **评估报告** | [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md) · `out/metrics_main.json` | suite=main 官方数字；semi smoke 分栏诚实声明 |

---

## 2. DoD 勾选 ↔ 路径（GOAL §2 D1–D9）

| ID | 要求 | 主路径（存在） |
|----|------|----------------|
| **D1** 标准化与实体链接 | 字段管道 + 模糊 + 可维护 KB | `task4_consistency/normalize/*` · `match/*` · `kb/store.py` · `kb/__init__.py` · `configs/kb/entity_kb.json` · Web `/api/kb*` |
| **D2** 规则框架 | exact / fuzzy / numeric_tolerance / list_contains / conditional_required | `rules/loader.py` · `rules/engine.py` · handler 注册表 · YAML |
| **D3** 业务规则界面 | 界面维护 | Web「规则维护」· `PUT /api/rules` · `rules/critical_guard.py` 指纹 |
| **D4** 三态可解释 | 一致/不一致/存疑 + 快照 + diff | `report.py` · JSON/MD/HTML · Web 三态表 |
| **D5** 指标 | cov≥80% FPR≤5% FNR≤3% 可复现 | `evaluate.py` · `fixtures/applications/` · `python -m task4_consistency evaluate --suite main` · miss_rate 门禁 |
| **D6** Web 集成演示 | 端到端 | `scripts/run_web.sh` · `scripts/smoke_web.py` · `GET /api/health` |
| **D7** 文档包 | README + 接口/部署/配置/评估 + 日志 | 见 §3；状态：[`STATUS.md`](../STATUS.md)（根目录） |
| **D8** 审查与对抗 | 无开放 P0 | `scripts/attack_probes.py` · `scripts/attack_web_kb.py` · [`ATTACK_CASES.md`](ATTACK_CASES.md) · [`REVIEW.md`](REVIEW.md) |
| **D9** 契约 | schema + adapter | [`INTERFACE.md`](INTERFACE.md) · `adapters/step2_page_order.py` · `adapters/external_ocr_import.py` · `fixtures/` · `fixtures/ocr_inbox/` |

---

## 3. 技术文档包（路径核对）

| 文档 | 路径 | 状态 |
|------|------|:----:|
| 总览 / 入口 | [`README.md`](../README.md) | ✓ |
| 设计说明 | [`docs/DESIGN.md`](DESIGN.md) | ✓ |
| 接口与 schema | [`docs/INTERFACE.md`](INTERFACE.md) | ✓ |
| 部署手册 | [`docs/DEPLOY.md`](DEPLOY.md) | ✓ |
| 配置方案 | [`docs/CONFIG_GUIDE.md`](CONFIG_GUIDE.md) | ✓ |
| 评估报告 | [`docs/EVALUATION_REPORT.md`](EVALUATION_REPORT.md) | ✓ |
| 架构辩论 | [`docs/ARCH_DEBATE.md`](ARCH_DEBATE.md) | ✓ |
| 迭代日志 | [`docs/ITERATION_LOG.md`](ITERATION_LOG.md) | ✓ |
| 交付索引 | [`docs/DELIVERABLES.md`](DELIVERABLES.md) | ✓ |
| 编排 | [`docs/ORCHESTRATION.md`](ORCHESTRATION.md) | ✓ |
| 角色约定 | [`AGENTS.md`](../AGENTS.md)（仓库根） | ✓ |
| 状态板 | [`STATUS.md`](../STATUS.md)（仓库根） | ✓ |
| 目标（Manager 独占） | [`GOAL.md`](../GOAL.md) | ✓ 只读（非 dev） |
| 测试计划 | [`docs/TEST_PLAN.md`](TEST_PLAN.md) | ✓ |
| 对抗记录 | [`docs/ATTACK_CASES.md`](ATTACK_CASES.md) · [`ATTACK_RESPONSE.md`](ATTACK_RESPONSE.md) | ✓ |
| 审查 | [`docs/REVIEW.md`](REVIEW.md) | ✓ |

---

## 4. 源码 / 配置 / 数据 / 脚本清单

### 4.1 包与安装

| 项 | 路径 |
|----|------|
| 包根 | `task4_consistency/` |
| 安装元数据 | `pyproject.toml` 或 `setup.cfg` / `requirements.txt`（以仓库实存为准） |
| 测试 | `tests/` |

### 4.2 配置

| 项 | 路径 |
|----|------|
| 默认规则包 | `configs/rules_auto_lease.yaml` |
| 运行时规则 | `configs/runtime_rules.yaml`（Web 保存，可缺） |
| 实体 KB | `configs/kb/entity_kb.json` |

### 4.3 评估与样例

| 项 | 路径 |
|----|------|
| suite=main | `fixtures/applications/*.json`（`meta.field_source=synthetic`） |
| suite=semi | `fixtures/semi/`（external_ocr import） |
| OCR 中间样例 | `fixtures/ocr_inbox/example.json` |
| 标签索引 | `fixtures/labels/expected_verdicts.json`（若存在） |
| step2 布局 | `data/step2/*_page_order.json` |

### 4.4 关键脚本

| 脚本 | 作用 |
|------|------|
| `scripts/ci_gate.sh` | CI 一键门禁 |
| `scripts/smoke_web.py` | TestClient：health + check |
| `scripts/run_web.sh` | 启动 Web |
| `scripts/demo.sh` | CLI 演示报告 |
| `scripts/bench.py` | 微基准 → `out/bench.json` |
| `scripts/import_external_ocr.py` | 离线 OCR JSON 导入 |
| `scripts/attack_probes.py` | 身份/金额等对抗 |
| `scripts/attack_web_kb.py` | Web/KB/W1/W2 对抗 |
| `scripts/baseline_exact_only.py` | 精确串基线对比（若存在） |

---

## 5. 一键验收

```bash
# 全部门禁
bash scripts/ci_gate.sh

# 分项
.venv/bin/pytest -q
.venv/bin/python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json
.venv/bin/python scripts/smoke_web.py
.venv/bin/python scripts/attack_web_kb.py    # open=0 w12_open=0
.venv/bin/python scripts/attack_probes.py    # release_open=0
bash scripts/run_web.sh                      # 人工浏览器
```

**门禁期望：** `=== CI GATE PASS ===`；main `THRESHOLD PASS`；web/kb `open=0`；probes `release_open=0`。

---

## 6. 边界（诚实声明）

| 项 | 状态 |
|----|------|
| 主评估集 `field_source` | **synthetic**（可绑 step2 页序元数据） |
| 真实 OCR 全链路 | `import_external_ocr` → `fixtures/semi`；无 label → **smoke only** |
| 任务1–3 视觉/OCR 训练 | **非本模块** |
| 生产鉴权 | 可选 `TASK4_WEB_TOKEN`；审计 `out/audit.log` |
| suite=all | 仅调试，**禁止**当交付主数字 |

---

## 7. 交叉核对记录

| 检查 | 结果 |
|------|------|
| §3 文档链接目标文件 | 全部存在（AGENTS/STATUS 在**仓库根**） |
| 脚本表 | 全部存在 |
| critical_guard / adapters | 存在 |
| D1–D9 路径 | 与 GOAL §2 对齐，均有代码/文档落点 |
| fixtures (main) | **≥100**（Round33 里程碑） |
| 官方指标 | cov **0.9818** · TP **79** · FPR/FNR/miss **0** · THRESHOLD PASS |
| ci_gate | `bash scripts/ci_gate.sh` → **PASS**（见 ITERATION_LOG Round33） |

- docs/TASK4_DATA_REQUIREMENTS_AND_STEP2_ANALYSIS.md — 任务4数据需求与 step2 归属分析
