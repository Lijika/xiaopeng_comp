# Attack Cases — Adversarial Review (adversary)

**Author:** Adversarial Reviewer (`adversary`)  
**Date:** 2026-07-25  
**Cwd:** `/home/lhjysyx/xiaopeng_comp`  
**Scope:** FP/FN paths, metric inflation, rule bypass, normalize cracks  
**Method:** `.venv/bin/python` code probes against live `RuleEngine` + `evaluate`  
**Constraint:** no mass business rewrite; fixtures/probes only  

---

## Round16 复验规格（W1 / W2）— 等 dev 交付后执行

**禁改 GOAL.md**  
**目的:** Round16 关闭 Web 规则 P0 后，adversary **硬复验** ADV-W1 / ADV-W2。  
**关联:** `docs/ROUND16_BRIEF.md` · loader 侧已有 `_REQUIRED_CRITICAL_FIELDS` / 原子写（以**运行中服务 + 当前 wheel/源码**为准，勿只信磁盘片段）。

### 总判定

| 结果 | 条件 |
|------|------|
| **PASS / CLOSED** | 下表 W1 全部子案 + W2 全部子案 **均 CLOSED** |
| **FAIL** | 任一条 OPEN → 保持 P0，退回 dev |

**命令入口（交付后）:**

```bash
# 重启 Web 以加载新代码（重要）
pkill -f 'uvicorn task4_consistency.web.app' || true
rm -f configs/runtime_rules.yaml
.venv/bin/python -m uvicorn task4_consistency.web.app:app --host 127.0.0.1 --port 8765 &
sleep 2
.venv/bin/python scripts/attack_w1_w2_retest.py   # 见规格；或手工 curl
```

---

### ADV-W1 — runtime 毒化 / 原子写 · P0

| 子案 | 步骤 | 期望（CLOSED） | 失败=OPEN |
|------|------|----------------|-----------|
| **W1-a** 缺 id | `PUT /api/rules` `content.rules=[{type:exact,field:vin,docs:[发票]}]` | HTTP **4xx**；`configs/runtime_rules.yaml` **不存在**或仍为**上一合法版** | 4xx 但留下**非法** runtime |
| **W1-b** 坏 YAML | `PUT` `yaml_text: "rules: ["` | **4xx**；runtime 不毒化；`GET /api/health` → **200** `ok:true` | health **500** 或 check 全挂 |
| **W1-c** 合法后失败回滚 | 先 PUT 合法 runtime（改 version 尾巴）；再 PUT 非法 | 非法 4xx 后 `GET /api/rules` 仍为**前一合法 version** | 回滚到空/默认丢失或非法文件 |
| **W1-d** 并发双写（可选） | 两路几乎同时 PUT 不同合法 version | 最终 runtime **可 load_rules**；无半截 YAML | 文件损坏 / load 失败 |

**探针断言伪码:**

```python
# W1-a
code, _ = put_rules(bad_missing_id)
assert code >= 400
assert not runtime_path.exists() or load_rules(runtime_path)  # 若存在必须合法
assert get_health()["ok"] is True
```

**CLOSED 记录模板:** `W1-a..d 全过 · runtime 无残留毒化 · health 200`

---

### ADV-W2 — critical 身份规则保护 · P0

**最小身份字段（当前 loader 约定）:** `vin`, `engine_no`, `id_number` 各至少一条 **`severity=critical`** 且 **`on_missing != skip`**。

| 子案 | 步骤 | 期望（CLOSED） | 失败=OPEN |
|------|------|----------------|-----------|
| **W2-a** 删除 `R_VIN_CROSS` | GET rules → 过滤掉该 id → PUT | HTTP **4xx**；body 含 critical/vin 类提示；**不写**成功 runtime | **200** 且 check 报告无 VIN 规则 |
| **W2-b** 删光所有 `severity=critical` | 仅保留 major/minor | **4xx** | **200** |
| **W2-c** VIN 规则 `severity→info` | 只改 R_VIN_CROSS severity | **4xx**（或等价拒绝 demote） | **200** 导致身份规则非 critical |
| **W2-d** VIN `on_missing=skip` | 只改 on_missing | **4xx** | **200**（缺页直接 skip 漏检） |
| **W2-e** 改 type 使 VIN 不再 exact 覆盖（若策略禁改 type） | type=fuzzy thr=0 等 | 按 arch 决议：**拒绝** 或 **仍 inconsistent 于真冲突** | 真 VIN 冲突变 consistent/skip |
| **W2-f** 正向对照 | 仅 bump `version` 字符串 PUT | **200**；规则数不变 | 误伤正常保存 |

**端到端漏检断言（W2-a 若错误地 200 时）:**

```python
app = four_docs_vin_mismatch()  # 两 VIN 不同
rep = post_check(app)
assert any(c["rule_id"]=="R_VIN_CROSS" and c["verdict"]=="inconsistent" for c in rep["checks"])
# CLOSED 路径：PUT 已 4xx，无需跑到这里
```

**CLOSED 记录模板:** `W2-a..f 全过 · 删/降级 critical 均 4xx · version bump 仍 200`

---

### 复验记录表（Round17 实测 · 2026-07-25）

**环境:** 重启后 `http://127.0.0.1:8767` · `scripts/attack_w1_w2_retest.py` **exit 0**

| 子案 | 结果 | HTTP/备注 |
|------|------|-----------|
| W1-a | **CLOSED** | 400 · runtime 不存在 |
| W1-b | **CLOSED** | 400 · health 200 |
| W1-c | **CLOSED** | bad 400 · version 仍 `w1c-good` |
| W2-a | **CLOSED** | 400 `critical_rule_missing` |
| W2-b | **CLOSED** | 400 critical 全降级拒 |
| W2-c | **CLOSED** | 400 `critical_semantic_tamper` |
| W2-d | **CLOSED** | 400 `critical_on_missing_skip` |
| W2-f | **CLOSED** | 200 version bump |
| **W1/W2 汇总** | **PASS · 全 CLOSED** | P0 规则保存毒化/删 critical **关闭** |

### Round17 K* / W4 复验

| ID | 结果 | 实测 |
|----|------|------|
| ADV-K1a | **OPEN P1** | KB `org_aliases` 一汽大众/上汽大众→大众 后 **`normalize_brand` 双变「大众」**（brand 接入 `resolve_org` 可重演 ADV-01 FP） |
| ADV-K1b | **CLOSED** | 同 alias 下地址串仍区分 |
| ADV-K2 | **CLOSED** | person section 422；人名不串 |
| ADV-K3 | **CLOSED** | 跨城别名 PUT **400** |
| ADV-K8 | **CLOSED** | 短 key「路」PUT **400** |
| ADV-K9 | **CLOSED** | 人保→平安 地址仍区分（或未折叠） |
| ADV-K10 | **CLOSED** | 单字「州」PUT **400** |
| ADV-W4 | **CLOSED** | rel_tol=0.5 PUT **400** |

**Round17 结论:** **W1/W2 P0 关闭确认。** 残留 **ADV-K1a P1**：禁止/校验 org 别名把不同 JV 品牌折成同一 canonical，或 brand normalize **勿**对短品牌 token 套 org 折叠。

---


## Post-Round14 攻击实测（审计 · 鉴权 · 规则保存）

**Date:** 2026-07-25 · **禁改 GOAL.md**  
**Base:** open `:8765` + token `:8766`（`TASK4_WEB_TOKEN=secret-r14-token`）  
**实现:** `OptionalTokenAuth` + `audit.write_audit` → `out/audit.log`

### 🚨 P0

| ID | 洞 | 实测 | 修复 |
|----|----|------|------|
| **ADV-W2** | ~~仍可删除 critical~~ → **R17 CLOSED** | R17：删 VIN / demote / skip → **400** | loader 最小 critical 集 |

### OPEN P1

| ID | 洞 | 实测 |
|----|----|------|
| **ADV-W4** | `rel_tol` 无上限 | `0.5` → 1e6 vs 5e5 **consistent** |
| **ADV-A3** | 未设 token 时 **admin API 全开放** | `auth_required=false`；reset/PUT/KB 均 200（demo 设计；生产误开会裸写配置） |

### OPEN P2

| ID | 洞 | 实测 |
|----|----|------|
| **ADV-A2b** | KB 审计明文 **value** | JSONL 含别名目标串 |
| **ADV-A20** | 校验失败的 rules 保存 **不写 audit** | 缺 id → 400 且 log 无 `rules_save ok=false` |
| **ADV-A22** | Bearer **尾空格**仍通过 | `Bearer secret ` → 200（strip） |

### CLOSED（相对 R10）

| ID | 结果 |
|----|------|
| **ADV-W1** | 坏 PUT **无** runtime 残留；health 200（先校验+原子写） |
| **ADV-A1/A2/A5** | `rules_save` / `kb_add` / `rules_reset` **有** JSONL |
| **ADV-A10/11/21** | 开 token 后无/错 token → **401**（含 KB 写） |
| **ADV-A12/13/16/19** | 正确 Bearer/`X-Task4-Token` OK；`auth_denied` 入日志；query token **不能**旁路 |

### 优先级

1. **P0** W2 critical 最小集  
2. **P1** W4 tol 护栏；生产强制 `TASK4_WEB_TOKEN`  
3. **P2** 失败保存也审计；KB value 截断/哈希；actor+IP  

```bash
# W2
# GET /api/rules → 删 R_VIN_CROSS → PUT → 200
# token
TASK4_WEB_TOKEN=secret uvicorn task4_consistency.web.app:app --port 8766
curl -s localhost:8766/api/rules  # 401
curl -s -H 'Authorization: Bearer secret' localhost:8766/api/rules  # 200
tail out/audit.log
```

**R14 结论:** 审计+可选鉴权 **落地**；**W1 已关**；W2 当时仍 P0 → **R17 已 CLOSED**。开放模式无 auth 仍为部署风险。

---

## Post-Round10 攻击实测（Web 规则保存 · KB 别名）

**Date:** 2026-07-25 · **Rev:** 10.1（历史）· R14 后 **W1 已 CLOSED**  
**Base:** `http://127.0.0.1:8765`

### 🚨 P0（R10 时；R14 复验见上）

| ID | 洞 | 实测 | 修复建议 |
|----|----|------|----------|
| **ADV-W1** | ~~先 write 再 validate~~ → **R14 CLOSED** | 曾：缺 id 残留 runtime + health 500 | 已：temp 校验 + 原子写 |
| **ADV-W2** | UI 可 **删除 critical 规则** | **仍 OPEN（R14）** | 最小规则集 / 禁止删 critical |

### OPEN P1（含 10.1 新挖）

| ID | 洞 | 实测 |
|----|----|------|
| **ADV-W4** | `rel_tol` 无上限 | `0.5` 可存；**1e6 vs 5e5 → consistent** |
| **ADV-W7** | UI 改 `field_aliases` 把 `contract_date` 并进 `reg_date` | 仅合同签署日齐时 **`R_DATE_CROSS=consistent`**（登记日/合同日语义塌缩） |
| **ADV-W9** | 批量 `severity→info` + `on_missing=skip` | PUT **200** 无护栏 |
| **ADV-K3** | 地址别名跨城 | `江苏苏州→江苏南京` → 两城地址 **同串 FP** |
| **ADV-K1b / K9** | `org_aliases` 套进 `normalize_address` | `人保→平安` 后「南京人保大厦」≡「南京平安大厦」**FP**；一汽/上汽同理 |
| **ADV-K10** | 单字/短 key 别名 | `州→X` → `江苏省苏州市` → 含 **`X`** 损坏 |

### OPEN P2

| ID | 洞 | 实测 |
|----|----|------|
| **ADV-W11** | 规则保存无版本/乐观锁 | 连 PUT 后 version=`race-B` last-write-wins |
| **ADV-K8** | 短 key 膨胀 | `路→道路道路道路` → `中山道路道路道路1号` |

### CLOSED

| ID | 结果 |
|----|------|
| ADV-W8 空 rules 列表 | PUT **400**（本轮起拒绝掏空） |
| ADV-W12 YAML `!!python` | 400（safe_load） |
| ADV-W5 默认包 | 不覆盖 `rules_auto_lease.yaml` |
| ADV-W6 rules_path 逃逸 | 非 200 |
| ADV-K1a brand 字段 | 不走 org KB |
| ADV-K2/K4/K5/K6 | section 拒 / 无死循环 / reload OK / 空 key 拒 |
| ADV-K7 plate_prefixes | normalize_plate **未消费**（死配置，无 FP） |

### 优先级（dev）

1. **P0** W1 原子写 + 失败回滚 + health 自愈  
2. **P0** W2 critical 最小集 / 禁删  
3. **P1** W7 保护 `field_aliases` 危险合并（reg≠contract）  
4. **P1** W4/W9 tol 与 severity 护栏  
5. **P1** K3/K9/K10：地址 **禁止** 套 org；短 key / 单字别名拒绝  

### 复现摘要

```bash
# W1 poison → health 500
curl -s -X PUT localhost:8765/api/rules -H 'content-type: application/json' \
  -d '{"content":{"version":1,"field_aliases":{},"rules":[{"type":"exact","field":"vin"}]}}'

# W2 删 VIN 规则：GET /api/rules → 去掉 R_VIN_CROSS → PUT content → POST /api/check

# W7 field_aliases.reg_date 追加 contract_date → R_DATE_CROSS 伪一致
# W4 rel_tol=0.5 → 100万 vs 50万 consistent
# K10 POST /api/kb {"section":"address_aliases","key":"州","value":"X"}
# K9  org_aliases 人保→平安 污染地址
```

**Adversary 结论（10.1）:** P0 **W1/W2 仍未关**。新 P1：**W7 别名语义塌缩**、**W9 降级无护栏**、**K10 单字别名**、**K9 org→地址 FP**。空规则列表已拒（W8）。未改 GOAL。

---



## Post-goal residual（达标抽检）

**抽检命令:** `.venv/bin/python scripts/attack_probes.py` → **exit 0**  
`release_open=0` · `r7_open=0` · **P0 无回潮**（ADV-01/02/03 CLOSED）

### P0 回潮检查

| ID | 结果 |
|----|------|
| ADV-01 brand JV | **CLOSED** 一汽大众≠上汽大众 |
| ADV-02 VIN IOQ | **CLOSED** mixed → uncertain + ocr_fix |
| ADV-03 date DMY/MDY | **CLOSED** ambiguous → None + uncertain |

### 残洞优先级（仅 P2，不挡 goal）

| Pri | ID | 残洞 | 建议 |
|----:|----|------|------|
| 1 | ADV-15 | VIN 无 ISO 校验位；`I×17`→`1×17` 仍 charset-valid → consistent | 加 check digit；失败 → uncertain |
| 2 | ADV-09 | `multi_numeric_all` 首锚非传递；`[100,100.9,99.1]` abs_tol=1 仍 match | 全对或 min/max 跨度检查 |
| 3 | ADV-11 | placeholder/空白 ID 条件必填 → 仍可能 uncertain 非 hard fail | denylist 后 **inconsistent** |
| 4 | ADV-12 | 短姓名形近（张伟/张玮）现 uncertain（改善）；硬匹配策略可再调 | 音形码/编辑距离专用 |
| 5 | — | 真实 OCR/全量 `1.zip` 未接入 | 范围外风险，README 边界 |

### 已关（抽检确认）

ADV-01..08,10,13,14,16..22（含 used-car/价税/buyer/reason_codes/underscore/placeholder engine）

**Adversary 抽检结论:** goal 门 **无 P0 回潮**。残留 **P2 only**，优先级表见上；可不挡交付。

---

## Severity legend

| Sev | Meaning |
|-----|---------|
| **P0** | Correctness hole: real mismatch → consistent (FN) or real match-class different entities → consistent (FP) on critical identity/brand |
| **P1** | High risk: hide real inconsistency as uncertain; metric definition lets FNR stay 0; date/money silent wrong |
| **P2** | Medium: business-tol too loose, short-name OCR, CLI, polish |

---

## Summary table（历史 + 新案）

| ID | Attack | Sev | Status | Class |
|----|--------|-----|--------|-------|
| ADV-01 | Brand JV prefix collapse | P0 | **CLOSED** | was FP |
| ADV-02 | VIN I/O/Q silent merge | P0 | **CLOSED** | was FP |
| ADV-03 | Date DMY/MDY false consistent | P0 | **CLOSED** | was FN |
| ADV-04 | Year-month day=01 | P1 | **CLOSED** | was FN |
| ADV-05 | Low-conf hide mismatch | P1 | CLOSED* | FN-hide |
| ADV-06 | Money `约X万` hide | P1 | CLOSED* | FN-hide |
| ADV-07 | uncertain ∉ FNR | P1 | **CLOSED** (miss_rate) | metric |
| ADV-08 | rel_tol 0.1% | P2 | **CLOSED** | was FP-biz |
| ADV-09 | multi_numeric non-transitive | P2 | **OPEN** | latent FP |
| ADV-10 | ID 15 vs 18 | P2 | **OPEN** | FP-ops |
| ADV-11 | Placeholder ID | P2 | **OPEN** | FN-cond |
| ADV-12 | Short CJK name OCR | P2 | **OPEN** | FP-ops |
| ADV-13 | Money sci-notation | P1 | **CLOSED** | was FP |
| ADV-14 | miss_rate gate | P1 | **CLOSED** | was metric |
| ADV-15 | VIN no check digit / all-I | P2 | **OPEN** | FP-weak |
| ADV-16 | Money underscore `1_280_000→1` | P2 | **OPEN** | FP |
| ADV-17 | Placeholder engine/reg_cert | P2 | **OPEN** | FP-weak |
| ADV-18 | reason_codes 半落地/无 INTERFACE 表 | P2 | **PARTIAL** | traceability |
| ADV-19 | 二手车过户姓名硬不一致 | **P1** | **OPEN** | **FP-ops** |
| ADV-20 | 发票价税合计 vs 不含税 | **P1** | **OPEN** | **FP-biz** |
| ADV-21 | 发票 buyer 不进姓名规则 | **P1** | **OPEN** | **FN** |
| ADV-22 | 并发 RuleEngine 串扰 | P2 | **CLOSED** | concurrency |

---

## ADV-01 — Brand joint-venture prefix collapse (FP) · P0

### Attack goal
Force different manufacturers' JV brands to look identical after normalize.

### Steps
```python
from task4_consistency.normalize.base import normalize_brand
from task4_consistency.models import Application, Document, FieldValue
from task4_consistency.rules.loader import load_rules
from task4_consistency.rules.engine import RuleEngine

assert normalize_brand("一汽大众") == "大众"
assert normalize_brand("上汽大众") == "大众"
assert normalize_brand("东风本田") == normalize_brand("广汽本田") == "本田"

rules = load_rules("configs/rules_auto_lease.yaml")
eng = RuleEngine(rules)
app = Application("atk_brand", [
    Document("d1", "机动车登记证书", {"brand": FieldValue("一汽大众"), "vin": FieldValue("LGXCE4CB0N0123456")}),
    Document("d2", "交强险保单", {"brand": FieldValue("上汽大众"), "vin": FieldValue("LGXCE4CB0N0123456")}),
    Document("d3", "融资租赁合同", {"brand": FieldValue("一汽-大众"), "vin": FieldValue("LGXCE4CB0N0123456")}),
    Document("d4", "发票", {"brand": FieldValue("大众"), "vin": FieldValue("LGXCE4CB0N0123456")}),
])
r = eng.run(app)
assert next(c for c in r.checks if c.rule_id == "R_BRAND_CROSS").verdict.value == "consistent"
```

### Expected hole
Different legal/JV brands collapsed → **false consistent** on major brand rule.

### Measured
`R_BRAND_CROSS = consistent` (`brand 跨单据完全一致`).  
Root: `normalize_brand` strips prefixes `一汽/上汽/东风/广汽/北汽/长安`.

### Fixture draft
`fixtures/applications/app_atk_brand_jv.json` — expected `R_BRAND_CROSS=inconsistent`.

---

## ADV-02 — VIN I/O/Q merge + no check digit (FP) · P0

### Attack goal
Two distinct raw VIN strings become one after OCR “fix”; also accept any charset-17 string.

### Steps
```python
from task4_consistency.normalize.vin import normalize_vin
a, b = "LGXCE4CB0N012345I", "LGXCE4CB0N0123451"
assert normalize_vin(a) == normalize_vin(b) == "LGXCE4CB0N0123451"
assert normalize_vin("IIIIIIIIIIIIIIIII") == "11111111111111111"  # nonsense accepted
assert normalize_vin("OOOOOOOOOOOOOOOOO") == "00000000000000000"
```

Engine: four docs with mixed `…I` / `…1` → `R_VIN_CROSS=consistent`.

### Expected hole
Critical identity field FP; no ISO-3779 check digit; I/O/Q rewrite is unconditional.

### Measured
`R_VIN_CROSS = consistent`. Prior review marked IOQ “by design” — **adversary rejects**: silent merge of different raw strings without uncertain band is identity risk.

### Fixture draft
`app_atk_vin_ioq_fp.json` — label inconsistent **or** expect uncertain if product keeps OCR fix (must not be silent consistent across different raw without audit flag).

---

## ADV-03 — Date DMY forced parse → false consistent (FN) · P0

### Attack goal
US-style MDY `01/02/2023` (Jan 2) vs ISO `2023-02-01` (Feb 1) — real world mismatch — system agrees.

### Steps
```python
from task4_consistency.normalize.date import normalize_date
assert normalize_date("01/02/2023") == "2023-02-01"  # DMY: Feb 1
assert normalize_date("2023-02-01") == "2023-02-01"
# If upstream meant MDY Jan 2, true dates differ; system says same.
```

Engine on 机动车登记证书 `reg_date=01/02/2023` + 融资租赁合同 `reg_date=2023-02-01` → `R_DATE_CROSS=consistent`.

### Expected hole
**FN**: real calendar mismatch classified consistent. Ambiguous `dd/mm` vs `mm/dd` never raised to uncertain.

### Measured
Confirmed consistent. Review Round2 only documented DMY preference as note — **not tested as FN path**.

### Fixture draft
`app_atk_date_mdy_fn.json` + policy: when both day/month ≤12, prefer **uncertain** unless `date_order` configured.

---

## ADV-04 — Year-month only → day=01 (FN) · P1

### Attack goal
Incomplete date coerces to day 1 and matches full ISO day-1.

### Steps
```python
assert normalize_date("2023年1月") == "2023-01-01"
# real reg day may be 15th; written "2023年1月" vs contract "2023-01-01" → false consistent
```

Engine: both sides → `R_DATE_CROSS=consistent`.

### Expected hole
Missing-day should be **uncertain**, not exact ISO with synthetic day.

### Measured
consistent.

---

## ADV-05 — Low confidence hides VIN mismatch (FN-hide) · P1

### Attack goal
Adversarial/OCR pipeline sets `confidence < low_confidence_threshold (0.6)` on mismatched VINs → never inconsistent.

### Steps
```python
# vin A vs B different, both confidence=0.3 on all four docs
# → R_VIN_CROSS = uncertain  ("存在低置信度值")
```

### Expected hole
Ops/metric: real fraud/mismatch becomes uncertain; evaluate excludes uncertain from FNR → **invisible miss**.

### Measured
`R_VIN_CROSS=uncertain` on clear VIN conflict.

### Note
By design for OCR noise — but **no severity escalation / no separate “blocked_low_conf_mismatch” signal**. Critical rules should still surface raw inequality when both norms parse.

---

## ADV-06 — Money `约12.5万` normalize None hides gap (FN-hide) · P1

### Attack goal
Common OCR phrase with 约 + 万 fails parse; paired with any other amount → uncertain, not inconsistent.

### Steps
```python
from task4_consistency.normalize.money import normalize_money
assert normalize_money("约12.5万元整") is None
assert normalize_money("约12.5万") is None
# Engine: contract "约12.5万" vs invoice "100" → R_AMOUNT_TOL=uncertain
# (not inconsistent despite 100 vs ~125000)
```

### Expected hole
Any unparseable money + parseable other → uncertain. Attacker/OCR can cloak amount fraud with `约…万`.

### Measured
`R_AMOUNT_TOL=uncertain` message `标准化/格式校验失败`.

Contrast: `大约128000` / `128000左右` still parse via first-number fallback (inconsistent correctly) — **unit-word path is the crack**.

---

## ADV-07 — Metric: uncertain prediction never raises FNR · P1

### Attack goal
Prove FNR can stay 0 while system fails to flag labeled inconsistencies.

### Steps
```python
from task4_consistency.evaluate import evaluate_report, compute_metrics
from task4_consistency.models import Verdict
# report predicts uncertain for R_VIN_CROSS; label = inconsistent
pairs, _ = evaluate_report(report, {"R_VIN_CROSS": "inconsistent"})
m = compute_metrics(pairs, [0.0])
assert m.false_negative == 0
assert m.false_negative_rate == 0.0
assert m.uncertain_when_labeled == 1
```

Cherry-pick fixtures: only label easy TN/TP; hide_fn with invalid VINs → uncertain; FPR=FNR=0.

### Expected hole
GOAL “漏报 ≤3%” can pass while many true inconsistencies are uncertain. No `fn_or_uncertain_rate` / critical-only FNR.

### Measured
Confirmed on synthetic 3-fixture dir: FNR=0 with hide_fn uncertain.  
Live suite: all 20 labeled inconsistent are decisive TP — **suite does not stress uncertain-hide path**.

### Review must respond
- Accept secondary metric: `miss_rate = (FN + uncertain|expected_inconsistent) / labeled_inconsistent`  
- Or require critical rules: raw-mismatch → inconsistent even if conf low / norm fail (with flag)

---

## ADV-08 — rel_tol 0.1% money admit · P2

### Attack goal
Business-unacceptable yuan drift still consistent.

### Measured
| 合同 | 发票 | result |
|------|------|--------|
| 1000000 | 1001000 | **consistent** (rel≈0.001) |
| 100000 | 100100 | **consistent** (~100 元) |

Config: `abs_tol: 1.0`, `rel_tol: 0.001` (OR logic in `within_tolerance`).

### Suggest
`match = abs_diff <= abs_tol OR (rel_diff <= rel_tol AND abs_diff <= abs_cap)`; default `rel_tol=0` for lease amounts.

---

## ADV-09 — multi_numeric first-anchor non-transitive · P2

### Attack goal
With ≥3 values, pairwise extremes can exceed tol while each vs first passes.

### Measured
```python
multi_numeric_all(["100.0","100.9","99.1"], abs_tol=1.0)  # match=True
# but 100.9 vs 99.1 abs_diff=1.8 > 1.0
multi_numeric_all(["1000000","1000999","999001"], rel_tol=0.001)  # match=True
```

Current `R_AMOUNT_TOL` only 2 docs → latent until 3rd amount source added.

---

## ADV-10 — ID 15 vs 18 no link · P2

### Measured
Valid 18-digit vs corresponding 15-digit body → `R_ID_EXACT=inconsistent` (string exact after norm).  
Same person legacy docs → false ops alarm.

### Suggest
Optional 15→18 upgrade when birth/region align; else uncertain not inconsistent.

---

## ADV-11 — Placeholder ID satisfies “present” as uncertain · P2

### Measured
| raw id | R_ID_REQUIRED_IF_AMOUNT |
|--------|-------------------------|
| `"   "` / `\\t` / ZWSP | uncertain (格式校验失败) |
| `无` / `N/A` / `null` | uncertain |

Business want **inconsistent** (missing effective ID when amount present). Uncertain again dilutes FNR.

---

## ADV-12 — Short name OCR · P2

### Measured
`张伟` vs `张玮` score=0.5 → **inconsistent** (adaptive band for len≤2 needs score≥0.63).  
形近字 still hard-fail — review Round2 residual; still open.

---

## Reproduce all (one shot)

```bash
cd /home/lhjysyx/xiaopeng_comp
.venv/bin/python <<'PY'
# paste probes from ADV-01..07; assert holes still open
from task4_consistency.normalize.base import normalize_brand
from task4_consistency.normalize.vin import normalize_vin
from task4_consistency.normalize.date import normalize_date
from task4_consistency.normalize.money import normalize_money
assert normalize_brand("一汽大众") == normalize_brand("上汽大众") == "大众"
assert normalize_vin("LGXCE4CB0N012345I") == normalize_vin("LGXCE4CB0N0123451")
assert normalize_date("01/02/2023") == normalize_date("2023-02-01") == "2023-02-01"
assert normalize_date("2023年1月") == "2023-01-01"
assert normalize_money("约12.5万") is None
print("ADV core holes still open")
PY
```

---

## Demands for `review` (must answer / retest)

1. **Re-open or explicitly waive with risk accept** ADV-01 brand JV FP — Round2 PASS claimed brand PASS; this is **identity-class FP**.
2. **Reclassify VIN IOQ** from “by design HOLD” → either uncertain-on-rewrite, check digit, or documented residual **P0/P1** with fixture.
3. **Retest date** ADV-03 as **FN** (not “DMY note”): fixture where MDY-true differs from DMY-parse.
4. **Metric honesty**: add or reject `miss_rate` including uncertain|expected_inconsistent; document that FNR=0 ≠ zero miss.
5. **Money 约+万** ADV-06: must not be uncertain-hide; parse or explicit money_uncertain reason code.
6. **Low-conf critical mismatch** ADV-05: propose behavior for critical severity (still emit inconsistent+flag?).
7. **Confirm** live evaluate still THRESHOLD PASS **after** adding adversary fixtures with correct labels (expect FNR/FPR move or coverage drop).
8. **Do not** claim commercial-hard until ADV-01/02/03 closed or risk-accepted in DELIVERY/README.

### Retest commands for review
```bash
.venv/bin/pytest -q
.venv/bin/python -m task4_consistency evaluate fixtures/applications -o out/metrics_adv.json
# after fixture add:
.venv/bin/python -c "from task4_consistency.normalize.base import normalize_brand as b; print(b('一汽大众'), b('上汽大众'))"
```

---

## Out of adversary scope
- Mass OCR on `1.zip`
- Rewriting engine in this role (dev’s job)
- Changing review’s PASS without their re-eval

---

## Round3 新案详情

### ADV-13 — Money scientific notation truncation (FP) · P1 · OPEN

#### Attack goal
OCR/export may emit `1.28e6` meaning 1_280_000. Parser takes only leading float → `1.28`.

#### Steps
```python
from task4_consistency.normalize.money import normalize_money
assert normalize_money("1.28e6") == "1.28"   # BUG: should be 1280000 or None
assert normalize_money("1e6") == "1"
# Engine: contract "1.28e6" vs invoice "1.28" → R_AMOUNT_TOL=consistent  (FALSE)
# Engine: contract "1.28e6" vs invoice "1280000" → inconsistent (wrong reject too)
```

#### Expected hole
False consistent on wrong scale; also false inconsistent vs true full amount.

#### Measured (post R3)
```
1.28e6 -> 1.28
R_AMOUNT vs 1.28 = consistent   # FP
R_AMOUNT vs 1280000 = inconsistent
```

#### Suggest
Reject `e/E` scientific form (return None → uncertain) or parse full float/`Decimal`.

#### Fixture draft
`app_atk_money_sci.json` — expected uncertain or inconsistent with clear label policy.

---

### ADV-14 — miss_rate not gated by pass_thresholds · P1 · OPEN

#### Attack goal
Even after miss_rate exists, CLI/evaluate **THRESHOLD PASS** only checks cov/FPR/FNR. Suite can hide all labeled inconsistent as uncertain → FNR=0, miss_rate=1, still PASS.

#### Steps
```python
# 20× expected inconsistent → uncertain; 100× consistent → consistent
# → coverage≈0.83, FPR=0, FNR=0, miss_rate=1.0
# pass_thresholds all True for the three classic gates
```

#### Measured
```
pass_thresholds = {
  'coverage>=0.80': True,
  'false_positive_rate<=0.05': True,
  'false_negative_rate<=0.03': True,
}
# no miss_rate<=X key
```

#### Suggest
Add `miss_rate<=0.10` (or critical-only) into `pass_thresholds` + CLI fail.

---

### ADV-15 — VIN charset-only, no check digit · P2 · OPEN

#### Measured
`normalize_vin("IIIIIIIIIIIIIIIII")=="11111111111111111"`；四单同值 → `R_VIN_CROSS=consistent`。  
ADV-02 仅修「混合 I/1 silent merge」；全同垃圾 VIN 仍放行。

---

## Round3 re-verify demands for `review`

1. **Confirm** ADV-01/02/03 CLOSED（adversary 复验 exit 0 同意）。
2. **Open** ADV-13 P1 money sci-notation — 必须补测；建议升 dev 修复。
3. **Open** ADV-14 P1 — `pass_thresholds` 纳入 `miss_rate` 或书面 risk-accept。
4. ADV-08/09/15 仍 OPEN P2 — 可进下一轮，不阻塞 P0。
5. 根据复验更新 verdict：建议 **PASS_WITH_NOTES**（P0 关；P1 新洞 ADV-13/14 未关）或维持 PASS 但 NOTES 强制列 ADV-13/14。
6. 写/更新 `docs/ATTACK_RESPONSE.md` 或 `docs/REVIEW.md` Round3.1。

```bash
.venv/bin/python scripts/attack_probes.py
.venv/bin/python -c "from task4_consistency.normalize.money import normalize_money as m; print(m('1.28e6'), m('1e6'))"
```

**Adversary Round3 verdict:** 原 P0 **CLOSED 确认**。新 **ADV-13/14 P1 OPEN**。指标仍可被 miss_rate 旁路 PASS。

---

## Round5 新案（轻量）

### ADV-16 — Money underscore thousands (FP) · P2 · OPEN

```python
normalize_money("1_280_000") == "1"   # truncates at _
# contract 1_280_000 vs invoice 1 → R_AMOUNT_TOL=consistent  (FP)
# vs 1280000 → inconsistent (false reject)
```

**Suggest:** strip `_` thousand separators before parse, or reject `_` → None/uncertain.

### ADV-17 — Placeholder engine / reg_cert · P2 · OPEN

```python
# engine_no="无" ×3 docs → R_ENGINE_CROSS=consistent
# reg_cert_no="N/A" ×2 → R_REG_CERT_CROSS=consistent
```

**Suggest:** denylist `无/N/A/***/—/-` → normalize None → uncertain（同 ID placeholder 策略）。

**Round5 adversary:** `release_open=0` 确认。仅 P2 新洞，不挡 release。

---

## Round7 新案（并行攻击）

### ADV-18 — reason_codes 半落地 · P2 PARTIAL

**Round7 中期复测:** 字段已存在；VIN 冲突 → `['VIN_MISMATCH']`；歧义日期 → `['NORMALIZE_FAIL','DATE_AMBIGUOUS']`；低置信比较 → `['LOW_CONF','LOW_CONF_COMPARED','VIN_MISMATCH']`。

**仍 OPEN:**
- `docs/INTERFACE.md` **无** reason_codes 枚举表（brief 第7条）
- 无允许码集合校验（自由字符串可漂）
- 价税/过户场景无专用码（`AMOUNT_TAX_GAP` / `NAME_TRANSFER_SUSPECTED`）

---

### ADV-19 — 二手车过户姓名 · P1 · OPEN

**目标:** 登记证仍 原车主，合同/保单/身份证 新承租人 — 合法过户场景。

**实测:** `R_NAME_FUZZY=inconsistent`（score=0），无 `used_car` / `name_change_ok` / 存疑策略；与 VIN 一致并存时业务应 **uncertain 或可配置 skip**，非一律硬不一致。

**期望:** 规则包 `mode: used_car` 或条件规则：VIN 一致且姓名不一致 → uncertain + `NAME_TRANSFER_SUSPECTED`；或 docs 去掉登记证参与姓名 cross。

**Fixture 草案:** `app_used_car_name_transfer.json` expected `R_NAME_FUZZY=uncertain`。

---

### ADV-20 — 发票价税合计 vs 不含税 · P1 · OPEN

**目标:** 融资额常对齐 **价税合计**；发票 OCR 常抽 **金额(不含税)** 或混排「金额/税额/价税合计」。

**实测:**
```text
normalize_money("金额:113207.55 税额:14716.98 价税合计:127924.53") → 113207.55  # 取首数
合同 financed=127924.53 vs 发票 amount/invoice_amount=113207.55 → inconsistent
diff 恰为税额 14716.98 — 无 TAX_EXCLUDED 识别
```

**期望:** 字段拆分 `amount_excl_tax` / `amount_incl_tax` / `tax_amount`；或容差/规则允许 13% VAT 关系；混排串优先解析「价税合计」。

**Fixture:** `app_invoice_tax_total.json`。

---

### ADV-21 — 发票 buyer_name 姓名规则漏检 · P1 · OPEN

**目标:** 发票购买方与承租人完全不同应被抓。

**实测:** `R_NAME_FUZZY.docs` = 登记证/交强险/合同/身份证，**无发票**；`buyer_name` 别名在 person 但规则不采发票 → 姓名 **consistent**，购买方错名 **FN**。

**期望:** docs 加入 `发票` 或独立 `R_BUYER_NAME`；reason `BUYER_LESSEE_MISMATCH`。

---

### Round7 给 dev 的必回应清单

1. ADV-18 落地 `reason_codes: list[str]` + 枚举表  
2. ADV-19 二手车姓名策略（uncertain 或可配置）+ fixture  
3. ADV-20 价税字段/解析优先级  
4. ADV-21 发票购买方纳入姓名/身份交叉  
5. 回归：`scripts/attack_probes.py` 追加 ADV-18..21 断言（可先 warn 不进 release_open）
