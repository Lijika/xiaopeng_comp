# 配置方案 — 融资租赁跨单据一致性（终稿）

## 1. 配置分层

| 层 | 文件 | 职责 |
|----|------|------|
| 规则包 | `configs/rules_auto_lease.yaml` | 版本化业务规则（package/version/changelog） |
| 运行时规则 | `configs/runtime_rules.yaml` | Web UI 热改副本；存在则优先加载 |
| 实体知识库 | `configs/kb/entity_kb.json` | 地址/机构/车牌别名，可 CRUD |
| 评估标签 | `fixtures/labels/expected_verdicts.json` + fixture 内嵌 `expected_verdicts` | 指标复现 |

## 2. 规则包顶层字段

```yaml
package: auto_lease
version: "1.9.0"
changelog: ["..."]
low_confidence_threshold: 0.6
default_require_all_docs: null   # null=critical 自动 require
date_order: null                  # null|DMY|MDY
vin_fix_ioq: true
vin_strict_check_digit: false     # 生产可 true
expand_id15_to_18: true
critical_low_conf_compare: true
field_aliases: { ... }
rules: [ ... ]
```

### 2.1 field_aliases

规范字段 → 同义名列表。引擎在单据上按别名解析字段。

示例：`owner_name` 含 `lessee_name` / `insured_name` / `buyer_name`。

### 2.2 规则类型

| type | 要点参数 | 行为 |
|------|----------|------|
| `exact` | `field`, `docs`, `on_missing`, `require_all_docs` | 标准化后全等 |
| `fuzzy` | `threshold`, `uncertain_band`, `transfer_name_policy` | 相似度；近邻/过户可 uncertain |
| `numeric_tolerance` | `abs_tol`, `rel_tol` | 金额容差（金融 rel 宜严） |
| `list_contains` | `list_field`, `item_field` | 元素 ∈ 列表（元素同 item 类型 normalize） |
| `conditional_required` | `if_field_present`, `required_field` | 条件必填 |

通用：`severity`（critical/major/minor/info）、`on_missing`（uncertain/skip/inconsistent）。

### 2.3 过户姓名策略（ADV-19）

```yaml
- id: R_NAME_FUZZY
  transfer_name_policy: uncertain   # 推荐：二手车登记旧主 vs 承租新主
  transfer_old_docs: [机动车登记证书]
  transfer_new_docs: [交强险保单, 融资租赁合同, 身份证, 发票]
```

干净「一侧旧主、一侧新主且内部一致」→ **uncertain** + `USED_CAR_NAME_TRANSFER`，而非直接 inconsistent。

## 3. 知识库（KB）

路径：`configs/kb/entity_kb.json`

| section | 用途 | 接入点 |
|---------|------|--------|
| `address_aliases` | 地址书写变体 | `normalize_address` |
| `org_aliases` | 机构全称→简称 | 地址/品牌链路可扩展 |
| `plate_prefixes` | 号牌省份城市提示 | 查询/展示（可扩展校验） |

维护方式：

- Web「实体知识库」页  
- `GET/POST/DELETE /api/kb`  
- 直接编辑 JSON 后 `POST /api/kb/reload` 或重启进程  

**注意：** 错误别名可导致假一致；变更后应跑 `evaluate` + 相关 fixture。

## 4. 阈值建议（融资租赁）

| 场景 | 建议 |
|------|------|
| 金额 `rel_tol` | `0.0001`（0.01%）+ `abs_tol: 1` 分位 |
| 姓名 `threshold` | `0.88`，`uncertain_band: 0.12` + 形近字表 |
| 地址 fuzzy | `0.85` 左右 |
| 低置信 | `low_confidence_threshold: 0.6`；critical 可仍比对 |
| VIN | 默认 IOQ 纠错但合并不同 raw→uncertain；校验位生产可开 |

## 5. 生效顺序

1. CLI：`-c` 指定路径  
2. Web check：优先 `runtime_rules.yaml`，否则默认包  
3. KB：进程内单例；Web 写入后 `reload_kb()`  

### 5.1 KB 稳定 API（Round17）

```python
import task4_consistency.kb as kb
kb.add_alias("org_aliases", "某某融资租赁有限公司", "某某金租")
kb.list_section("org_aliases")
kb.remove_alias("org_aliases", "某某融资租赁有限公司")
kb.apply_aliases("江苏省南京市…", "address_aliases")
# 或显式：kb.get_kb() / kb.reload_kb(path)
```

分区仅：`address_aliases` | `org_aliases` | `plate_prefixes`。短 key / 跨城地址别名拒绝（ADV-K10/K3）。  

## 6. 版本协商

报告字段：`rule_package` / `rule_config_version` / `rule_changelog`。  
评估与生产流水线应记录规则文件哈希 + version + KB version。

## 7. Critical 语义指纹（Round16 终裁 · 代码权威）

权威：`task4_consistency/rules/critical_guard.py` 常量 `CRITICAL_FINGERPRINTS`。  
**YAML 不可关闭。** Web `PUT /api/rules` 与 `load_rules` 均 enforce；失败 **零写** active runtime。

| rule.id | field | type | severity | require_all_docs | on_missing | docs_min（runtime ⊇） |
|---------|-------|------|----------|------------------|------------|----------------------|
| `R_VIN_CROSS` | `vin` | `exact` | `critical` | `true` | uncertain\|inconsistent | 机动车登记证书, 融资租赁合同 |
| `R_ENGINE_CROSS` | `engine_no` | `exact` | `critical` | `true` | uncertain\|inconsistent | 机动车登记证书 |
| `R_ID_EXACT` | `id_number` | `exact` | `critical` | `true` | uncertain\|inconsistent | 融资租赁合同, 身份证 |

可扩 docs 超集；**禁止**删 id / 改 field·type·severity / 掏 docs / `on_missing=skip`。  
无 break-glass：要改最小集 → 改代码指纹表 + 默认 YAML + 发版。

### 规则保存原子语义（W1）

1. parse → schema/policy/fingerprint 全过  
2. 写锁内：同目录 `.yaml.tmp` → flush/fsync → `os.replace`  
3. 任一步失败：**不碰** `runtime_rules.yaml`（无 `write_text` 回滚）  
4. 每请求 `load_rules(active)`；毒化文件隔离为 `*.yaml.bad`

### API error 码（detail.error）

| code | 含义 |
|------|------|
| `critical_rule_missing` | 缺三剑客之一 |
| `critical_semantic_tamper` | field/type/severity/require_all_docs 偏离 |
| `critical_docs_stripped` | docs 不含 docs_min |
| `critical_on_missing_skip` | critical 上 on_missing=skip |
| `invalid_yaml` / `rules_schema_invalid` | YAML/schema |
| `rules_save_failed` | 写盘失败（active 仍旧） |
