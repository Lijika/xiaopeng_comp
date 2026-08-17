# Task4 接口说明

## 1. Application 输入 JSON

```json
{
  "application_id": "APP-001",
  "documents": [
    {
      "doc_id": "reg_cert",
      "doc_type": "机动车登记证书",
      "fields": {
        "vin": {"raw": "LSVAA4182N2123456", "confidence": 0.91, "source_page": 1},
        "owner_name": {"raw": "张三", "confidence": 0.95}
      }
    }
  ],
  "expected_verdicts": {
    "R_VIN_CROSS": "consistent"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `application_id` | string | 申请编号 |
| `documents[]` | array | 单据列表 |
| `documents[].doc_id` | string | 单据实例 ID |
| `documents[].doc_type` | string | 单据类型（与规则 `docs` 对齐） |
| `documents[].fields` | object | 字段名 → FieldValue |
| `fields.*.raw` | string\|null | 原始抽取文本 |
| `fields.*.confidence` | number | OCR/抽取置信度，默认 1.0 |
| `fields.*.source_page` | int? | 来源页 |
| `expected_verdicts` | object? | 可选，评估用期望结论 |

字段值也可简写为字符串：`"vin": "LSV..."`。

## 2. 规则 YAML

见 `configs/rules_auto_lease.yaml`。

### 顶层

| 键 | 说明 |
|----|------|
| `version` | 配置版本 |
| `low_confidence_threshold` | 低于此置信度 → 存疑（默认 0.6） |
| `field_aliases` | 规范字段 → 别名列表 |
| `rules` | 规则数组 |

### 规则类型

| type | 关键参数 | 行为 |
|------|----------|------|
| `exact` | `field`, `docs` | 标准化后全等 |
| `fuzzy` | `field`, `threshold`, `uncertain_band` | 相似度；近阈值 → 存疑 |
| `numeric_tolerance` | `abs_tol`, `rel_tol` | 数值容差 |
| `list_contains` | `list_field`, `item_field` | 元素是否在列表中 |
| `conditional_required` | `if_field_present`, `required_field` | 条件必填 |

通用：`on_missing`（`uncertain`/`inconsistent`/`skip`）、`severity`（`critical`/`major`/`minor`/`info`）。

## 3. 报告输出 JSON

```json
{
  "application_id": "APP-001",
  "summary": {
    "consistent": 8,
    "inconsistent": 1,
    "uncertain": 1,
    "coverage": 0.9,
    "total": 10
  },
  "checks": [
    {
      "rule_id": "R_VIN_CROSS",
      "name": "VIN跨单据一致",
      "verdict": "inconsistent",
      "severity": "critical",
      "message": "vin 不一致: ...",
      "snapshots": [
        {
          "doc_id": "reg_cert",
          "doc_type": "机动车登记证书",
          "field": "vin",
          "raw": "...",
          "normalized": "...",
          "confidence": 0.91
        }
      ],
      "diff_highlight": {"pos": 12, "left": "1", "right": "7", "detail": "diff at index 12"},
      "score": 0.0,
      "rule_type": "exact",
      "flags": ["low_conf"],
      "reason_codes": ["VIN_MISMATCH", "LOW_CONF"]
    }
  ],
  "rule_config_version": "1.7.0",
  "rule_package": "auto_lease",
  "rule_changelog": ["1.7.0 Round7: ..."]
}
```

`verdict` ∈ `consistent` | `inconsistent` | `uncertain` | `skipped`。

`coverage` = (consistent + inconsistent) / total（不含 skipped）。

### reason_codes（机读，Round7）

稳定枚举见 `task4_consistency/reason_codes.py`。常见：

| code | 含义 |
|------|------|
| `VIN_MISMATCH` | VIN 跨单不一致 |
| `VIN_OCR_FIX_MERGE` | VIN I/O/Q 纠错后合并不同 raw → 存疑 |
| `ENGINE_MISMATCH` | 发动机号不一致 |
| `ID_MISMATCH` | 证件号不一致 |
| `NAME_MISMATCH` / `NAME_NEAR_UNCERTAIN` | 姓名硬差 / 近邻存疑 |
| `PLATE_MISMATCH` / `PLATE_NOT_IN_LIST` | 车牌不一致 / 不在列表 |
| `AMOUNT_MISMATCH` / `AMOUNT_APPROX` | 金额超容差 / 约数不可自动放行 |
| `DATE_MISMATCH` / `DATE_AMBIGUOUS` / `DATE_INCOMPLETE` | 日期差 / 歧义 slash / 缺日 |
| `BRAND_MISMATCH` / `MODEL_MISMATCH` | 品牌/型号不一致 |
| `LOW_CONF` / `LOW_CONF_COMPARED` | 低置信；低置信下仍比对 |
| `MISSING_DOCS` / `MISSING_FIELD` / `NORMALIZE_FAIL` | 齐套/缺字段/标准化失败 |
| `CONDITIONAL_REQUIRED_FAIL` | 条件必填失败 |
| `SKIPPED` / `CONSISTENT` | 跳过 / 一致 |

### 规则包版本

YAML 顶层：`package` / `version` / `changelog[]`。报告回写 `rule_package` / `rule_config_version` / `rule_changelog`。

## 4. CLI

```bash
python -m task4_consistency.cli check <app.json> -c <rules.yaml> \
  [-o out.json] [--markdown out.md] [--html out.html] [--strict] [--strict-vin]
# suite 默认 main；apps_dir 可省略
python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json
python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite semi -o out/metrics_semi.json
python -m task4_consistency.cli evaluate [apps_dir] -c <rules.yaml> \
  [--suite main|semi|all] [--mode labeled|smoke] \
  [-o metrics.json] [--html metrics.html] [-l labels.json] [--max-miss-rate 0.1]
# 离线 external OCR 中间 JSON → fixtures/semi
.venv/bin/python scripts/import_external_ocr.py fixtures/ocr_inbox/example.json -o fixtures/semi/ --demo-note demo
bash scripts/demo.sh
.venv/bin/python scripts/bench.py
```

### 批量评估 vs Web（Arch Round26 终裁）

| 需求 | 入口 | 说明 |
|------|------|------|
| **全量带标签评估（交付数字）** | **CLI** `python -m task4_consistency evaluate --suite main -c configs/rules_auto_lease.yaml -o out/metrics_main.json` | 唯一官方 metrics；可 `--suite semi\|all` |
| **Web 评估摘要** | `GET /api/evaluate/summary?suite=main` | 同步跑 suite 目录；返回 metrics JSON + HTML 摘要 |
| **Web 多样例校验** | `POST /api/check/batch` | 仅 **check**（三态汇总），**不是** evaluate；无 FP/FN |
| **单笔校验** | `POST /api/check` | 完整 report + html |

**明确不做（终裁）：** `/api/evaluate/batch`、异步 job 队列、后台任务轮询。大批量/CI 请走 **CLI evaluate** 或 `bash scripts/ci_gate.sh`。

#### `POST /api/check/batch` 上限与超时

| 项 | 建议 |
|----|------|
| **N 上限** | 默认 **`BATCH_CHECK_MAX_N=50`**（超限 HTTP 400 `batch_too_large`） |
| **请求体** | `{"fixture_files":["a.json",…]}` 和/或 `{"applications":[…]}` |
| **超时** | 同步接口；单笔 ≪50ms 时 50 笔通常 <3s。反向代理建议 **≥30s**；更大集合请 CLI 分批或 `evaluate` |
| **用途** | 演示/抽检；**禁止**用 batch check 结果替代 suite=main 漏报/误报门禁 |

### CLI 退出码

| code | 含义 |
|-----:|------|
| 0 | 成功（evaluate 门槛通过） |
| 1 | evaluate 门槛失败 / 用法问题 |
| 2 | `check --strict` 且存在 inconsistent |
| 3 | 输入/配置文件不存在 |
| 4 | 非法 JSON / application schema |
| 5 | 非法规则配置 |
| 6 | 未预期运行时错误 |

### 规则版本协商

- 报告字段 `rule_config_version` 来自 YAML `version`
- 调用方应记录所用规则文件路径 + version；规则变更后重跑 evaluate

### evaluate 指标 JSON（单一定义）

| 字段 | 含义 |
|------|------|
| `coverage` | `decisive_pairs / active_labeled_pairs`（排除 skipped） |
| `false_positive_rate` | FP/(TN+FP)：标签 consistent 且预测 decisive 中的误报 |
| `false_negative_rate` | FN/(TP+FN)：标签 inconsistent 且预测 decisive 中的漏报 |
| `miss_rate` | 标签 inconsistent 中预测为 consistent **或** uncertain 的比例（含 uncertain 隐匿） |
| `mean_app_coverage` | 各申请 report.coverage 均值（仅信息，不参与门槛） |
| `n_inconsistent_labeled_decisive` | FNR 分母；过小时会 warning |
| `pass_thresholds` | labeled：coverage≥0.80 / FPR≤0.05 / FNR≤0.03 / **miss_rate≤0.10**；smoke：仅 `smoke_load_ok` |
| `suite` | `main` \| `semi` \| `all`（交付只认 main） |
| `mode` | `labeled` \| `smoke` |
| `honesty_note` | 诚实性声明（禁止用 semi smoke 宣称漏报≤3%） |
| `n_apps_loaded` / `n_check_ok` / `n_check_fail` | 加载/运行计数（smoke 主指标） |
| `verdict_counts` / `uncertain_rate` | 三态分布 |

### External OCR 中间 schema（Round19 · 唯一 import）

| 字段 | 必填 | 说明 |
|------|------|------|
| `schema_version` | Y | 仅 `1` |
| `ocr_model` / `ocr_version` | Y | 非空 |
| `application_id` | Y | |
| `documents[]` | Y | ≥1；`doc_id`/`doc_type`/`fields` |
| `fields.<name>.raw` | Y | str\|null |
| `fields.<name>.confidence` | N | [0,1]，默认 1.0 |

import 后 `meta` 强制：`source=external_ocr`, `field_source=external_ocr`, `ocr_model`, `ocr_version`, `ocr_imported_at`。  
路径限 repo 内、单文件 ≤2MB。**无 OCR 引擎、无假生成器。**

### `meta.field_source`

| 值 | 含义 |
|----|------|
| `synthetic` | 手写/业务构造（含 step2 绑定假字） |
| `external_ocr` | 经 import schema |
| `null` | step2 adapter 仅框、raw=null |

`field_source≠external_ocr` **不得**宣传真实 OCR 评估。

配置要点：

| 键 | 默认 | 说明 |
|----|------|------|
| `date_order` | `null` | slash 歧义日期；null→uncertain |
| `vin_fix_ioq` | `true` | I/O/Q 纠错；不同 raw 合并→uncertain |
| `vin_strict_check_digit` | **`false`** | ISO 3779 校验位；合成 fixture 默认关，生产可开 |
| `expand_id15_to_18` | `true` | 15 位证扩 18（世纪 19xx） |
| `critical_low_conf_compare` | `true` | critical 低 conf 仍报不一致 |
| `transfer_name_policy`（规则级） | `uncertain` | 二手车过户姓名 |

占位符 denylist（engine/reg_cert/id 等）：`无`/`N/A`/`—`/`暂无`/… → normalize None → **uncertain** + `PLACEHOLDER_VALUE`。

标签**必须**来自 fixture 内 `expected_verdicts` 或 labels 文件；**禁止**用预测回填标签（soft-label 已移除）。

### 规则扩展字段（Round1）

| 键 | 说明 |
|----|------|
| `require_all_docs` | true 时 listed `docs` 必须齐套有值；critical 默认 true |
| `default_require_all_docs` | 配置级默认；`null`=critical 自动开启 |
| `on_missing: skip` | 缺字段时 `verdict=skipped`，**不计入** coverage 分母 |

注意：`reg_date` **不** alias `contract_date`（登记日 ≠ 合同日）。

品牌/型号：`R_BRAND_CROSS` / `R_MODEL_CROSS`（缺字段 skip，不强制齐套）。

### Critical 指纹与规则校验（Round16 · Issue #45 收缩后）

- 模块：`task4_consistency.rules.critical_guard`
- `enforce_critical_fingerprints(cfg)`：缺/篡改 critical 三剑客 → `CriticalGuardError`（`.error` 码）
- Web（收缩后**仅校验、不写盘**）：
  - `POST /api/rules/validate`：干跑；返回 `critical_fingerprints` 元数据；**永不写 `runtime_rules.yaml`**
  - `PUT /api/rules` / `POST /api/rules/reset`：已退役（请求落 405/404 absence）；规则/KB 变更由 S08/S09 治理
- error 码：`critical_rule_missing` / `critical_semantic_tamper` / `critical_docs_stripped` / `critical_on_missing_skip`

## 5. Python API

```python
from task4_consistency.models import Application
from task4_consistency.rules.loader import load_rules
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.evaluate import evaluate_directory, evaluate_suite

app = Application.from_dict(data)
engine = RuleEngine(load_rules("configs/rules_auto_lease.yaml"))
report = engine.run(app)
print(report.to_dict())

# Round19 suites
metrics_main = evaluate_suite("main", "configs/rules_auto_lease.yaml")
metrics_semi = evaluate_suite("semi", "configs/rules_auto_lease.yaml")  # smoke if unlabeled

metrics = evaluate_directory("fixtures/applications", "configs/rules_auto_lease.yaml")
```

## 6. 标准化约定（摘要）

| 类型 | 输出 |
|------|------|
| VIN | 大写、去分隔；可选 I/O/Q→1/0/0 |
| 日期 | `YYYY-MM-DD` |
| 金额 | 无货币符号的十进制字符串 |
| 车牌 | 大写、去 `·`/空格 |
| 姓名 | 去空白/全半角 |
| 证件号 | 大写 X、去分隔 |
| 地址 | 去空白 + 省市别名归一 |

## 7. step2 适配器

```python
from task4_consistency.adapters.step2_page_order import load_page_order
app = load_page_order("data/step2/xxx_page_order.json")
```

检测类名映射到逻辑字段；无 OCR 时 `raw=null`，仅保留 bbox 置信度与页码。

## 审计 JSONL 信封（Round43 · schema_ver=1）

路径：`out/audit.log`（`TASK4_AUDIT_LOG` 可覆盖）。每行一 JSON；写失败 best-effort 不抛。

| 字段 | 类型 | 必填 | 说明 |
|------|------|:----:|------|
| `schema_ver` | int | Y* | 新写入 **`1`**；旧行读侧视为 **`0`（legacy）** |
| `ts` | str | Y | ISO-8601 UTC |
| `action` | str | Y | 自由串（非封闭 Enum） |
| `actor` | str | Y | 默认 `web` |
| `ok` | bool | Y | 成功与否 |
| `detail` | object | Y | 自由 dict，不校验形状 |

\* 读路径 `normalize_audit_record` / `read_audit_tail` / `GET /api/audit/recent` 兼容无版号旧行。
