# 设计说明 — 任务4 跨单据一致性校验

## 1. 问题

融资租赁放款审核中，同一申请多源单据（登记证、保单、合同、发票、证件）字段表述不一致、OCR 噪声导致精确字符串比对误报高；需标准化、可配置规则与可解释三态结论。

## 2. 架构

```
Application JSON → Normalize (+ KB aliases) → RuleEngine (YAML) → Report (consistent|inconsistent|uncertain)
                                      ↑
                         Web UI / CLI / evaluate
```

- **normalize/**：类型化标准化  
- **kb/**：可维护实体别名库  
- **match/**：exact / fuzzy / numeric / list  
- **rules/**：加载 + handler 注册表引擎  
- **web/**：演示、规则维护、KB 管理  
- **evaluate/**：标签集指标  

详见 `ARCHITECTURE.md`。

## 3. 关键设计决策

| 决策 | 理由 |
|------|------|
| 与 OCR 解耦 | 任务4 吃结构化字段；影像属任务1–3 |
| 三态 + miss_rate | 控误报；暴露 uncertain 隐匿漏检 |
| YAML + Web 热改 runtime 副本 | 业务可维护且默认可回滚 |
| 合成评估 + 诚实边界 | 官方全量标注未提供时仍可回归 |

## 4. 扩展点

新规则 type → 注册 handler；新实体类型 → KB 类别 + normalizer。
