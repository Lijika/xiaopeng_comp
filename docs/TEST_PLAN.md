# Test Plan — Task4 Consistency（Round11）

**Owner:** Reviewer + Tester  
**Scope:** 合成 fixture + 单元/回归/对抗 + Web/KB；**不含**全量真实 OCR。  
**Last run:** 2026-07-25 Round11 — pytest **91 passed**；evaluate **THRESHOLD PASS**（cov **0.965** / FPR0 / FNR0 / miss0）；`attack_probes` **release_open=0**；fixtures **48**；D1/D3/D6/D7 见 `docs/REVIEW.md`

### Web / KB / 文档回归（Round10–11）

```bash
.venv/bin/pip install httpx -q   # TestClient（若缺）
.venv/bin/pytest -q tests/test_web_kb.py
bash scripts/run_web.sh          # http://127.0.0.1:8765/
# 手测清单：
#   D6: 选 fixture → 运行校验 → 看三态表/HTML
#   D3: 规则页改 YAML 保存 → 再校验（用 runtime_rules）→ reset
#   D1: KB 加地址别名 → 地址字段归一变化可演示
# D7 文档抽检：docs/DEPLOY.md CONFIG_GUIDE.md EVALUATION_REPORT.md 存在且可跟随操作
```

| Web 检查 | 标准 | Round11 |
|----------|------|---------|
| `GET /` | 200 | PASS |
| `GET /api/health` | ok | PASS |
| `GET /api/fixtures` | n≥40 | **48** |
| `POST /api/check` | report+html | PASS |
| `GET/PUT /api/rules` | runtime 生效/校验 400 | PASS |
| `GET/POST/DELETE /api/kb` | CRUD + address normalize | PASS（plate_prefixes 未接 normalizer） |

## 1. 目标与门槛

| 门槛 | 标准 | Round11 |
|------|------|---------|
| 单元/回归 | `pytest -q` 全绿 | **91 passed** |
| 经典指标 | coverage≥0.80，FPR≤0.05，FNR≤0.03 | **PASS**（0.965/0/0） |
| **miss_rate 门禁** | miss_rate≤0.10 | **PASS**（0.0） |
| 样本 | fixtures≥40 | **48** |
| 标签 | mismatches=0 | OK |
| 对抗 release | release_open=0，无开放 P0 | **0** |
| 文档 D7 | DEPLOY+CONFIG+EVAL 存在 | **PASS** |

## 2. 回归命令（必跑 / CI）

```bash
cd /home/lhjysyx/xiaopeng_comp

# A. 全量单测
.venv/bin/pytest -q

# B. 对抗 + 硬化子集
.venv/bin/pytest -q \
  tests/test_adversary.py \
  tests/test_round3_adv.py \
  tests/test_p0_p1_regressions.py \
  tests/test_round2.py

# C. 攻击探针（release gate）
.venv/bin/python scripts/attack_probes.py
# expect: release_open=0

# D. fixture 评估（含 miss_rate 门槛）
.venv/bin/python -m task4_consistency.cli evaluate fixtures/applications \
  -c configs/rules_auto_lease.yaml -o out/metrics.json
# expect: THRESHOLD PASS; pass_thresholds includes miss_rate<=0.1

# E. 冒烟报告
.venv/bin/python -m task4_consistency.cli check \
  fixtures/applications/app_consistent_01.json \
  -c configs/rules_auto_lease.yaml \
  -o out/report_ok.json --markdown out/report_ok.md --html out/report_ok.html

# F. demo（可选）
bash scripts/demo.sh
```

**CLI 退出码:** 0 ok · 1 threshold/usage · 2 strict inconsistent · 3 not found · 4 bad JSON · 5 bad config · 6 runtime  

**可选:** `--max-miss-rate` 覆盖默认 0.1。

## 3. 对抗门禁矩阵（Round5）

| ID | 期望 | 验证 |
|----|------|------|
| ADV-01 brand JV | inconsistent | adversary / round3 / probes / `app_atk_brand_jv` |
| ADV-02 VIN IOQ | uncertain + ocr_fix | round3 / probes / `app_atk_vin_ioq` |
| ADV-03 date ambig | None → uncertain | adversary / probes / `app_atk_date_ambig` |
| ADV-04 year-month | None | probes |
| ADV-05 low conf critical | inconsistent + flags | probes / lowconf fixture |
| ADV-06 约万 | parse + approx → uncertain 策略 | probes / money_approx fixture |
| ADV-07/14 miss_rate | 定义 + **≤0.1 gate** | `test_adv14_*` / evaluate |
| ADV-08 rel_tol | 0.0001；0.1% inconsistent | `test_a2_*` / probes |
| ADV-13 sci money | `1.28e6`→1280000；vs 1.28 inconsistent | `test_adv13_*` / probes |
| ADV-09 numeric | 探针 CLOSED；全对可选 P2 | probes |

## 4. 核心用例（摘要）

### Normalize
VIN/IOQ+ocr_fix · date ambig/unambig/年月 · money 万/约/科学计数 · brand JV 前缀 · plate_list · id checksum  

### Rules
exact/fuzzy/tol/list/conditional · require_all_docs · skip ∉ coverage · critical low_conf compare · flags  

### Evaluate
四门 pass_thresholds · miss_rate · 35 fixtures 零错配 · n_inc≥15  

### CLI / Report
错误码 3/4/5 · HTML 复核清单 · markdown/json  

## 5. Fixture 规模

| 项 | 值 |
|----|-----|
| applications | **35** |
| 含 atk / bad / consistent / uncertain | 见 `fixtures/applications/` |
| 标签 | 内嵌 `expected_verdicts` |

## 6. 测试文件

| 文件 | 作用 |
|------|------|
| `tests/test_adversary.py` | ADV-01/08/13/14 等对抗钉 |
| `tests/test_round3_adv.py` | R3 P0 + miss_rate 单元 |
| `tests/test_round2.py` | band/skip/html |
| `tests/test_p0_p1_regressions.py` | R1 回归 |
| `tests/test_normalize_*.py` / `test_match.py` / `test_rules_engine.py` | 基础 |
| `tests/test_evaluate.py` / `test_cli.py` / `test_report.py` | 评估/CLI |
| `scripts/attack_probes.py` | release 探针 |

## 7. 通过准则（本轮）

- [x] pytest 68 绿  
- [x] attack_probes release_open=0  
- [x] evaluate 四门 THRESHOLD PASS  
- [x] ADV-13/14 行为与测例一致  
- [ ] P2 backlog：VIN check digit、multi 全对、15/18 证、真实 OCR  

## 8. 非范围

- 任务1–3 视觉训练  
- 全量影像 OCR  
- 生产 SLA 压测 baselining  
