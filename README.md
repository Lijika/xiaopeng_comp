# 任务4：跨单据一致性校验

同一笔汽车融资租赁申请下，登记证、保单、合同、发票、身份证等单据的关键字段交叉核验。

输入是结构化 JSON，不是影像。引擎先标准化（VIN / 日期 / 金额 / 地址 / 姓名 / 车牌 / 证件号），再按 YAML 规则比对，给出 **一致 / 不一致 / 存疑** 三态结论和可解释报告。

| 项 | 值 |
|----|----|
| 包 | `task4_consistency` **0.1.0** |
| 规则包 | `configs/rules_auto_lease.yaml` **1.9.0**（与库版本独立） |
| Python | ≥ 3.10 |
| 默认 Web | http://127.0.0.1:8765/ |

这是赛题对齐与可复现评估的 MVP，**不是**生产核贷终审系统。合成 fixture 上的指标达标，不能宣传为真实 OCR 成绩。

---

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate # Windows: .venv\Scripts\activate
pip install -U pip
pip install -e ".[dev,web]"
```

系统 Python 受 PEP 668 限制时必须用 venv。也可 `pip install -r requirements.txt`。

```bash
python -c "import task4_consistency as t; print(t.__version__)"
python -m task4_consistency --version
```

## 运行

### Web

```bash
bash scripts/run_web.sh
```

打开 http://127.0.0.1:8765/ ，必要时强制刷新。岗位在页面右上角切换。步骤见 [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)。

| 变量 | 默认 | 说明 |
|------|------|------|
| `HOST` / `PORT` | `127.0.0.1` / `8765` | 端口占用：`PORT=8767 bash scripts/run_web.sh` |
| `TASK4_S01_STATE_PATH` | `<仓库根>/out/s01.sqlite3` | S01 权威账本，必须是绝对路径；未设置时由脚本按仓库根生成 |
| `TASK4_FULL_DEMO_ROOT` | `/tmp/task4-full-demo.XXXXXX` | 全流程会话目录，每次启动新建；排查时指定绝对路径 |
| `TASK4_WEB_MODE` | `full` | `basic` 为旧业务演示入口 |

`out/` 与 `*.sqlite3` 是运行产物，已忽略，不入库。仓库已包含当前 React 构建（`task4_consistency/web/static/react/`）。重建前端需要 Node 22 / npm 11：

```bash
npm ci
npm run build
```

生产安装见 [`docs/DEPLOY.md`](docs/DEPLOY.md)。

### CLI

```bash
# 单申请
python -m task4_consistency check fixtures/applications/app_consistent_01.json \
  -c configs/rules_auto_lease.yaml -o out/report_ok.json

# 官方评估数字（suite=main）
python -m task4_consistency evaluate \
  -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json

# 测试
pytest -q

# 一键门禁：pytest + evaluate main + 对抗 + smoke_web + bench
bash scripts/ci_gate.sh
# 期望：=== CI GATE PASS ===
```

---

## 能做什么

1. **字段标准化与实体链接**：VIN、日期、金额、地址、姓名、车牌、证件号等
2. **YAML 规则**：exact / fuzzy / numeric_tolerance / list_contains / conditional_required
3. **三态报告**：一致 / 不一致 / 存疑，含字段快照与 diff
4. **本地 Web**：核验 → 复核 → 补件 / 特批 → 规则发布 → 投递 / 取消 / 删除 / 导出（全流程演示）

常用入口：

| 入口 | 命令 |
|------|------|
| Web 演示 | `bash scripts/run_web.sh` |
| 单申请校验 | `python -m task4_consistency check <app.json> -c configs/rules_auto_lease.yaml` |
| 评估（交付数字） | `python -m task4_consistency evaluate --suite main -c configs/rules_auto_lease.yaml -o out/metrics_main.json` |
| 半真实 OCR 导入集 | `… --suite semi -o out/metrics_semi.json`（无标签时仅 smoke） |
| CI 门禁 | `bash scripts/ci_gate.sh` |
| Web smoke | `python scripts/smoke_web.py` |
| OCR 中间 JSON 导入 | `python scripts/import_external_ocr.py fixtures/layout_slots/example.json -o fixtures/semi/` |
| 对抗探针 | `python scripts/attack_probes.py` |
| 性能基线 | `python scripts/bench.py` |

---

## 输入 / 输出

- 输入：一笔申请的多单据 JSON，见 `fixtures/applications/`
- 规则：`configs/rules_auto_lease.yaml`
- 输出：JSON 报告，可选 `--markdown` / `--html`

改规则：编辑 YAML 的 `field_aliases` 与 `rules[]`。缺字段默认 `on_missing: uncertain`，避免误报。改完重新跑 `evaluate`。

### 评估指标（合成 fixture）

| 指标 | 目标 | 定义 |
|------|------|------|
| coverage | ≥ 80% | 有标签且得到 decisive 结论的比例 |
| FPR | ≤ 5% | 标签 consistent，预测 inconsistent |
| FNR | ≤ 3% | 标签 inconsistent，预测 consistent |
| miss_rate | ≤ 10% | 标签 inconsistent，预测 consistent **或** uncertain |

数字以 `evaluate --suite main` 为准。`suite=all` 只用于调试，不作交付口径。

---

## 仓库结构

```
task4_consistency/     # 核心包：normalize / match / rules / kb / web / adapters
configs/               # 规则包与实体 KB
fixtures/
  applications/        # suite=main（synthetic）
  semi/                # suite=semi（external OCR import）
  layout_slots/        # 待填字的版面槽位 + 外部 OCR 导入样例
data/registration_layout/  # 登记证页序 + 检测框（无文本）
frontend/              # React 源码；构建写入 task4_consistency/web/static/react/
scripts/               # run_web.sh / ci_gate.sh / demo.sh / bench / attack_*
tests/  docs/  out/    # out/ 仅运行产物，git 只保留 .gitkeep
```

登记证版面适配（页序与检测框 → 占位申请单，字段 `raw` 为空）：

```bash
python -m task4_consistency.adapters.registration_layout \
  data/registration_layout/JFL25P02L080310-01_page_order.json -o out/layout_app.json
```

---

## 文档

| 文档 | 内容 |
|------|------|
| [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) | 浏览器操作路径 |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | 安装、环境变量、生产发布 |
| [`docs/INTERFACE.md`](docs/INTERFACE.md) | 输入输出与 API |
| [`docs/CONFIG_GUIDE.md`](docs/CONFIG_GUIDE.md) | 规则与 KB |
| [`docs/EVALUATION_REPORT.md`](docs/EVALUATION_REPORT.md) | 评估口径与结果 |
| [`docs/DELIVERABLES.md`](docs/DELIVERABLES.md) | 交付清单 ↔ 仓库路径 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 模块边界 |

---

## 边界（必读）

| 边界 | 说明 |
|------|------|
| 输入 | 结构化字段；不含任务 1–3 视觉/OCR 训练与影像包 |
| 评估数据 | 合成 fixture（含对抗样例）；达标 ≠ 真实业务分布达标 |
| miss_rate | 防止把真不一致藏进「存疑」来刷 FNR |
| 约数金额 | `约12.5万` 可解析但带 `money_approx` → 存疑，不自动放行 |
| 人工复核 | critical inconsistent 必须人工处理 |
| 运行状态 | 账本、审计日志、`out/` 不入库；每次干净检出从空账本起步 |

生产接入前：换真实标注集、标定阈值、审计规则 version，并与任务 1–3 字段契约联调。
