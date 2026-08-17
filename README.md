# 任务4：跨单据字段对齐与鲁棒一致性校验（MVP）

面向汽车融资租赁场景：同一申请下多份单据（登记证、保单、合同、发票、身份证等）的关键字段交叉核验。

**库版本：** `task4_consistency.__version__` ≡ `pyproject.toml` → **0.1.0**（与规则包 `configs/rules_auto_lease.yaml` 的 `version` 字段独立；规则包当前 `1.9.0`）。

## 一键验收（Round30）

```bash
cd /home/lhjysyx/xiaopeng_comp   # 或你的克隆路径
source .venv/bin/activate        # 若尚未创建：python3 -m venv .venv && pip install -e ".[dev,web]"

# 1) 全部门禁（pytest + evaluate main + 对抗 + smoke_web + bench）
bash scripts/ci_gate.sh
# 期望：=== CI GATE PASS ===

# 2) Web 演示（另开终端）
bash scripts/run_web.sh
# 浏览器：http://127.0.0.1:8765/  ·  health: curl -s localhost:8765/api/health | jq .
```

查版本：

```bash
python -c "import task4_consistency as t; print(t.__version__)"
python -m task4_consistency --version
```

## 能力

1. **字段标准化与实体链接**：VIN / 日期 / 金额 / 地址 / 姓名 / 车牌 / 证件号等
2. **多层级一致性规则（YAML）**：exact / fuzzy / numeric_tolerance / list_contains / conditional_required
3. **三态结论 + 可解释报告**：一致 / 不一致 / 存疑，含字段快照与 diff 高亮

## 安装

```bash
cd /home/lhjysyx/xiaopeng_comp
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
# 或: pip install -r requirements.txt
```

Python ≥ 3.10；依赖轻量（PyYAML + 标准库）。系统 Python 若受 PEP 668 限制，请用 venv。

## 入口图（Round21）

| 入口 | 命令 | 说明 |
|------|------|------|
| **CI 门禁** | `bash scripts/ci_gate.sh` | pytest + evaluate main + attack_web_kb + attack_probes + smoke_web + bench |
| **交付索引** | [`docs/DELIVERABLES.md`](docs/DELIVERABLES.md) | 赛题任务4 交付清单 ↔ 仓库路径 |
| **Web smoke** | `python scripts/smoke_web.py` | TestClient：`/api/health` + check fixture |
| **CLI check** | `python -m task4_consistency check <app.json> -c configs/rules_auto_lease.yaml` | 单申请三态报告 |
| **evaluate main** | `python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json` | **唯一交付数字** |
| **evaluate semi** | `… --suite semi -o out/metrics_semi.json` | external_ocr 集；无 label 时仅 smoke |
| **Web 演示** | `bash scripts/run_web.sh` → http://127.0.0.1:8765/ | 校验 / 批量 / 规则·KB 只读（变更走 S08/S09 治理）；可选 `TASK4_WEB_TOKEN` |
| **OCR 导入** | `python scripts/import_external_ocr.py fixtures/ocr_inbox/example.json -o fixtures/semi/` | 离线中间 JSON，无 OCR 引擎 |
| **bench** | `python scripts/bench.py` | → `out/bench.json`（mean≪50ms） |
| **demo** | `bash scripts/demo.sh` | 报告样例 + evaluate + probes |

## 快速运行

```bash
# 推荐：一键门禁（非 0 即失败）
bash scripts/ci_gate.sh

# 单申请校验
python -m task4_consistency.cli check fixtures/applications/app_consistent_01.json \
  -c configs/rules_auto_lease.yaml -o out/report_ok.json

# 官方评估（suite=main）
python -m task4_consistency evaluate \
  -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json

# 单元测试
pytest -q

# Web 集成演示 + 规则/KB 只读视图（变更由 S08/S09 治理）
bash scripts/run_web.sh
# 浏览器打开 http://127.0.0.1:8765/
#   校验演示 / 批量·evaluate(suite) / 规则·知识库（只读；变更走 S08 策略管理 / S09 治理工作区）
```

## 输入 / 输出

- 输入：一笔申请的多单据结构化 JSON（见 `fixtures/applications/`）
- 规则：`configs/rules_auto_lease.yaml`
- 输出：JSON 报告（可选 `--markdown` / `--html`）

文档：

| 文档 | 内容 |
|------|------|
| [`docs/INTERFACE.md`](docs/INTERFACE.md) | 输入输出与 API |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 部署手册 |
| [`docs/CONFIG_GUIDE.md`](docs/CONFIG_GUIDE.md) | 配置方案 |
| [`docs/EVALUATION_REPORT.md`](docs/EVALUATION_REPORT.md) | 正式评估报告 |
| [`docs/STEP2_TO_TASK4_PIPELINE.md`](docs/STEP2_TO_TASK4_PIPELINE.md) | step2 框位清单 → 外部 OCR → 任务4 衔接（`1.zip`+step2 不能单独当真跨单输入） |

## 目录

```
task4_consistency/     # 核心包
  normalize/ match/ rules/ kb/ web/
  adapters/            # step2 + external_ocr_import
  report.py / evaluate.py / cli.py
configs/               # rules + kb + runtime
fixtures/
  applications/        # suite=main（field_source=synthetic）
  semi/                # suite=semi（external_ocr import）
  ocr_inbox/           # 离线 OCR 中间 JSON 样例
scripts/
  ci_gate.sh           # CI 一键门禁
  run_web.sh demo.sh bench.py attack_*.py import_external_ocr.py
tests/ docs/ out/
```

## 规则维护

编辑 `configs/rules_auto_lease.yaml`：

- `field_aliases`：跨单据同义字段映射（如 `owner_name` ↔ `lessee_name`）
- `rules[]`：每条规则指定 `type` / `field` / `docs` / 容差或阈值
- `on_missing: uncertain`：缺字段默认存疑，控制误报
- `low_confidence_threshold`：低 OCR 置信度 → 存疑

改规则后重新跑 `evaluate` 验证指标。

## 评估指标（fixture 集）

| 指标 | 目标 | 定义 |
|------|------|------|
| 自动化覆盖率 coverage | ≥ 80% | decisive / active_labeled（排除 skipped） |
| 误报率 FPR | ≤ 5% | expected=consistent 且 decisive 中预测 inconsistent |
| 漏报率 FNR | ≤ 3% | expected=inconsistent 且 decisive 中预测 consistent |
| **miss_rate** | **≤ 10%**（硬门槛） | expected=inconsistent 中预测为 **consistent 或 uncertain**；CLI `--max-miss-rate` 可调 |

```bash
python -m task4_consistency.cli evaluate fixtures/applications \
  -c configs/rules_auto_lease.yaml -o out/metrics.json
# 关注 pass_thresholds + miss_rate + n_inconsistent_labeled_decisive
```

## 商业边界声明（必读）

本仓库是 **任务4 规则/标准化 MVP**，用于赛题对齐与可复现评估，**不是**生产核贷终审系统。

| 边界 | 说明 |
|------|------|
| 输入 | 结构化 JSON 字段；**不**含任务1–3 视觉/OCR 训练与全量 `1.zip` 抽取 |
| 评估数据 | **合成 fixture**（含对抗 ADV 样例）；指标达标 ≠ 真实业务分布达标 |
| FNR 分母 | 仅 decisive 预测；另用 **miss_rate** 覆盖 uncertain 隐匿真不一致 |
| 品牌/VIN/日期 | Round3 已关 ADV-01/02/03；合资前缀、I/O/Q 纠错、歧义日期有明确策略 |
| 低置信 | critical 规则可在低 conf 下仍报 **inconsistent+low_conf**（`critical_low_conf_compare`） |
| 约数金额 | `约12.5万` 可解析但带 `money_approx` → **uncertain**，不自动放行 |
| 人工复核 | HTML/Markdown 报告供复核；critical inconsistent 必须人工处理 |

生产接入前请：换真实标注集、标定阈值、审计规则 version、与任务1–3 字段契约联调。

## 可选：step2 适配

```bash
python -m task4_consistency.adapters.step2_page_order \
  data/step2/JFL25P02L080310-01_page_order.json -o out/step2_app.json
```

将检测框映射为字段占位（无 OCR 文本时 `raw=null`），便于后续接任务1–3。

## 设计要点

- 任务4与 OCR **解耦**：只吃结构化字段
- **先标准化再比对**
- 规则配置化，业务改 YAML 不改代码
- 三态结论优先可解释与控误报
