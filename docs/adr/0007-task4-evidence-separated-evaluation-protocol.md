# ADR-0007: 任务四采用证据隔离的双轨评估协议

## Status

Accepted - 2026-08-01

任务四的赛题门槛是自动化覆盖率不低于 80%、误报率不高于 5%、漏报率不高于 3%，但现有真实材料只有机动车登记证，现有多单据标签集则全部由合成场景构成。任务四因此采用互不合并的**真实样本轨**与**受控场景轨**，以运行前冻结的**应评校验机会**和独立**评估金标**确定分母；受控场景表现不得冒充真实跨单据性能，点估计通过也不得替代样本充分性与置信界。

Canonical resolution: [GitHub issue #10](https://github.com/Lijika/xiaopeng_comp/issues/10).

## Decision

### 1. 两条轨及可声明范围

- **真实样本轨（R）**只接收真实原始附件及其可追溯派生结果。当前真实材料只支持登记证接入、页/字段证据、样本内核验与人工复核；只有取得同一申请的真实多单据和独立金标后，才可声明真实跨单据指标。
- **受控场景轨（C）**使用纯合成或受控扰动的多单据申请，验证标准化、实体链接、规则和三态路由行为。绑定真实 `step2_sample_id` 但字段值为合成的样本仍属于 C 轨。
- 两轨的数据、分母、标签、区间和结论分别计算，禁止合并。无金标的真实或 external-OCR 数据只能形成 `SMOKE_ONLY` 结果。
- R 轨并列保留 `R-E2E` 与 `R-T4-conditional` 两个视图。前者从原始附件运行到任务四结论并保留上游失败；后者以人工核验的结构化字段证据为固定输入，只隔离评价任务四。两者不得拼接或择优。

### 2. cohort、分区与泄漏防护

- 在看到预测前冻结完整 cohort、纳入/排除规则和文件哈希。真实轨从当版全部可用原始材料出发；缺 step2、OCR、字段证据、标签或处理失败都必须留在相应的上游覆盖、labelability、自动化覆盖或错误统计中，不能静默删除。
- 只允许按预注册规则排除确认重复、非目标文档或完整性失败的对象，并保存原因、数量和哈希。
- 分区在基础 cluster 层、生成扰动前完成。真实轨以申请、登记证样本及可关联车辆/主体为 cluster；同一原件的页面、OCR 版本、裁剪和派生物不得跨区。受控轨以基础场景、实体值和随机种子为 cluster；其全部变体必须留在同一区。
- 使用 `development/regression`、`calibration`、`acceptance_holdout` 三种用途分区，不强制通用比例。样本量由各检查类型的置信界要求倒推；任何已查看标签、错误详情或用于调参的 cluster 永久属于 development。
- 当前 159 个可见标签的 `fixtures/applications` 全部属于 development/regression；正式 acceptance 需要由独立保管标签的新冻结 holdout。

### 3. 主评估单位与金标

主评估单位是运行前清单中的一项：

`(track, base_cluster, application, processing_cycle, check_id, target_scope, evidence_snapshot)`

同一检查在不同目标范围或证据快照中可以形成不同机会。受控场景必须显式声明目标检查；未受扰动的旁路规则只能作回归诊断，不能堆入主分母。申请级“全部必核项是否自动完成”另行报告，不替代校验机会级主指标。

金标状态为：

- `consistent`
- `inconsistent`
- `indeterminate`：证据不足或真实不可判定
- `not_applicable`

只有前两类进入主性能指标。模型的 `uncertain` 是预测状态，不是金标，也不能反向生成金标。

真实轨每个主标签由两名标注员依据原始材料和字段证据独立标注，分歧由第三方裁决；记录样本、单据、页/区域、字段语义、标注协议版本、时间和理由。受控轨标签来自冻结基础值与变换清单，并由独立校验器或复核员核对。单人标签、无定位标签、模型/规则反推标签和没有变换 provenance 的内嵌期望值只能用于开发回归。

### 4. 指标和固定分母

令 `E` 为全部金标属于 `{consistent, inconsistent}` 的应评校验机会，`N_C` 和 `N_I` 分别为其中真实一致和真实不一致的数量。预测状态为 `{consistent, inconsistent, uncertain, skipped, missing, error}`；`missing` 也包括清单中应产生但没有匹配结果的机会。

- `coverage = count(predicted in {consistent, inconsistent}) / |E|`
- `FPR_primary = count(gold=consistent and predicted=inconsistent) / N_C`
- `FNR_primary = count(gold=inconsistent and predicted=consistent) / N_I`
- `miss_rate = count(gold=inconsistent and predicted != inconsistent) / N_I`
- `labelability = count(gold in {consistent, inconsistent}) / count(applicable opportunities)`

`uncertain`、`skipped`、`missing` 和 `error` 都是未覆盖；它们不会从 `E`、`N_C` 或 `N_I` 删除。它们不单独构成 FP/FN，但金标不一致时全部进入 `miss_rate`。另报 `uncertain_on_inconsistent_rate`、跳过率、缺失率和错误率。传统 `FP/(TN+FP)` 只作为“已自动判定条件误报率”辅助项，不是验收主 FPR。

所有指标先报全局，再按 track/view、check family、难度、数据来源、单据组合和扰动族分层。完整范围 `PASS` 要求每个声明支持的必核 check family 均有可估计的 C/I 样本并通过同一组门；总体平均不得掩盖失败或样本不足的检查类型。

### 5. 受控扰动分布

C 轨 acceptance 使用平衡分层压力矩阵，而不伪称真实业务频率：

- 每个 check family 的目标机会按 `consistent:inconsistent = 1:1` 配置。
- 难度分为 `clear`、`boundary`、`degraded-evidence`，各约三分之一，并使用不同基础 cluster。
- 扰动族覆盖语义等价格式变化、明确语义冲突、容差边界、OCR 类替换/缺失/插入、低置信、多候选冲突、单据角色和基数异常。
- 多字段组合攻击属于独立 robustness panel，不混入主矩阵改变权重。
- OCR 扰动率只有在独立真实 OCR 错误审计后才能按实测分布校准；此前只声明受控强度。

### 6. 区间、门槛与样本充分性

点估计按校验机会计算；不确定性以基础样本/申请为 cluster 整体重采样。每轨使用固定种子、10,000 次分层 cluster bootstrap，报告双侧 95% 区间。验收使用单侧 95% 界：coverage 看下界，FPR、FNR 和 miss 看上界。

零错误使 bootstrap 退化时，再按暴露于相应类别的独立 cluster 数计算单侧 95% Clopper-Pearson 上界，并采用更保守的界。有效重采样不足 95%、重采样持续缺少所需类别或独立 cluster 数不足时，指标为 `not estimable`。仅按零错误的精确界估算，FPR 上界不高于 5% 至少需要 59 个独立一致 cluster，FNR 上界不高于 3% 至少需要 99 个独立不一致 cluster；实际 cluster 设计可能要求更多。

正式 `PASS(scope=...)` 必须在同一冻结数据、标签、证据快照、治理策略发布、校验器和评估器版本上同时满足：

- coverage 点估计和单侧 95% 下界均 `>= 0.80`
- FPR 点估计和单侧 95% 上界均 `<= 0.05`
- FNR 点估计和单侧 95% 上界均 `<= 0.03`
- miss 点估计和单侧 95% 上界均 `<= 0.10`

不得跨运行拼接最好结果。任一声明范围内的必核检查类型不可估计或未过门，完整范围就不能 PASS。

### 7. 固定停止、运行状态与重放

正式运行前预注册轨道版本、分层矩阵、目标 cluster 数、最大数据/标注预算、标签协议、规则与知识发布、评估器版本、随机种子和文件哈希。达到样本计划后只执行一次正式盲测；若先达到预算但仍不可估计，则停止为 `INSUFFICIENT`，不能挑样本直到达标。失败后保留快照；任何修复都创建新版本并在完整冻结 holdout 上重跑。

运行只允许以下状态：

- `INVALID`：完整性、provenance、版本、分区、泄漏或评估执行失效；不报性能。
- `INSUFFICIENT`：运行有效，但样本、类别、分层、金标或区间不足；只报告缺口。
- `FAIL`：运行有效且可估计，但任一点估计、置信界或关键分层未过门。
- `PASS(scope=...)`：明确范围内全部检查类型同时过门。
- `SMOKE_ONLY`：无可靠金标，仅证明加载、解析、运行和失败报告链路。

每次正式运行形成不可变**评估运行包**，至少固定数据与排除清单、SHA-256、cluster/split/应评机会、标签与裁决、变换与种子、原始和派生证据引用、治理策略发布、实现与依赖版本、命令、逐项预测、错误、分层指标、区间和最终状态，并为 manifest 自身计算哈希。缺少任何影响分母、标签或预测的版本事实时，运行 `INVALID`。

真实轨运行包留在受控环境；公开仓库只发布不可反推的代号、哈希、聚合统计、公式、区间和复现命令框架。无法同时满足受限数据保护和证据留存时，不得降低任一要求以换取公开结果。

## Current Evidence Verdict

- 原始包经机械核算为 21 本机动车登记证、97 张影像（72 PNG、25 JPG）。step2 覆盖 10 本、44 条页记录，但没有字段文本或一致性标签。OCR 派生产物覆盖 10 本、实际 40 页；10 本有 full result，6 本有 compact result；没有独立人工真值，也没有同申请其他单据。因此当前真实跨单据主评估机会为 0，R 轨真实跨单据指标全部 `not estimable`，状态只能是 `SMOKE_ONLY/INSUFFICIENT`。
- 当前受控资产为 159 个全合成多单据申请、1920 个内嵌标签对（1792 consistent、106 inconsistent、22 uncertain），其中 87 个只绑定真实 step2 样本 ID。现有评估器得到 coverage 0.9885、FPR/FNR/miss 0，但标签公开并与规则共同演进，没有冻结 holdout，且现有实现会让预测结果参与部分分母选择；这些数字只证明开发回归场景通过，不是本协议的正式 PASS，更不是现实性能证据。
- `fixtures/semi` 当前只有 1 个无金标 external-OCR 演示申请，只能产生 `SMOKE_ONLY` 结果。

## Considered Options

- 混合真实、step2-bound 和合成样本形成一个较大的主分母：数字更稳定，但完全丢失证据来源与可声明范围，拒绝。
- 沿用可见 fixture 标签和点估计门槛：复现成本低，但存在同源标签、泄漏、跳过删分母和零错误小样本过度声明，拒绝。
- 两轨隔离、盲测 holdout、固定分母并以 cluster 置信界验收：数据和标注成本更高，但结论可证伪、可重放且不会越过真实证据边界，采用。

## Consequences

- 当前 `task4_consistency.evaluate` 的指标语义和 fixture 组织是待实施迁移项；本 ADR 不修改产品源码、fixtures 或 configs。
- 在取得真实多单据与独立金标前，项目可以声明真实登记证链路覆盖和受控跨单据行为，但不能声明真实跨单据三项指标达标。
- 后续 PRD/实施票据必须把清单、标签、分区、统计、隐私发布和旧评估退役作为同一协议迁移，而不是只替换一个公式。
