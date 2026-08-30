# 任务4 MVP 架构设计

## 1. 赛题定位

任务4解决：结构化提取之后，**同一申请多份单据** 的关键字段交叉核验。

业务卡点：
- 同一信息表述变体（日期格式、金额单位、地址简繁/别名、姓名多音/OCR 噪声）
- 主键对齐依赖非精确匹配
- 业务对不一致容忍度极低，但精确字符串比对误报高

交付对标：
- 跨单据一致性校验自动化覆盖率 ≥ 80%
- 误报率（一致→不一致） ≤ 5%
- 漏报率（不一致→一致） ≤ 3%

## 2. 现有数据盘点

| 资产 | 内容 | 对任务4的含义 |
|------|------|----------------|
| 赛题说明 | 任务定义与指标 | 需求源 |
| 登记证影像 | 多页机动车登记证书 | 上游影像，非任务4主输入 |
| 登记证版面 | 页序 / 页类型 / 检测框 | 有字段位置，无字段文本 |

结论：任务4 MVP 以 **结构化 JSON 输入** 为主；用合成跨单据 fixture 验证规则与指标。版面适配器可选，把检测框接到申请单 schema（`raw` 仍为空）。

## 3. MVP 系统边界

```
[任务1-3 结构化输出 / 合成 fixture]
              │
              ▼
     ┌──────────────────┐
     │  Application JSON │  一笔申请 = 多单据 = 多字段
     └────────┬─────────┘
              ▼
     ┌──────────────────┐
     │  Field Normalizer │  类型化标准化 + 实体链接
     └────────┬─────────┘
              ▼
     ┌──────────────────┐
     │  Rule Engine      │  YAML 规则：exact/fuzzy/tol/list/cond
     └────────┬─────────┘
              ▼
     ┌──────────────────┐
     │  Graded Report    │  一致|不一致|存疑 + 快照/高亮
     └──────────────────┘
```

## 4. 目录结构（目标）

```
xiaopeng_comp/
  GOAL.md
  ARCHITECTURE.md
  DELIVERY.md
  README.md
  pyproject.toml / requirements.txt
  configs/
    rules_auto_lease.yaml
  task4_consistency/
    __init__.py
    models.py          # Application/Document/Field/CheckResult
    normalize/
      __init__.py
      base.py
      vin.py
      date.py
      money.py
      address.py
      person.py
      plate.py
      id_number.py
    match/
      __init__.py
      exact.py
      fuzzy.py
      numeric.py
      list_ops.py
    rules/
      __init__.py
      loader.py
      engine.py
    report.py
    evaluate.py
    adapters/
      registration_layout.py
    cli.py
  fixtures/
    applications/
      app_consistent_01.json
      app_inconsistent_vin.json
      app_inconsistent_amount.json
      app_uncertain_ocr_noise.json
      ...
    labels/
      expected_verdicts.json
  tests/
    test_normalize_*.py
    test_match_*.py
    test_rules_engine.py
    test_report.py
    test_evaluate.py
    test_cli.py
  docs/
    INTERFACE.md
    REVIEW.md
  out/                 # gitignore 运行产物
  data/
    registration_layout/  # 登记证页序与检测框
```

## 5. 数据契约

### 5.1 Application 输入

```json
{
  "application_id": "APP-001",
  "documents": [
    {
      "doc_id": "reg_cert",
      "doc_type": "机动车登记证书",
      "fields": {
        "vin": {"raw": "LSVAA4182N2123456", "confidence": 0.91, "source_page": 1},
        "engine_no": {"raw": "EA888-123456", "confidence": 0.88},
        "owner_name": {"raw": "张三", "confidence": 0.95},
        "reg_cert_no": {"raw": "3201xxxxx", "confidence": 0.9},
        "plate_no": {"raw": "苏A·12345", "confidence": 0.9}
      }
    },
    {
      "doc_id": "policy",
      "doc_type": "交强险保单",
      "fields": {
        "vin": {"raw": "LSVAA4182N2123456", "confidence": 0.93},
        "owner_name": {"raw": "张 三", "confidence": 0.9},
        "plate_no": {"raw": "苏A12345", "confidence": 0.92}
      }
    },
    {
      "doc_id": "lease_contract",
      "doc_type": "融资租赁合同",
      "fields": {
        "vin": {"raw": "LSVAA4182N2123456", "confidence": 0.99},
        "lessee_name": {"raw": "张三", "confidence": 0.99},
        "id_number": {"raw": "320102199001011234", "confidence": 0.99},
        "financed_amount": {"raw": "128000.00元", "confidence": 0.99}
      }
    }
  ]
}
```

### 5.2 规则配置（YAML）

```yaml
version: 1
field_aliases:
  owner_name: [owner_name, lessee_name, insured_name, buyer_name]
  vin: [vin, vehicle_id, 车辆识别代号]
rules:
  - id: R_VIN_CROSS
    name: VIN跨单据一致
    type: exact          # exact | fuzzy | numeric_tolerance | list_contains | conditional_required
    field: vin
    docs: [机动车登记证书, 交强险保单, 融资租赁合同]
    on_missing: uncertain
    severity: critical
  - id: R_AMOUNT_TOL
    name: 融资金额容差
    type: numeric_tolerance
    field: financed_amount
    docs: [融资租赁合同, 发票]
    abs_tol: 1.0
    rel_tol: 0.001
  - id: R_NAME_FUZZY
    name: 姓名模糊一致
    type: fuzzy
    field: owner_name
    docs: [机动车登记证书, 交强险保单, 融资租赁合同, 身份证]
    threshold: 0.88
  - id: R_ID_REQUIRED_IF_AMOUNT
    name: 有融资金额则身份证必填
    type: conditional_required
    if_field_present: financed_amount
    required_field: id_number
    docs: [融资租赁合同]
```

### 5.3 报告输出

```json
{
  "application_id": "APP-001",
  "summary": {"consistent": 8, "inconsistent": 1, "uncertain": 1, "coverage": 0.85},
  "checks": [
    {
      "rule_id": "R_VIN_CROSS",
      "verdict": "inconsistent",
      "severity": "critical",
      "message": "VIN 不一致",
      "snapshots": [
        {"doc_type": "机动车登记证书", "field": "vin", "raw": "...", "normalized": "..."},
        {"doc_type": "交强险保单", "field": "vin", "raw": "...", "normalized": "..."}
      ],
      "diff_highlight": {"pos": 12, "left": "1", "right": "7"}
    }
  ]
}
```

## 6. 核心设计决策

1. **任务4与 OCR 解耦**：接口吃结构化字段；图像链路属任务1–3。
2. **标准化先于比对**：所有匹配在 normalized value 上执行。
3. **规则配置化**：业务改规则不改代码；MVP 用 YAML，后续可接 UI。
4. **三态结论**：一致 / 不一致 / 存疑。低置信度、缺字段、近阈值 → 存疑，控误报。
5. **可解释优先**：每条规则带快照与 diff，服务人工复核。
6. **评估驱动**：fixtures 带期望 verdict；`evaluate` 输出覆盖/误报/漏报。

## 7. 标准化策略（摘要）

| 类型 | 策略 |
|------|------|
| VIN | 大写、去空格/分隔符、I/O/Q 纠错可选、长度/字符集校验 |
| 日期 | 解析多格式 → ISO `YYYY-MM-DD` |
| 金额 | 去货币符号/中文单位 → `Decimal` |
| 车牌 | 去分隔点/空格，统一大写 |
| 姓名 | 去空格，全半角，可选同音扩展（MVP 轻量） |
| 证件号 | 大写 X，校验位可选 |
| 地址 | 去空白、省市区别名表（知识图谱轻量版：dict alias） |

## 8. 实施分期

| Phase | 内容 | Owner |
|-------|------|-------|
| P0 | 工程骨架 + models + CLI 空跑 | dev |
| P1 | normalizers + matchers + 单测 | dev |
| P2 | 规则引擎 + 默认规则 YAML | dev |
| P3 | fixtures + evaluate + 指标 | dev |
| P4 | report 可解释 + README/接口文档 | dev |
| P5 | 登记证版面适配器（可选） | dev |
| P6 | review → fix → 收口 | review + dev + manager |

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 无真实多单据结构化标注 | 合成 fixture + 清晰标签；文档声明评估范围 |
| 模糊阈值不稳 | 默认保守阈值 + 存疑带；单测固定边界样例 |
| 角色 agent 跑偏 | GOAL.md 约束 + manager 分派明确验收命令 |

## 10. Manager 编排协议

1. Manager 写/更新 GOAL、ARCHITECTURE、当前任务卡。
2. `herdr agent prompt dev "..."` 下发可验收子任务，带 `--wait`。
3. 验收命令失败 → 回灌 dev。
4. 阶段性完成后 `herdr agent prompt review "..."` 独立审查。
5. review 问题进 `docs/REVIEW.md`；dev 修到 PASS。
6. 全部 checklist 绿 → 更新 GOAL Status = DONE。
