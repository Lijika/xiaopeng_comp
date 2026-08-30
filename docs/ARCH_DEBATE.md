# ARCH_DEBATE — 任务4 规则引擎 & 匹配算法

- Role: Architecture Discussant (`arch`)
- Date: 2026-07-25
- Scope: 议题 A/B 仅；**不改业务大代码**
- Code base: `task4_consistency/rules/engine.py` type 分发；`match/{exact,fuzzy,numeric,list_ops}`；`normalize/*`；`configs/rules_auto_lease.yaml`

---

## 议题 A：规则引擎扩展性

### 现状（事实）

- YAML 声明 + `RuleDef.type` 闭集：`exact | fuzzy | numeric_tolerance | list_contains | conditional_required`
- `RuleEngine._eval_rule` 硬 `if/elif` 分发；未知 type → `uncertain`
- 加新规则类型 = 改 loader allowlist + engine 分支 + 单测 + 文档
- 规则彼此独立顺序执行；无依赖图、无共享中间结果、无表达式

### 正方主张（本轮立场）

**主张：MVP 继续 YAML + 固定 type；商业级先做「策略注册表」薄扩展；拒绝 CEL/通用 DSL 与规则图（现阶段）。**

理由：
1. **业务规则形态稳定**：跨单据字段比对 ≈ 5 类谓词，不是任意业务流编排。CEL/DSL 解决「任意表达式」问题，本赛题不存在。
2. **可解释硬约束**：每条 check 必须有 snapshot/diff/severity。通用表达式引擎输出难映射到三态 + 中文 diff，调试成本爆炸。
3. **配置变更 ≠ 引擎变更**：新业务规则 90% 是「换 field/docs/threshold」，YAML 已够；真正缺的是 **type 可插拔**（策略注册），不是表达式语言。
4. **规则图过早**：当前无「规则 B 依赖规则 A 的 verdict」真实需求；`conditional_required` 已覆盖「字段存在→另一字段必填」。图调度带来拓扑/环检测/缓存失效，收益为零。
5. **商业可维护路径**：
   - P0：保持 YAML 契约稳定
   - P1：`type → handler` 注册表（策略模式），engine 不再 if 链
   - P2：可选「规则包」多文件 merge + schema 校验增强
   - **永不默认上 CEL**，除非出现跨字段算术/集合逻辑 >3 条且无法用新 type 表达

### 反方攻击

| 攻击 | 力度 | 回应 |
|------|------|------|
| 「if 链会腐烂；每加 type 触核心」 | 真 | 所以 P1 注册表；但腐烂速度与规则类型数成正比，当前 5 类，半年内 ≤10 类可接受 |
| 「业务要热更新复杂条件，YAML 字段爆炸」 | 中 | `RuleDef` 已有 `extra` 逃生舱；真复杂时加 **专用 type**（如 `date_order`）比塞 CEL 更可测 |
| 「CEL 业界成熟、审计友好」 | 弱 | 审计友好依赖可读 AST + 绑定语义；我们已有 rule_id/message/snapshot，比 CEL trace 更贴业务 |
| 「规则图可做 short-circuit / 共享 collect」 | 弱 | 单申请 10 单×20 字段，全量跑完 <1ms 级；优化图是过度设计 |
| 「插件注册无版本/签名=供应链风险」 | 中 | 商业级：只允许 in-process 注册 + YAML type 白名单；禁止任意路径 import |

### 决议 A

| 项 | 决议 |
|----|------|
| 主路径 | **YAML + 固定语义 type** 保留为对外契约 |
| 引擎内部 | **策略注册表**（`register(type, handler)`）替换 if 链 — 演进，非重写 |
| CEL / 通用 DSL | **否决（当前+近 2 轮迭代）** |
| 规则依赖图 | **否决**；仅当出现 ≥3 条真实跨规则依赖时重开 |
| 新 type 门槛 | 先证明「现有 5 type + normalizer 改不动」→ 再加 type；禁止为单 fixture 开 type |

---

## 议题 B：一致性匹配算法

### 现状（事实）

| 字段族 | 策略 | 实现点 |
|--------|------|--------|
| VIN / 证件号 / 车牌 / 发动机 / 品牌型号 / 登记证号 | normalize → **exact** | `match/exact.py` |
| 姓名 / 地址 | normalize → **fuzzy**（SequenceMatcher + adaptive uncertain band） | `match/fuzzy.py` |
| 金额 | normalize Decimal → **abs/rel tol** | `match/numeric.py` |
| 列表包含 | list_contains + 元素 normalize | `match/list_ops.py` |
| 置信度 | 低 conf / normalize 失败 → **uncertain** | engine gate |

**没有**：跨字段联合得分、贝叶斯传播、实体解析(ER)聚类、阻塞索引。

### 正方主张（本轮立场）

**主张：exact + fuzzy + 容差 + 三态 对任务4 主路径足够；禁止上贝叶斯/全量 ER。只在姓名/地址补「字段感知相似度」；性能无需索引。**

理由：
1. **问题形态是「已知实体、多源字段对齐」**，不是「海量候选里找同一人」。Entity Resolution（blocking + pairwise + clustering）解决的是开放世界去重；这里 application 内 doc 已绑定，候选集 = 同申请内 2–N 个值。
2. **主键字段不容糊**：VIN/证件号/发动机号 必须 exact（normalize 后）。Fuzzy VIN = 灾难性 FN 风险（差 1 位可能是真车/假车）。OCR 噪声用 **normalize 纠错 + uncertain**，不用 fuzzy 放行。
3. **金额用容差正确**：融资租赁合同 vs 发票常见分位/四舍五入；abs=1 + rel=0.001 合理。无需概率模型。
4. **姓名/地址是唯一薄弱点**：
   - 现状 `SequenceMatcher` 对短中文名 1 字差过狠（已靠 adaptive band 进 uncertain）
   - 缺：同音/形近 OCR 表、地址层级包含（「江苏南京」⊂「江苏省南京市玄武区…」）
   - **加权联合得分** 有限场景有用：例如「姓名 fuzzy  borderline + 证件号 exact → 抬升」；但必须 **规则显式组合**，禁止隐式全局贝叶斯（不可解释、指标不可复现）
5. **贝叶斯/置信度传播**：
   - OCR conf 已作 hard gate；再传播需要先验 + 似然校准数据 — **我们没有标注后验分布**
   - 合成 fixture 上调参 = 自嗨；上线后分布漂移 → 指标谎言
   - 决议：保留 conf 阈值 + uncertain 带；**不上概率图**
6. **性能**：10 docs × 20 fields × ~15 rules × pairwise ≤ C(10,2)=45 比较 → 微秒～毫秒。**禁止**为单申请建倒排/缓存层。仅当批量 evaluate ≥1e4 apps 时再谈进程内规则配置缓存（已 load 一次即可）。

### 反方攻击

| 攻击 | 力度 | 回应 |
|------|------|------|
| 「VIN exact 遇 OCR 1 位错 → 全漏杀/全误杀」 | 真 | 修 **vin normalizer**（I/O/Q、近形字符可选纠错）+ 低 conf→uncertain；**不**对 VIN 开 fuzzy。可加 `exact_with_edit1_uncertain` 专用 type（编辑距离=1 → uncertain，≠放行） |
| 「姓名仅 ratio 不够：同音、繁简、少数民族间隔点」 | 真 | P1：person normalizer 扩繁简/间隔点；可选拼音首字母旁路进 uncertain 而非 consistent |
| 「地址应用包含匹配而非纯 fuzzy」 | 真 | P1：`address` 专用 matcher：normalize 后 longest common / token-set / 层级包含 |
| 「多字段联合能降 uncertain」 | 中 | P2：显式 `composite` type：`all_of`/`any_of` 子检查 + 权重阈值；**禁止**黑盒 joint score |
| 「没有 ER 无法处理一人多证件别名」 | 弱 | 别名在 `field_aliases` + person normalize；跨人聚类超出任务4 |
| 「SequenceMatcher 慢」 | 无 | 短串；可忽略 |
| 「需要缓存 normalized 字段」 | 弱 | 已在 `_normalize_application` 一次写回；足够 |

### 决议 B

| 项 | 决议 |
|----|------|
| 主匹配族 | **保留** exact / fuzzy / numeric_tol / list_contains |
| VIN/ID/发动机 | **永远 exact**（normalize 后）；edit-distance 仅可产 uncertain，不可产 consistent |
| 姓名 | 增强 normalizer + 短串 band（已有）；**不上**全局加权 ER |
| 地址 | 从通用 fuzzy **演进为字段感知 matcher**（包含/token） |
| 联合得分 | 仅接受 **显式 composite 规则 type**；拒绝隐式贝叶斯传播 |
| 贝叶斯/置信度图 | **否决**（无校准数据 + 不可解释） |
| Entity Resolution | **否决**（问题形态不匹配） |
| 性能索引/缓存 | **否决**单申请索引；规则 YAML load 缓存即可 |

---

## 对 dev 的可执行建议

### P0（本轮可做，低风险，对齐决议）

1. **冻结对外规则契约**：`type` 五类 + YAML 字段语义不改；新能力优先塞 normalizer / threshold / fixture。
2. **VIN/证件 exact 铁律**：禁止对 `vin`/`id_number`/`engine_no` 配 `type: fuzzy`；loader 可加 warn（可选）。
3. **匹配失败路径可解释**：保持 snapshot + diff；任何新 matcher 必须吐 `score`/`message` 中文原因。
4. **不引入** cel-python / 规则图库 / sklearn / 概率图依赖。

### P1（商业级下一迭代，小改代码）

1. **策略注册表**（议题 A）：
   - `rules/handlers.py`：`HANDLERS: dict[str, Callable]`；`engine._eval_rule` 只查表
   - 现有 5 type 迁入 handler；行为单测 bit-identical
2. **地址字段感知匹配**（议题 B）：
   - `match/address.py`：normalize 后若 A in B or B in A → consistent；否则 token-set ratio；近阈 uncertain
   - YAML：`R_ADDRESS_FUZZY` 可改 `type: address` 或 engine 按 field 路由（二选一，偏好显式 type）
3. **VIN 近邻 uncertain**（可选 type 或 exact 开关）：
   - 编辑距离 1 且双方 conf 中等 → `uncertain`，**绝不** auto-consistent
4. **person normalizer**：繁简可选表、间隔点、后缀已有；补常见 OCR 形近（「日/曰」级，克制）

### P2（有证据再做）

1. **`composite` 规则 type**：显式 `all_of` / `weighted_any` 引用子 rule_id 或 inline 子句；用于「证件 exact 抬升姓名 borderline」— 仅当 fixture 证明 uncertain 过多且业务接受。
2. **多文件规则包** + JSON Schema 校验 + `version` 兼容矩阵。
3. **批量 evaluate 性能基线**：≥1e4 apps 时再 profile；预期瓶颈在 IO/YAML 而非 match。
4. **重开条件**：
   - CEL：当专用 type >12 且 30% 规则是 ad-hoc 表达式
   - 规则图：当真实跨 rule 依赖 ≥3
   - ER/贝叶斯：当输入变为「跨申请客户归并」而非「单申请多单据」

---

## 一句话总决

> **引擎：YAML 契约 + 策略注册演进，拒 DSL/图。匹配：normalize 后 exact/fuzzy/tol 够用，主键永不 fuzzy，地址字段化，联合分仅显式 composite，概率/ER 出局，性能无需索引。**

Manager 可据此压 dev：P0 纪律立刻生效；P1 进下一轮 ITERATION；P2 等指标/fixture 证据。

---

## 议题 C：`reason_codes` — 稳定枚举 vs 自由字符串

### 现状

`CheckResult.message` = 中文自由串（engine f-string）。无稳定机读码。Round7 拟加 `reason_codes: list[str]`。

### 正方（稳定闭集枚举）

- 评估/回归/下游路由靠码不靠中文解析
- 指标切片（`LOW_CONF` vs `VIN_MISMATCH`）可复现
- 多语言 UI：码固定，文案外置
- 防漂移：禁止 handler 随手 invent 新码

### 反方（自由字符串 / 开集）

- 枚举僵：新 failure mode 要改 Enum + 文档，节奏慢
- 过度抽象：一条 check 多原因时枚举组合爆炸
- MVP 已有 message；再加码 = 双源真相风险（码与文案不一致）

### 决议

**稳定闭集枚举（str Enum / 常量表）+ 可多码；message 仍为人读。**

| 项 | 决 |
|----|----|
| 形态 | `reason_codes: list[str]`，元素 ∈ 白名单 |
| 开集自由串 | **否**（禁止任意 `"foo_bar_baz"`） |
| 扩码 | 改 `ReasonCode` 表 + INTERFACE 一行；禁止 runtime 注册野码 |
| message | 保留中文；**不以 message 做逻辑分支** |
| 多码 | 允许（如 `["LOW_CONF","NORMALIZE_FAIL"]`）；顺序：主因在前 |

建议初集（可裁）：`MATCH` / `MISMATCH` / `NEAR_THRESHOLD` / `LOW_CONF` / `MISSING_FIELD` / `MISSING_DOC` / `NORMALIZE_FAIL` / `EDIT_DISTANCE_1` / `TOLERANCE_EXCEEDED` / `COND_REQUIRED_FAIL` / `LIST_NOT_CONTAIN` / `UNKNOWN_RULE_TYPE` / `SKIPPED`

### 对 dev（P1）

1. `models.ReasonCode`（str Enum）+ `CheckResult.reason_codes: list[str]`
2. engine/handler 每出口至少 1 码；未知码 loader/assert 测里拦
3. `INTERFACE.md` 表：码 → 含义 → 典型 verdict
4. evaluate 可选按码聚合；**禁止** `if "不一致" in message`
5. 旧 fixture 只比 verdict 时兼容缺码；新 fixture 尽量带期望码

---

## Round16 W1/W2 配置热更与 critical 保护

- Mode: **Round A**（正方主张 + 自反驳）；未终裁
- Code fact: `web/app.py` PUT 已 tempfile+`replace`，缺 fsync/锁；rollback 用 `write_text(prev)` 非原子；无 critical 最小集 enforce
- **禁改 GOAL.md**

### W1 runtime_rules 写入语义 — 主张 **A**

| 选项 | 一句话 | 本轮 |
|------|--------|------|
| **A** tempfile+fsync+replace；校验失败不碰 active | 单文件事务，POSIX 够用 | **选** |
| B active/standby + 健康切换 | 双文件状态机 | 拒 |
| C 仅内存 + 显式 publish | 无盘毒化，重启丢/另存 | 拒 |

**主张 A 细节**
1. 顺序：parse → `load_rules` 校验（内存/临时路径）→ **通过后** 同目录 `*.tmp` 写 → `flush+fsync` → `os.replace` →（可选）fsync 父目录
2. 校验失败：**零写** active；HTTP 4xx + audit `rules_save ok=false`
3. 写中崩：`replace` 前 active 旧文件仍在；毒化 tmp 可清
4. 并发两 PUT：整文件 replace → 文件级原子；语义 = last-writer-wins；加 **进程内锁** 防交错 audit/半逻辑
5. 与 reset：`unlink(runtime)` 回默认包；reset **不** 读半写文件（锁同源）
6. 失败可见：API error 码 + audit；UI 展示「未生效，仍用旧版」

**拒 B**：无多实例健康探针；standby 切换增加「谁 active」第三状态，攻击面+运维面双增。  
**拒 C**：演示/部署要重启后规则仍在（`DEPLOY.md` 已定 runtime 文件优先）；纯内存 = 另造 publish 持久化，最终仍回 A。

**失败模式（A 仍会死）**
- 跨文件系统 rename 非原子（少见；强制同 dir）
- last-writer 丢更新（两编辑者）
- 合法空规则包通过 schema → 业务漏检（→W2）

### W2 critical 保护 — 主张 **A**（硬 enforce；C 为结构升级方向）

| 选项 | 一句话 | 本轮 |
|------|--------|------|
| **A** 服务端最小 critical 集：不可删 / 不可改 type（及关键语义） | 资金安全底线在 server | **选** |
| B 可删 + warn + 二次确认 + 审计 | UI 仪式；脚本绕过 | 拒 |
| C base(immutable)+overlay(mutable) | 分层干净 | 延后，不替 A |

**主张 A 细节**
1. **最小集合**（按 `rule.id`，与默认包对齐）：至少  
   `R_VIN_CROSS` · `R_ENGINE_CROSS` · `R_ID_EXACT`  
   （可配置常量 `CRITICAL_RULE_IDS`；改集合 = 发版/改默认包，不让 UI 静默干掉）
2. save 路径 enforce：缺 id → 400 `critical_rule_missing`；同 id 但 `type` 被改离 exact（VIN/ID/发动机）→ 400 `critical_type_frozen`；`severity` 降非 critical → 400 或强制抬回
3. **`require_all_docs`**：最小集默认 **true**（与现 critical auto 一致）；save 时若改 false → 400 或 force true + audit warn
4. reset：删 runtime → 默认包自带最小集；无需额外
5. 拒 B：确认框防君子不防 PUT；审计只事后，漏检已发生

**为何不先上 C**：overlay 优，但无 A 的 id/type 硬闸，overlay 仍可「覆盖掉」critical 语义。C 应 **叠在 A 上**（base=默认包只读，overlay 禁删 base critical ids）。

**失败模式（A 仍会死）**
- 同 id 马甲：保留 `R_VIN_CROSS` 但 `docs: []` / 改 field → 空跑一致
- 只钉 id 不钉语义指纹 → 需 **语义锁**（field+type+severity+require_all_docs）
- 最小集写死滞后业务（新主键规则）

### 自反驳（致命风险 ≥2 / 题）

**杀 W1-A**
1. **Rollback 路径若再 `write_text(prev)` = 二次非原子** — 现码已踩；A 必须规定：失败时 **禁止** 覆写 active；仅删 tmp。否则「回滚」本身毒化。
2. **校验与生效时间窗**：进程已 load 旧 RuleEngine；磁盘 A 成功 ≠ 内存热切换。无显式 rebind → 「已保存」谎言。A 必须附：**save 成功后进程内替换 active config**（或每请求 `load_rules` + mtime 缓存）。
3. （附）无文件锁时两请求双 tmp replace 竞态：数据通常仍整文件合法，但 audit/UI 版本号乱序。

**杀 W2-A**
1. **id 闸不够**：`R_VIN_CROSS` 仍在，`docs` 掏空 / `on_missing: skip` / `field` 改 junk → 静默漏检。必须 **critical 语义指纹**（id+field+type+severity+require_all_docs 下限）。
2. **与产品冲突**：运营要「临时关发动机校验」被 400 卡死 → 绕道改代码/默认包，影子配置扩散。缺 **break-glass**（双人/审计 token）会逼出更脏旁路。
3. （附）最小集与 `package` 多包共存时 id 冲突未定义。

### Round A 暂定（非终裁；待 Round B）

| 题 | 暂定 | 硬约束带进 B |
|----|------|----------------|
| W1 | **A** | 校验先于写；失败零碰 active；fsync+同目录 replace；进程锁；save 后内存 rebind；rollback≠非原子写回 |
| W2 | **A** | 最小 critical ids + **语义指纹**；B 否；C 可作 P2 结构但不可削弱 A |
| 并发 | last-writer + lock | 不引入 standby |
| reset | unlink runtime | 与写同锁 |

### 给 Round B / 终裁清单

- [ ] W1：是否强制每请求 load vs mtime 缓存失效
- [ ] W2：语义指纹字段清单终表；break-glass 要不要（建议：不要，MVP）
- [ ] 最小集列表是否允许 YAML 声明 `immutable: true` 替代代码常量
- [ ] attack 探针：半写中断、非法 YAML、删 VIN、改 VIN→fuzzy、掏空 docs

**Round A 一句话：** 热更用单文件原子 A，不玩双文件/纯内存；critical 用服务端硬闸 A（id+语义），UI 确认无效，分层 C 后置且不得拆闸。

---

## Round16 Round B — 反方透镜 · 终裁

- Mode: **Round B** 专杀 A → **终裁**
- 事实：`_engine()` 已每请求 `load_rules(_active_rules_path())`；PUT 仍有非原子 rollback
- **禁改 GOAL.md**

### 1. 安全：防「留 id 掏 docs」— 语义指纹终表

**杀点：** 只锁 `id` / `type` → 攻击者 `docs: []` 或缩 docs / `on_missing: skip` / 改 `field` → 规则「在」但永不比对。

**闸：save 前 `enforce_critical_fingerprints(cfg)`；任一失败 → 400，零写盘。**

| rule.id | field（冻结） | type | severity | require_all_docs | on_missing 允许 | docs_min（runtime 必须 ⊇） |
|---------|---------------|------|----------|------------------|-----------------|---------------------------|
| `R_VIN_CROSS` | `vin` | `exact` | `critical` | `true` | `uncertain` \| `inconsistent` | `机动车登记证书`, `融资租赁合同` |
| `R_ENGINE_CROSS` | `engine_no` | `exact` | `critical` | `true` | `uncertain` \| `inconsistent` | `机动车登记证书` |
| `R_ID_EXACT` | `id_number` | `exact` | `critical` | `true` | `uncertain` \| `inconsistent` | `融资租赁合同`, `身份证` |

**校验规则（硬）**
1. 三 id **必须存在**（缺 → `critical_rule_missing`）
2. 上表每列 **精确相等**（除 docs）
3. `docs`：**⊇ docs_min**（可超集；空集/缺关键单据 → `critical_docs_stripped`）
4. `on_missing: skip` **禁止**（→ `critical_on_missing_skip`）
5. `field` / `type` 偏离 → `critical_semantic_tamper`
6. 同 id 多条 → 拒绝

docs_min 取「资金链路最小覆盖」，非默认包全文拷贝——允许加单据，禁止掏空。

### 2. 运维：rebind vs 每请求 load — **终选每请求 load**

| 方案 | 判 |
|------|-----|
| save 后内存 rebind 单例 | 拒：双真相（盘 vs 内存）；多 worker 不一致 |
| 每请求 load + mtime 缓存 | 可作 P2 优化 |
| **每请求 `load_rules(active_path)` 无长缓存** | **终选** |

**理由：** 现网已是此形；单申请规则包小；正确性 > 微秒 IO。save 成功 = 下次请求读新文件，无需 rebind API。  
**写路径仍要进程锁**（防双 PUT 交错 audit / 双 tmp）。  
毒化：`_active_rules_path` 校验失败 quarantine → 回默认包（保持）。

### 3. 产品：无 break-glass？— **MVP 不要**

| 风险 | 处置 |
|------|------|
| 运营想临时关发动机校验被 400 | **正确**；融资主键不可 UI 热关 |
| 逼影子配置（手改默认包 / 旁路 CLI） | 接受残差：FS/CLI 在 Web 威胁模型外 |
| 真要扩/缩最小集 | **改代码指纹表 + 默认 YAML + 发版**；不设双人 token |

**MVP 结论：无 break-glass。** 审计记拒绝即可。P2 若合规要例外：另开 RFC，默认仍关。

### 4. 最小集来源 — **终选代码常量**

| 方案 | 判 |
|------|-----|
| **代码 `CRITICAL_FINGERPRINTS`（唯一权威）** | **终选** |
| YAML `immutable: true` 为唯一闸 | 拒：攻击者 PUT 删 flag |
| YAML 声明 + 代码校验一致 | 可选 UI 提示；**安全不依赖 YAML** |

权威在代码；默认包须满足指纹（单测钉死）；runtime 保存再 enforce 一次。

### 5. 终裁决议

| 议题 | 终裁 |
|------|------|
| W1 写入 | **A**：校验通过 → 同目录 tmp → flush/fsync → `os.replace`；**失败零碰 active**；**禁止** `write_text(prev)` 回滚 |
| W1 生效 | **每请求 load**；无 RuleEngine 脏单例；写路径 `threading.Lock` |
| W1 reset | 同锁 `unlink(runtime)`；下次请求默认包 |
| W2 保护 | **A + 语义指纹终表**（上表）；B 否；C 延后 |
| W2 最小集 | **代码常量** 三 id；YAML immutable 不当事 |
| break-glass | **无（MVP）** |
| 并发 | last-writer-wins + 写锁 |
| 可见性 | 4xx 稳定 error 码 + audit；成功返回 path/version/n_rules |

**一句话终裁：** 原子单文件热更、失败不染 active、每请求读盘生效；critical 三剑客代码指纹锁死（id+field+type+severity+require_all_docs+on_missing+docs⊇min）；无 UI 逃生门。

### 6. 给 dev 的 P0 可执行清单

1. **抽 `task4_consistency/rules/critical_guard.py`（或 web 内模块）**  
   - `CRITICAL_FINGERPRINTS` 上表  
   - `enforce_critical_fingerprints(cfg) -> None`；违例 raise 带 `error` 码
2. **`PUT /api/rules` 改写序**  
   - parse → `load_rules` 校验 → **fingerprint enforce** →（通过）锁内：tmp 写 + fsync + `os.replace`  
   - 校验/指纹失败：**不创建、不 replace** active  
   - 删除 `prev_text` + `write_text` 回滚分支
3. **写锁**  
   - 模块级 `RULES_WRITE_LOCK`；`put_rules` / `reset_rules` 同锁
4. **生效路径**  
   - 保持 `_engine()` / check **每请求** `load_rules(_active_rules_path())`；**不**引入进程级 config 单例缓存
5. **毒化自愈**  
   - 保留 runtime load 失败 → `.bad` quarantine → 默认包；audit
6. **error 码（API detail.error）**  
   - `critical_rule_missing` / `critical_semantic_tamper` / `critical_docs_stripped` / `critical_on_missing_skip` / `invalid_yaml` / `rules_save_failed`
7. **单测（必）**  
   - 非法 YAML 不改 runtime  
   - 删 `R_VIN_CROSS` → 400  
   - `R_VIN_CROSS` type→fuzzy → 400  
   - `docs: []` 或去掉登记证 → 400  
   - `on_missing: skip` → 400  
   - 合法超集 docs + 改 threshold 类非关键字段 → 200  
   - reset 后 active=默认包且指纹仍过
8. **攻击探针**（`attack_web_kb` / probes）  
   - 半写/坏 YAML / 删 VIN / VIN→fuzzy / 掏空 docs → 期望 CLOSED
9. **文档**  
   - `CONFIG_GUIDE` / `INTERFACE` 补指纹表 + error 码；**禁改 GOAL.md**
10. **验收**  
    - `pytest -q` 绿；attack web 探针 W1/W2 CLOSED

**P0 不做：** standby 双文件、CEL、break-glass、mtime 缓存、base/overlay 分层、改 GOAL。

**Dev 开干信号：** 本终裁生效；实现只准落 P0 清单。

---

## Round19 半真实接入 · Round A

- Mode: **Round A**（主张 + 拒项 + 自反驳）；**未终裁**
- 边界锚点：`ARCHITECTURE`「任务4 与 OCR 解耦」；step2 有框无字；`1.zip` 大图 Out-of-scope 深度视觉
- **禁改 GOAL.md**

### 议题 1：输入路径 — 主张 **A 契约 + C 离线落盘**（拒 B 入核）

| 选项 | 含义 | 本轮 |
|------|------|------|
| **A** 仅 JSON 契约 + 外部/人工 OCR | RuleEngine **只吃** Application JSON | **契约法** |
| B 内置可插拔 `OcrProvider`（mock/paddle/云） | 进程内调 OCR | **拒入核** |
| **C** 离线批：影像→OCR JSON 落盘→再 check | 脚本管道产 fixture | **半真实入口** |

**主张**
1. **引擎边界永不破**：`check`/`evaluate` 输入 = Application JSON（A）。零影像、零 Provider 在 `RuleEngine`。
2. **半真实只走 C**：`scripts/` 或 `adapters/ocr_batch.py` 读外部 OCR 结果 → 写 `fixtures/semi/*.json` → 既有 CLI check。契约不变，可复现、可 diff、可 git。
3. **B 降级**：若未来要 mock，只许在 **批处理脚本** 内接口，**禁止** web/engine 依赖 paddle/云 SDK。

**拒 B（入核）**：重依赖炸 CI；非确定时序污染指标；超时/鉴权/配额变任务4 运维问题；与 GOAL「不做任务1–3 深度视觉」撞车。  
**拒纯 A 不做事**：半真实永远 PPT。  
**拒纯 C 改引擎吃图**：边界回退。

**自反驳（≥2）**
1. **假接入**：C 若用「合成 OCR JSON」冒充真实，指标仍合成 — 诚实性零增益，还多一层自嗨。
2. **管道无主**：无统一 OCR schema → 每家字段名漂移 → adapter 分叉地狱；半真实集不可比。
3. （附）落盘 JSON 无版本/模型名 meta → 无法追溯「哪版 OCR 产的」。

---

### 议题 2：无文本 step2 — 主张 **A 元数据 + 合成字段**；**C 文档化 1.zip**；拒 B

| 选项 | 含义 | 本轮 |
|------|------|------|
| **A** 页序/框元数据；字段值仍合成 | 现状增强 | **选** |
| B 框裁剪 + OCR | 真半链路 | **拒（Round19）** |
| **C** 不碰 1.zip 大图；仅文档化 | 范围闸 | **并选** |

**主张**
1. step2 = **布局/检测 conf 信号**，不是文本源。adapter 继续 `raw=null` 或 **注入合成 raw** 并写 `meta.step2_sample_id` + `meta.field_source=synthetic`。
2. 可用 step2 conf 做 **低置信探针**（检测 conf 映射 field.confidence）— 仍非 OCR 字。
3. **1.zip**：Round19 **不** 解压全量 OCR；`DEPLOY`/`EVALUATION_REPORT` 写死边界。裁剪 OCR = 任务1–3 地盘，无标注文本 GT 则评价谎言。

**拒 B**：无字级 GT；框≠字段值；裁剪质量/DPI/旋转未定义；拉 Paddle = 依赖炸弹；与议题1 拒入核一致。  
**拒「只 C 什么都不接」**：step2 资产浪费；至少 A 绑定样本 id 证明「结构可对齐真实册」。

**自反驳（≥2）**
1. **半真实名不副实**：真实框 + 假字 = 营销欺诈风险；评测读者以为接了 OCR。
2. **检测 conf ≠ OCR conf**：拿检测分当字段 conf 会系统性误导 uncertain 门控。
3. （附）单证类型页序 → 多单据申请仍靠合成拼装，跨单据一致性仍假。

---

### 议题 3：指标 — 主张 **`evaluate --suite semi` 分离**；禁并入主集默认分母

| 选项 | 本轮 |
|------|------|
| 半真实并入主 evaluate 同一 coverage/FP/FN | **拒** |
| **`evaluate --suite {main,semi,all}`；默认 main** | **选** |
| 只出报告文字、不跑数 | 拒（无牙） |

**主张**
1. **主套件** = 现合成 fixtures + labels；赛题门槛数字 **只报 main**（防半真实噪声洗指标）。
2. **semi 套件** = 显式路径/标签（可先空壳 + 1–2 条 step2 绑定样本）；指标 **单独表**；`EVALUATION_REPORT` 双栏，强制写「semi 非官方标注」。
3. `--suite all` 仅调试；**禁止** 把 all 当交付主数字。

**拒合并主集**：样本少/无真标签 → FP/FN 定义漂移；合成主集被稀释或「假绿」。  
**拒不跑 semi**：架构议题空转。

**自反驳（≥2）**
1. **双指标政治**：对内看 main、对外甩 semi 好看数 — 需报告模板钉死「主数字=main」。
2. **semi 无可靠 label**：只能测「跑通/不崩/uncertain 率」，不能认真报漏报≤3%；空壳 suite 变形式主义。
3. （附）fixture 路径分叉 → CI 矩阵膨胀；维护成本。

---

### Round A 串联立场（未终裁）

```
外部 OCR / 人工
      │
      ▼
[C 离线批 → Application JSON + meta.ocr_*]
      │
      ▼
RuleEngine ← 只认 JSON（A）
      │
step2 ──► adapter 元数据/合成字段（议题2-A），不裁图 OCR
      │
evaluate --suite main|semi   ← 数字隔离（议题3）
```

| 题 | Round A 主张 | 硬拒 |
|----|--------------|------|
| 1 | A 契约 + C 落盘 | B 入核 Provider |
| 2 | step2=元数据+合成；1.zip 文档化 | 框裁剪 OCR |
| 3 | suite 分离；默认 main | 半真实并主分母 |

### 给 Round B 必杀清单

- [ ] C 的 **OCR 中间 JSON schema** 最小字段表（含 model/version）
- [ ] semi 套件 **有无 label** 时指标怎么定义（跑通率 vs FP/FN）
- [ ] MVP 一周切片：只做 suite 开关 + 1 条 step2 绑定，还是假 OCR generator（**建议拒假生成器**）
- [ ] meta 强制标记 `field_source: synthetic|external_ocr|null` 防诚实性翻车

**Round A 一句话：** 引擎死守 JSON；半真实只准离线落盘；step2 不装 OCR；指标 main/semi 分家；假 OCR 生成器有罪推定。

---

## Round19 Round B 终裁

- Mode: **Round B 终裁**（写盘；dev 可开干）
- 继承 Round A：JSON 契约 + 离线 OCR 落盘、step2 仅元数据、`evaluate --suite main|semi`
- 事实：`app_r12/r13/r17_*step2*` 字仍合成；adapter `raw=null`；evaluate 尚无 suite
- **禁改 GOAL.md** · **拒假 OCR 生成器**

### 0. 专杀 A（收窄）

| 透镜 | 杀 | 终裁修正 |
|------|----|----------|
| 安全 | import 任意 path / 巨包 | 根内路径 + 2MB cap |
| 成本 | 真 OCR 流水线一周做不完 | C 降级 = schema+import，无引擎 |
| 诚实 | step2 假字报 semi FP/FN | step2 留 **main**；semi 仅 external |
| 运维 | schema 分叉 | **唯一** 中间 JSON schema |

### 1. OCR 中间 JSON 最小 schema（字段表 · 唯一 import 入口）

| 路径 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | int | Y | 目前仅 `1` |
| `ocr_model` | str | Y | 模型/供应商名；空串拒 |
| `ocr_version` | str | Y | 版本号；空串拒 |
| `application_id` | str | Y | 申请 ID |
| `documents` | list | Y | 长度 ≥1 |
| `documents[].doc_id` | str | Y | |
| `documents[].doc_type` | str | Y | 与规则 docs 对齐的中文类型名 |
| `documents[].fields` | object | Y | 可空 object，但 key 必须 str |
| `documents[].fields.<name>.raw` | str\|null | Y | OCR 原文；允许 null |
| `documents[].fields.<name>.confidence` | float | N | 默认 1.0；∈[0,1] |
| `documents[].fields.<name>.source_page` | int | N | 页码 |
| `documents[].fields.<name>.field_type` | str | N | 可选提示 |

**拒收：** 缺 model/version；非 mapping 根；`documents` 非 list；单文件 >2MB。  
**import 写出 Application.meta 必须含：** `source=external_ocr`, `field_source=external_ocr`, `ocr_model`, `ocr_version`, `ocr_imported_at`。

### 2. semi 无可靠 label 时指标定义

| 模式 | 条件 | 可报 | 禁报 |
|------|------|------|------|
| **smoke**（默认 semi） | 无 labels / 无 embedded expected | `n_apps_loaded`, `n_check_ok`, `n_check_fail`, verdict 计数分布, `uncertain_rate` | coverage/FP/FN、漏报≤3%、误报≤5% |
| **labeled_semi** | `fixtures/semi/labels/` 或 fixture 内 `expected_verdicts` 齐全 | 与 main 同公式 FP/FN，**分表** | 并入 main 分母 |
| **main** | `fixtures/applications/` 全量 | 交付唯一官方数字 | 用 semi 刷绿 |

metrics JSON 必带：`suite`, `mode: smoke|labeled`, `honesty_note`。  
**默认 evaluate = main。** `--suite all` 仅调试，禁止当验收主数字。

### 3. `meta.field_source` 强制

| 值 | 何时 |
|----|------|
| `synthetic` | 手写/业务构造字段值（含 step2 绑定假字） |
| `external_ocr` | 经 import schema 进入；且必须同时有 ocr_model/version |
| `null` | 无文本占位（step2 adapter 仅框、raw=null） |

**强制规则**
1. `fixtures/semi/*` 写入时 **必须** `field_source=external_ocr`（import 写死，调用方不可省略）
2. 新 main fixture 建议带 `field_source`；缺省 evaluate **warning**，不 fail（兼容旧集）
3. `source=semi_real_step2` 且有合成字 → `field_source` **不得** 标 `external_ocr`
4. 报告/README：**field_source≠external_ocr 不得宣传「真实 OCR 评估」**

### 4. 终裁总表

| 项 | 决 |
|----|----|
| 引擎 | 只吃 Application JSON |
| Provider/Paddle/云/1.zip 裁剪 OCR | **否** |
| 假 OCR 生成器 | **否** |
| 离线 C | schema + `import_external_ocr` 落盘 |
| suite | `main`=`fixtures/applications/`；`semi`=`fixtures/semi/`；默认 main |
| main 分母 | **冻结现状全量**（含 step2 合成绑定） |
| 交付数字 | **只认 main** |

**一句话：** 本周不接 OCR 引擎；钉中间 schema + suite + 强制 field_source；semi 无 label 只 smoke；拒假生成器。

### 5. MVP 一周切片 · P0 文件级清单

| # | 文件 | 动作 |
|---|------|------|
| 1 | `task4_consistency/cli.py` | `evaluate --suite {main,semi,all}` 默认 main |
| 2 | `task4_consistency/evaluate.py` | 多目录；metrics 增 suite/mode/honesty_note；smoke 不算 FP/FN |
| 3 | `fixtures/semi/.gitkeep` | semi 目录 |
| 4 | `fixtures/ocr_inbox/example.json` | 合法中间 schema 样例（demo 字可合成，但 import 后 meta=external_ocr 且 note=demo） |
| 5 | `task4_consistency/adapters/external_ocr_import.py` | **新** load+校验+meta 强制 |
| 6 | `scripts/import_external_ocr.py` | **新** CLI；repo 内路径；2MB 限 |
| 7 | `task4_consistency/adapters/step2_page_order.py` | meta `field_source=null`；不造假字 |
| 8 | `tests/test_evaluate_suite.py` | **新** main 回归；semi 空/样例不崩 |
| 9 | `tests/test_external_ocr_import.py` | **新** 缺 model 拒；meta 强制 |
| 10 | `docs/INTERFACE.md` | schema 字段表 + suite + field_source |
| 11 | `docs/EVALUATION_REPORT.md` | main/semi 双栏诚实声明 |
| 12 | `docs/ITERATION_LOG.md` | Round19 记录 |
| — | **GOAL.md** | **禁改** |

**验收**
```bash
.venv/bin/pytest -q
.venv/bin/python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite main -o out/metrics_main.json
.venv/bin/python -m task4_consistency evaluate -c configs/rules_auto_lease.yaml --suite semi -o out/metrics_semi.json
.venv/bin/python scripts/import_external_ocr.py fixtures/ocr_inbox/example.json -o fixtures/semi/
```

**P0 禁止：** GOAL 改动 · OcrProvider/paddle · 1.zip OCR · 假 OCR gen · step2 移出 main · semi smoke 宣称漏报≤3%

**Dev 开干信号：** 本终裁写盘生效。

---

## Round26 batch evaluate

- Mode: **轻量一轮终裁**（主张+自反驳合并）
- 事实：已有 `POST /api/check/batch`、`GET /api/evaluate/summary?suite=`、CLI `evaluate --suite`
- **禁改 GOAL.md**

### 主张 / 取舍

| 选项 | 含义 | 判 |
|------|------|----|
| **A** 不做新 API；批处理走 CLI；Web 维持单申请 check + suite summary | 零新资源模型 | **终选** |
| B 同步 `POST /api/evaluate/batch`（≤N 内存） | 上传多 app+label 内存算指标 | **拒（本阶段）** |
| C 异步 job 队列 | 任务 id / 轮询 / 持久化 | **拒 → P2 记名** |

**为何 A**
1. **evaluate ≠ check**：指标评估吃 **磁盘 fixture 套件**；`evaluate/summary` 已覆盖。多申请校验已是 `check/batch`。
2. **B 重复且易混**：再造 batch evaluate = 第二套限流/超时/半失败语义，与 check/batch 边界糊。
3. **C 改交互与运维面**（队列、worker、崩溃恢复）— MVP/演示不值。

### 自反驳（杀 A）

1. **集成方要「一次 POST 多申请带 label 出 FP/FN」** — A 逼走 CLI/本地脚本；Web 弱。  
   → 回应：赛题交付数字认 suite/main；集成用 CLI 或自拼 check 结果。真刚需再开 B 且 N≤20、body≤2MB、同步超时硬顶。
2. **check/batch 已在却无 cap** — A 不修也不扩 evaluate，留下 DoS 面。  
   → 回应：属 **加固 check/batch**，不是新 evaluate API；可顺手文档写 N 建议，非本议题必做 C。

### 终裁

| 项 | 决 |
|----|----|
| MVP batch evaluate API | **不做（A）** |
| 批处理路径 | **CLI** `evaluate --suite main\|semi` |
| Web | 单申请 `POST /api/check`；多申请 **已有** `POST /api/check/batch`；套件指标 **已有** `GET /api/evaluate/summary` |
| B | 本阶段否；触发条件：外部明确要「上传多 app 在线 FP/FN」且 N 有 SLA |
| C | **P2 only**，本阶段零代码 |

**一句话：** 别造第三套批评估；CLI 批、Web 摘要、check 已能 batch。

### 给 dev 一句指令

**A 生效：勿实现 `/api/evaluate/batch` 与 job 队列；只在 `docs/INTERFACE.md`（或 CONFIG/DEPLOY 一句）写清「批量评估走 CLI evaluate；Web 用 evaluate/summary + check/batch」。禁改 GOAL。ci_gate PASS。**

---

## Round43 audit schema

- Mode: **轻量一轮终裁**
- 事实：`audit.write_audit` 已写 JSONL 信封 `ts/action/actor/ok/detail`；无 `schema_ver`；`action` 自由串；best-effort 不抛
- **禁改 GOAL.md**

### 主张 / 取舍

| 选项 | 含义 | 判 |
|------|------|----|
| A 维持 ad-hoc JSONL | 调用方随意塞字段 | 拒作「规范」 |
| **B 最小版本化 schema** | 固定信封 + `schema_ver` | **终选** |

**主张 B（轻）**
1. 现状已是准 schema；差一版号。统一成本 ≈ 1 字段 + 文档，不是重写。
2. 运维/攻击探针/多读者要稳定解析；无 `schema_ver` 时 detail 一变全盲。
3. **只钉信封，不钉业务**：`action` 仍开放 str；`detail` 仍 dict 自由。禁止上「全量 action 枚举冻结」——那是过度。

**信封终表（schema_ver=1）**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_ver` | int | Y | 当前 `1` |
| `ts` | str ISO8601 UTC | Y | |
| `actor` | str | Y | 默认 `web` |
| `action` | str | Y | 如 `rules_save` / `kb_add`；开放词表 |
| `ok` | bool | Y | |
| `detail` | object | Y | 可 `{}`；业务扩展只进这里 |

### 自反驳（杀 B）

1. **版号仪式**：无人读 audit，schema_ver 垃圾字段。  
   → 回应：`/api/audit/recent` + 排障已在用；版号成本零，缺了才痛。
2. **强制 schema 校验写失败 → 反噬主路径**。  
   → 回应：写路径仍 best-effort；**不**因 detail 形状 throw；仅保证信封键存在。
3. **action 不枚举 = 仍 ad-hoc**。  
   → 回应：故意。枚举冻结逼改核心；B 管运输层，不管业务动词表（文档可列「常见 action」非闭集）。

### 终裁

| 项 | 决 |
|----|----|
| 统一 audit/event | **B：最小版本化信封** |
| A 纯 ad-hoc | 否（不再当规范） |
| action 闭集 Enum | **否**（开放 str） |
| detail 强 schema | **否**（自由 object） |
| 破坏 best-effort | **否** |
| 迁移旧日志 | 不强制；读侧：缺 `schema_ver` 视为 0/legacy |

**一句话：** 钉信封+版号，放开 action/detail；audit 不挡业务。

### 给 dev 指令

**B 生效：`write_audit` 每条写入 `schema_ver: 1`（及现有 ts/actor/action/ok/detail）；`docs/INTERFACE.md` 补信封表；读 tail 兼容无版号旧行。勿引入 action Enum、勿校验 detail 致抛错、勿改 GOAL。pytest 绿即可。**
