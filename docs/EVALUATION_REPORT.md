# 评估报告 — 任务4 跨单据一致性校验

**模块：** `task4_consistency`  
**规则包：** `configs/rules_auto_lease.yaml`  
**报告日期：** 2026-07-25  
**对应赛题：** 江苏金租 · 任务4 指标与交付  

---

## 1. 评估目标与赛题对照

| 赛题指标 | 阈值 | 本报告口径 |
|----------|------|------------|
| 跨单据一致性校验自动化覆盖率 | ≥ 80% | `coverage` = 标签对中得到 decisive 结论（非 skip）的比例，与引擎 report coverage 对齐；见 `evaluate.py` |
| 校验误报率（一致→不一致） | ≤ 5% | `false_positive_rate` = FP / (TN+FP)，仅统计 decisive 预测 |
| 校验漏报率（不一致→一致） | ≤ 3% | `false_negative_rate` = FN / (TP+FN) |
| （内部防注水） | miss_rate ≤ 10% | expected=inconsistent 且 predicted∈{consistent, uncertain} 的比例 |

另设对抗探针 `scripts/attack_probes.py`：身份类 P0 洞必须 `release_open=0`。

---

## 2. 评估集

| 属性 | 说明 |
|------|------|
| 路径 | `fixtures/applications/*.json` |
| 规模 | **100** 笔申请（多单据：登记证书 / 交强险保单 / 融资租赁合同 / 发票 / 身份证 等）— **百级里程碑** |
| 标签 | fixture 内嵌 `expected_verdicts`（rule_id → consistent\|inconsistent\|uncertain） |
| 场景覆盖 | 全一致、VIN/金额/姓名/证件/发动机/登记证/地址/车牌/日期/品牌/型号不一致、OCR 噪声、格式变体、低置信、约数金额、二手车过户、价税合计、品牌合资、I/O/Q、歧义日期、placeholder、ID15/18、buyer 错位等 |
| **边界（强制声明）** | **合成 + 业务规则驱动构造**，非主办方官方全量标注集；未接任务1–3 真实 OCR 全链路。结论外推到生产须追加真实样本复测。 |

半真实资产：`data/step2/*_page_order.json`（页序/检测框，无 OCR 文本值）经 `adapters/step2_page_order.py` 可转占位结构；Round12/13 fixtures 绑定 `meta.step2_sample_id`（字段值仍合成）。

---

## 3. 复现命令

```bash
cd /home/lhjysyx/xiaopeng_comp
bash scripts/ci_gate.sh          # 推荐：pytest + evaluate main + 对抗 + smoke_web + bench
# 或分项：
.venv/bin/pytest -q
.venv/bin/python -m task4_consistency evaluate \
  -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json
.venv/bin/python scripts/attack_probes.py
.venv/bin/python scripts/attack_web_kb.py
.venv/bin/python scripts/baseline_exact_only.py  # 可选基线对比
```

---

## 4. 实测结果（主系统）

**快照时间：** 2026-07-25 Round33（fixtures ≥100 里程碑）  

### 4.A suite=main（**唯一交付官方数字**）

| 指标 | 实测 | 阈值 | 结论 |
|------|-----:|------|:----:|
| coverage | **0.9818** | ≥0.80 | **PASS** |
| false_positive_rate | **0.0** | ≤0.05 | **PASS** |
| false_negative_rate | **0.0** | ≤0.03 | **PASS** |
| miss_rate | **0.0** | ≤0.10 | **PASS** |
| total_pairs | 1207 | — | — |
| decisive_pairs | 1185 | — | — |
| TP / TN / FP / FN | **79** / 1106 / 0 / 0 | — | — |
| n_inconsistent_labeled_decisive | **79** | — | FNR 分母 |
| fixtures | **100**（`fixtures/applications`） | — | **≥100 里程碑**；含 hard inconsistent |
| field_source | **synthetic** | — | **非 external_ocr**；不得宣传真实 OCR |
| pytest | **147+** | 全绿 | **PASS** |
| `bash scripts/ci_gate.sh` | **PASS** | 全部门禁 | **PASS** |

CLI：`evaluate --suite main` → **THRESHOLD PASS**。  
`honesty_note`：不得将 main 合成集宣传为真实 OCR 评估。

### 4.B suite=semi（external_ocr 导入；无可靠 label → **smoke only**）

| 项 | 说明 |
|----|------|
| 路径 | `fixtures/semi/`（import 自 `ocr_inbox` 中间 JSON） |
| mode | **smoke**（无 embedded expected_verdicts 时） |
| 可报 | `n_apps_loaded` / `n_check_ok` / `n_check_fail` / `verdict_counts` / `uncertain_rate` |
| **禁报** | coverage / FPR / FNR / miss_rate /「漏报≤3%」 |
| field_source | import **强制** `external_ocr` + ocr_model/version |

示例导入：
```bash
.venv/bin/python scripts/import_external_ocr.py fixtures/ocr_inbox/example.json -o fixtures/semi/ --demo-note demo
.venv/bin/python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite semi -o out/metrics_semi.json
```

### 4.C 对抗门禁（与 suite 正交）

| 检查 | 结果 |
|------|------|
| attack_probes | **release_open=0** |
| attack_web_kb | **open=0 / w12_open=0** |

`pass_thresholds`（main）四门均为 true。

---

## 5. 与「精确字符串比对」基线对比

赛题指出：精确字符串比对误报高。本仓库提供 `scripts/baseline_exact_only.py`（若存在）：

- **Baseline：** 原始 `raw` 字符串 exact，无 normalize/fuzzy/容差  
- **System：** 完整 normalize + 规则引擎  

预期（与设计一致）：

| 现象 | Baseline | System |
|------|----------|--------|
| `苏A·12345` vs `苏A12345` | 易误报不一致 | plate normalize → 一致 |
| `2023年2月1日` vs `2023-02-01` | 易误报 | date normalize → 一致 |
| `12.8万` vs `128000` | 易误报 | money normalize → 一致 |
| 一汽大众 vs 上汽大众 | 可能都「含大众」误合并 | 品牌保留前缀 → 不一致 |

实测（`scripts/baseline_exact_only.py`，302 个 exact 规则×申请对）：

| 方法 | 要点 | 结果 |
|------|------|------|
| 朴素 raw exact | 无 normalize | 判不一致 59 对；consistent_rate 0.8046 |
| task4 引擎 | normalize+规则 | 一致 278 / 不一致 14 / 存疑 10 |
| **normalize rescue** | raw 不等但引擎一致 | **42**（基线会误报为不一致） |

主评估集标签指标（全规则）：system coverage **0.9676** / FPR **0** / FNR **0**。

---

## 6. 对抗与回归摘要

| 类别 | 结果 |
|------|------|
| ADV-01..03 等历史 P0 | CLOSED |
| ADV-19 二手车姓名 / ADV-21 buyer | CLOSED |
| Web/KB | `scripts/attack_web_kb.py` + 单测 |
| 详见 | `docs/ATTACK_CASES.md` |

---

## 7. 能力覆盖（任务4 三条）

| 要求 | 验证方式 |
|------|----------|
| 标准化 + 实体链接 | 单测 normalize/*；KB `configs/kb/entity_kb.json` + Web CRUD |
| 多层级规则 + 界面维护 | YAML 五类规则；Web 规则页 → `runtime_rules.yaml` |
| 三态 + 快照 + 高亮 | CLI/HTML/Web 报告；fixtures 回归 |

---

## 8. 风险与后续

1. **评估集合成偏差** → 接入真实多单据 OCR 后重跑 evaluate  
2. **FNR 分母规模** → 持续增加 labeled inconsistent  
3. **知识库质量** → 错误别名可致 FP；需权限与审计（P2）  
4. **与任务1–3 集成** → 契约已有，链路待实数据打通  

---

## 9. 结论

在 **当前版本化合成评估集** 上，任务4 三项量化指标 **均达到赛题阈值**，自动化回归与对抗门禁通过。  
**完整赛题「测试数据集」若指主办方官方集，则需在获数后按第 3 节命令重出本报告。**  
文档包：本报告 + `DEPLOY.md` + `CONFIG_GUIDE.md` + `INTERFACE.md` + `README.md` 构成任务4 技术文档主干。
