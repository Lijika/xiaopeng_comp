# Attack Response — Round3.1（adversary 复验）

**Author:** Reviewer + Tester (`review`)  
**Date:** 2026-07-25  
**Constraint:** **不改业务实现**  
**Source:** `docs/ATTACK_CASES.md` Round3 复验 + ADV-13/14  

---

## 0. Verdict 修订

| 项 | 结论 |
|----|------|
| Round3 初版 **PASS** | **降级 → PASS_WITH_NOTES** |
| 原因 | P0（ADV-01/02/03）**确认关闭**；新开 **ADV-13/14 P1** 未关 |
| 是否 FAIL | **否** — 系统可演示；fixture 阈值仍绿；非身份 P0 回潮 |
| 商业硬指标 / THRESHOLD=零漏检 | **不可宣称**（miss_rate 未 gate；金额科学计数 FP） |

**与 adversary 建议对齐：PASS_WITH_NOTES。**

---

## 1. P0 关闭确认（同意 adversary exit 0）

| ADV | 复验 | Reviewer |
|-----|------|----------|
| 01 brand JV | inconsistent；norm 一汽≠上汽 | **CLOSED 确认** |
| 02 VIN IOQ | uncertain + ocr_fix | **CLOSED 确认** |
| 03 date ambig | None + uncertain | **CLOSED 确认** |
| 04 year-month | None + uncertain | **CLOSED 确认** |
| 07 miss_rate 存在 | fnr=0 时 miss_rate 可=1 | **CLOSED 作为“指标已暴露”**；**未** gate → 见 ADV-14 |

```bash
.venv/bin/python scripts/attack_probes.py   # exit 0, p0_open=0
```

---

## 2. 新 OPEN — 强制回应

### ADV-13 P1 — `1.28e6` → `1.28` 金额假一致（FP）

| 项 | 回应 |
|----|------|
| **(A)** | **承认，不反驳。** |
| **(B)** | **P1 OPEN**（金额 FP；非 VIN/品牌身份 P0，但金融正确性严重）。 |
| **(C)** | 见下方实测。 |

**实测：**

```text
normalize_money('1.28e6') == '1.28'
normalize_money('1.28E6') == '1.28'
normalize_money('1.28e+6') == '1.28'
normalize_money('1280000') == '1280000'

R_AMOUNT_TOL: contract 1.28e6 vs invoice 1.28 → consistent  # FP
# vs 1280000 would be inconsistent (wrong reject of true amount)
```

```bash
.venv/bin/python -c "from task4_consistency.normalize.money import normalize_money as m; print(m('1.28e6'), m('1e6'), m('1280000'))"
# 1.28 1 1280000
```

**根因（审查）：** money 解析用正则抓「首段」`-?\d+(?:\.\d+)?`，在 `e` 处截断，指数丢弃。  

**Dev 建议（不实施）：**  
- 拒收含 `[eE]` 的串 → None → uncertain；或  
- 用 `Decimal` 解析完整科学计数 → `1280000`。  
- fixture `app_atk_money_sci.json` + 单测。

---

### ADV-14 P1 — `pass_thresholds` 不含 `miss_rate`

| 项 | 回应 |
|----|------|
| **(A)** | **承认。** miss_rate **已计算、已进 JSON/definitions**，但 **不进** `pass_thresholds` / CLI THRESHOLD 判定。 |
| **(B)** | **P1 OPEN**（指标诚实 / 门禁旁路）。ADV-07「有 miss_rate」≠「门槛强制」。 |
| **(C)** | 合成：expected=inconsistent→uncertain ×2 + consistent×1 → **FNR=0, miss_rate=1.0**；`pass_thresholds` 无 miss 键；FNR 门槛仍 True。 |

**实测：**

```text
pass_thresholds = {
  'coverage>=0.80': ...,
  'false_positive_rate<=0.05': True,
  'false_negative_rate<=0.03': True,
}
# no miss_rate<=...
live suite: miss_rate=0.0 + THRESHOLD PASS  # 标签未压 hide 路径
```

**Dev 建议：** `pass_thresholds["miss_rate<=0.10"]= miss_rate<=0.10`（或 critical 子集）；CLI 失败打印 miss。  
**Risk-accept 替代：** README/INTERFACE 明文「THRESHOLD 不含 miss_rate；发布门禁须人工看 miss_rate」——在 accept 前仍标 **P1 OPEN**。

---

### ADV-08 / ADV-15 P2 — 仍 OPEN

| ADV | 实测 | 分级 |
|-----|------|------|
| **08** rel_tol=0.001 | 1_000_000 vs 1_001_000 → match | **P2 OPEN** |
| **15** VIN 无校验位 | `I×17`→`1×17`，四单同 → **consistent** | **P2 OPEN**（ADV-02 只修混合 raw） |

不阻断 P0；进下一轮 hardening。

---

### ADV-05/06 备注

- 探针脚本可能标 CLOSED/OPEN 因实现漂移；行为上 low-conf / 约万 仍属 **P1 residual**（hide 或解析）。  
- 本轮以 **ADV-13/14** 为强制新开项。

---

## 3. 指标与测试快照（复验时）

```text
pytest: 62 passed
attack_probes: p0_open=0 exit 0
evaluate: coverage≈0.9543 FPR=0 FNR=0 miss_rate=0 THRESHOLD PASS
# THRESHOLD 仍不检查 miss_rate
```

---

## 4. 对 Manager / Dev 清单

1. **确认** ADV-01/02/03 CLOSED — Reviewer **同意**。  
2. **打开** ADV-13 — 科学计数金额 FP — **优先修**。  
3. **打开** ADV-14 — miss_rate 入 `pass_thresholds` 或书面 risk-accept。  
4. ADV-08/15 P2 — backlog。  
5. **Verdict = PASS_WITH_NOTES**（本文件 + `docs/REVIEW.md`）。

---

## 5. 签字

- P0 三洞：**关闭确认**  
- ADV-13/14：**承认 OPEN P1，有复现**  
- Round3 **PASS → PASS_WITH_NOTES**  
- 业务码：**未改**
