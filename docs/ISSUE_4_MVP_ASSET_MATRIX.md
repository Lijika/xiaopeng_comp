<!-- issue4-asset-matrix-20260801 -->
# 现有 MVP 资产矩阵

关联票据：[盘点现有 MVP 的可复用资产、耦合与技术债务](https://github.com/Lijika/xiaopeng_comp/issues/4)

## 范围与判读规则

本矩阵只盘点当前工作树中的 MVP，记录已观察到的事实、事实带来的影响范围和未被证据证明的事项。它不是保留、封装、重写或废弃的裁决，也不实现或修复产品。

术语遵循 `codebase-design`：**module** 指有 interface 的实现单元；**seam** 指 interface 所在的位置；**adapter** 指在 seam 上把一种输入形状接入另一种形状的实现；**depth** 以 caller 可从 interface 获得的行为衡量；**leverage** 与 **locality** 分别描述复用和维护事实集中程度。下文的深浅、耦合和债务均是当前观察，不是处置建议。

## 证据索引

| ID | 证据 | 观察结果 |
| --- | --- | --- |
| S1 | `.codegraph` 存在；先执行 `codegraph explore`，再核对所列源码和调用路径 | 定位了 `RuleEngine`、`load_rules`、HTTP interface、评估、报告与 adapter 的 current source/call paths。 |
| R1 | `.venv/bin/pytest -q` | `174 passed`，9 条警告；警告来自刻意构造的小分母指标测试和 `TestClient` 的上游弃用提示。 |
| R2 | `.venv/bin/python -m task4_consistency.cli evaluate fixtures/applications -c configs/rules_auto_lease.yaml -o out/metrics.json` | `suite=main`、`mode=labeled`；159 个样本均加载，1,920 个带标签对，coverage 0.9885、FPR/FNR/miss_rate 均 0；四个门槛通过。运行输出明确 main 为 synthetic/Step2-bound 轨，不是 real OCR 声明。 |
| R3 | `.venv/bin/python scripts/attack_probes.py` | 19 个探针均为 `CLOSED`，`release_open=0`、`r7_open=0`。 |
| R4 | `.venv/bin/python scripts/smoke_web.py` | `health`、fixtures、一次 `/api/check` 和 HTML shell 均通过；159 个 fixtures，`SMOKE_WEB PASS`。 |
| R5 | Step2 适配器 CLI：`.venv/bin/python -m task4_consistency.adapters.step2_page_order data/step2/JFL25P02L080310-01_page_order.json`，随后以同一规则包执行 | 生成一份登记证 document、17 个字段，所有 `raw=null`、`field_source=null`；规则执行结果为 10 uncertain、2 skipped、1 consistent。 |
| R6 | 外部 OCR adapter 在临时目录调用 `import_external_ocr_to_dir(...)` | 写出 `APP-EXT-OCR-DEMO-01.json`（6,770 bytes），输出同时含 `field_source=external_ocr` 与 `source=external_ocr`；未写入仓库 fixture。 |
| R7 | `.venv/bin/python -m task4_consistency.cli evaluate --suite semi --mode smoke -c configs/rules_auto_lease.yaml -o out/metrics-semi-smoke.json` | 1 个 external-OCR fixture 读取和执行成功；无可靠标签，FP/FN/coverage gates 不计算。 |
| R8 | `TestClient` 只读调用 `/api/step2/samples`、`/api/ocr_inbox`、`/api/check/batch` | HTTP interface 返回 10 个 Step2 样本、10 个 slot manifests；两份 fixture batch 返回 `n=2`，汇总为 21 consistent、1 inconsistent、0 uncertain、4 skipped。 |
| D1 | 当前 fixture/data/config 结构化汇总 | 159 个 main fixtures，全部 `field_source=synthetic` 且全部带嵌入式 `expected_verdicts`；1 个 semi fixture；10 个 Step2 page-order 文件；10 个 slot manifests。 |

## 资产矩阵

### A1. 申请 JSON 传输 module

- **权威 owner 与 interface：** `task4_consistency/models.py:24-225`；`Application.from_dict()/to_dict()`、`Document.from_dict()/to_dict()` 与 `FieldValue.from_obj()/to_dict()` 是 current JSON seam。
- **可观察行为：** application 输入成为 `Application -> list[Document] -> dict[str, FieldValue]`；一个字段键对应一个 `FieldValue`，含 `raw`、confidence、source_page、normalized、OCR notes。除 `application_id` 与 `documents` 外的顶层键被保留在 `Application.meta`（`models.py:92-113`）。R4 的 `/api/check` 和 R2 的 159 份 fixture 都跨过该 interface。
- **调用者与 blast radius：** `RuleEngine`、CLI、HTTP interface、report、evaluation 和三类 adapter 都依赖这一个传输形状；`tests/test_rules_engine.py`、`tests/test_cli.py`、`tests/test_web_kb.py` 覆盖该形状的代表路径。
- **当前耦合、隐含假设与 locality：** document 识别依赖字符串 `doc_type`，字段依赖字符串键；一份 document 内每个字段只有一个 current `FieldValue`。因此 caller 必须在这个 interface 外表达附件、页归属、document role、多个候选观察和选择历史；这些概念在 current module 中没有独立表示。该 interface 给调用者带来统一 JSON leverage，但其证据语义 locality 只到单值字段。
- **未证实项：** 未见真实多附件/多实例申请对重复 document type、角色选择或证据版本的运行证据；未见面向外部集成方的版本兼容契约。

### A2. 标准化与匹配 module 群

- **权威 owner 与 interface：** `task4_consistency/normalize/base.py:14-302` 的 `normalize_field_ex()` / `normalize_field()` 和注册表；`task4_consistency/match/*.py` 的 exact、fuzzy、numeric、list interfaces。
- **可观察行为：** 引擎在每次 `run()` 内按 field name/field_type 推断 normalizer，保留 OCR 修正与 notes（`rules/engine.py:57-91`）。VIN、日期、金额、身份证、车牌、地址、品牌等具有专用实现；brand normalizer 读取 KB alias（`normalize/base.py:144-170`）。R3 实际覆盖了品牌合资、VIN I/O/Q、日期歧义、低置信、金额约数和 ID15/18 等行为。
- **调用者与 blast radius：** `RuleEngine` 是主要 caller；CLI、HTTP、evaluation、reports、fixtures 与攻击探针通过它获得结果。`tests/test_normalize_*.py`、`tests/test_match.py`、`tests/test_adversary.py` 和 R3 提供测试/运行证据。
- **当前耦合、隐含假设与 depth：** 一个较深的 `normalize_field_ex` interface 隐藏了多种格式规则，给 rule callers 带来 leverage；同时 field 类型路由依赖命名表 `FIELD_TYPE_HINTS`，品牌/地址的结果又依赖进程全局 KB。异常被转换为 `NormalizeResult(value=None)`（`normalize/base.py:271-286`），所以 caller 接收到的是不可标准化结果而非底层异常类别。
- **未证实项：** 未见真实 OCR 文本分布、跨文档字段别名冲突或用户维护 alias 后的统计影响证据。

### A3. YAML 规则包与 critical guard module

- **权威 owner 与 interface：** `task4_consistency/rules/loader.py:35-358` 的 `RuleDef`、`RuleConfig`、`load_rules()`；默认事实源是 `configs/rules_auto_lease.yaml:1-211`；强制语义由 `rules/critical_guard.py:16-143` 保有。
- **可观察行为：** loader 限定五种 rule type、缺失策略、严重级别、字段 alias、容差和 critical identity rules；加载时执行 package policy 和三个 critical fingerprint。当前 YAML 有 13 条规则，配置了中文 document type、field alias 和标准化开关。R2、R3、R4 均成功加载同一个规则包。
- **调用者与 blast radius：** CodeGraph 记录 `load_rules` 有 77 个 callers、`RuleEngine` 有 57 个 callers，覆盖 CLI、脚本、HTTP interface 和测试；`tests/test_round16.py`、`tests/test_p0_p1_regressions.py` 与 R3 覆盖其 policy/guard 行为。
- **当前耦合、隐含假设与 locality：** rule definition 将 document type、field naming、缺失处理、阈值和转移姓名策略集中在一个 package，带来 policy locality；同一字符串集合同时出现在 fixtures、engine 选择逻辑与 Web 编辑表单。critical fingerprint 的权威在代码而非 YAML，任何 runtime rule writer 必须经过该 seam。
- **未证实项：** 未见规则管理员的实际审批、版本发布、回滚、多人并发维护或业务政策来源证据。

### A4. RuleEngine 执行 module

- **权威 owner 与 interface：** `task4_consistency/rules/engine.py:25-677`；公开入口是 `RuleEngine(config).run(application) -> Report`。
- **可观察行为：** engine 先规范化，再逐条分派 exact/fuzzy/numeric/list/conditional rule；按照 `docs_by_type()` 与 alias 收集字段，处理缺失、低置信、OCR 修正、金额约数和二手车姓名分支，并输出 snapshots、flags、reason codes。R2 对 159 个 fixture 跑完；R5 的 layout-only Step2 application 得到 10 uncertain/2 skipped，证明 raw-null 数据不会被当作跨单据可比值。
- **调用者与 blast radius：** CLI `check/evaluate`、HTTP `/api/check` 与 `/api/check/batch`、evaluation、攻击脚本以及至少 11 个 engine-focused 测试文件直接或间接调用。该 module 的 interface 是评估、报告和 Web 的共同 seam，故规则或数据形状改变会扩散到所有这些 callers。
- **当前耦合、隐含假设与 depth：** `run()` 是深 interface：一次调用涵盖规范化、字段选择、比较和 report 构造，给 callers 较高 leverage。字段选择按 `doc_type` 将同类 document 的命中值全部聚集（`engine.py:93-149`），而非依据 document role、实体关系、附件页归属或指定 evidence snapshot；执行仅在内存中生成结果。
- **未证实项：** 多个同 type document、多个实体、角色歧义、并发重跑和历史 run 可追溯性在当前运行证据中未被证明。

### A5. 结果、reason code 与可读报告 module

- **权威 owner 与 interface：** `models.py:123-225` 的 `CheckResult`/`Report`；`report.py:19-262` 的 `build_report()`、JSON/Markdown/HTML writers；`reason_codes.py:7-181` 的 `infer_reason_codes()`。
- **可观察行为：** report 统计 consistent/inconsistent/uncertain/skipped 与 coverage，呈现 snapshots、diff、flags、reason codes；CLI 可输出 JSON、Markdown 和 HTML。R2 写入 metrics，R4 从 `/api/check` 取得 `report` 和 HTML，R3 验证 VIN 等 reason code。
- **调用者与 blast radius：** `RuleEngine` 创建 result，CLI/Web/report tests 和浏览器 `renderReport()` 消费字段；`tests/test_report.py`、`tests/test_round2.py`、`tests/test_round4.py` 与 R4 是代表证据。
- **当前耦合、隐含假设与 locality：** 报告 interface 让 CLI 和 HTTP 共用同一 result 形状，形成一定 leverage；浏览器 UI 直接读取 summary/checks/snapshots/diff/flags，接口字段名变动会跨 Python/JS seam 传播。HTML/Markdown 输出包含 `raw` 和 normalized 字段值（`report.py:77-250`）。
- **未证实项：** 未见报告与人工处置、例外、复核队列或不可变 audit event 的关联；未见面向生产身份与脱敏的呈现证据。

### A6. CLI、fixture 评估与指标 module

- **权威 owner 与 interface：** `cli.py:36-255` 的 `check` / `evaluate` 子命令；`evaluate.py:33-631` 的 `evaluate_paths()` / `evaluate_suite()` / `Metrics`；fixture 根为 `fixtures/applications` 与 `fixtures/semi`。
- **可观察行为：** `check` 接受单个 application JSON 和规则包，可写 JSON/Markdown/HTML；`evaluate` 加载 fixture、优先使用 fixture 内 `expected_verdicts`，在 labeled/smoke 模式分别产生阈值或仅稳定性结果。R2 输出 159 main synthetic fixtures 的可重复数字，R7 输出 1 个 external-OCR semi fixture 的 smoke-only 结果。
- **调用者与 blast radius：** CLI、HTTP `/api/evaluate/summary`、`scripts/demo.sh`、测试和攻击探针依赖 evaluation；`tests/test_cli.py`、`tests/test_evaluate.py`、`tests/test_evaluate_suite.py`、`tests/test_round20.py` 是代表覆盖。
- **当前耦合、隐含假设与 locality：** metrics 定义集中在 `evaluate.py:1-18,163-295`，有利于指标计算 locality；main suite 的全部 159 个 fixture 都是 synthetic 且都嵌入 expected verdicts（D1），评估路径因此同时依赖 fixture 数据和其中的标签。semi suite 明确不把 smoke 作为 FP/FN/coverage evidence。
- **未证实项：** 独立人工真值来源、真实跨 document 样本的分母、标签审阅流程和跨版本可比性均未被当前证据证明。

### A7. Step2 page-order adapter

- **权威 owner 与 interface：** `adapters/step2_page_order.py:11-90`；`page_order_to_application(data)`、`load_page_order(path)` 以及 `python -m task4_consistency.adapters.step2_page_order`。
- **可观察行为：** adapter 将 page-order detection 的 class 映射到少数 field keys，保留最高 confidence detection 的 page order；没有 OCR 文本时将 `raw=None`，并设 `source=step2_page_order`、`field_source=None`。R5 的实际 CLI/engine 运行证明该 interface 生成 layout-only application 而不是可完成的多 document 一致性输入。
- **调用者与 blast radius：** `tests/test_adapter_step2.py`、`tests/test_external_ocr_import.py` 和 Step2-related fixtures/tests 使用它；Web 读取同一 `data/step2` 的文件但不是调用该 adapter。
- **当前耦合、隐含假设与 adapter seam：** class label 到 field key 的映射固定在 `_CLASS_TO_FIELD`；未知 class 被保存为 `det_<id>`；sample_id 同时成为 application_id 和 document_id，默认 document type 为登记证。这个 adapter seam 保留位置与 confidence，却假设一份 page-order 样本可表示一份单 document application。
- **未证实项：** 未见该 detection 结果与原始页图、OCR 文本、多个 document instance 或真实业务 application 的端到端绑定证据。

### A8. Step2 OCR slot-manifest adapter

- **权威 owner 与 interface：** `adapters/step2_slots.py:13-81`；`validate_step2_slots()`、`load_step2_slots()`、`list_step2_slot_files()`；schema 是 `task4.external_ocr_slots.v1`。
- **可观察行为：** module 只验证 sample_id、doc_type、slot 的 field/bbox/raw 和可选 n_slots；raw 可为 null 或字符串。R8 的 HTTP 列表返回 10 个 manifests；`tests/test_step2_slots.py` 覆盖 schema、bbox、空字段和列举行为。
- **调用者与 blast radius：** 当前 Web 仅列出 manifest 摘要（`web/app.py:384-409`）；测试直接调用 validator/loader。完整文件未提供 slot-to-`Application` 转换 interface。
- **当前耦合、隐含假设与 locality：** manifest interface 将 crop/OCR 前置结构集中在 `fixtures/ocr_inbox`，但其输出没有同 current application seam 的 adapter；OCR 填值、页面区域与后续 document field 的关联需要在该 module 之外发生。
- **未证实项：** 未见 crop 生成、外部 OCR 回填、bbox 坐标系、slot 与 source page 的生产级关联或失败恢复的运行证据。

### A9. 外部 OCR 中间格式 adapter

- **权威 owner 与 interface：** `adapters/external_ocr_import.py:15-207`；`validate_external_ocr_payload()`、`external_ocr_to_application()`、`import_external_ocr_to_dir()`；脚本 seam 为 `scripts/import_external_ocr.py:24-57`。
- **可观察行为：** schema_version=1 输入要求 OCR model/version、application_id、documents 和可选字段 confidence；转换时强制 `source=external_ocr`、`field_source=external_ocr`，离线 import 写 semi-style JSON。R6 在临时目录证实文件输出和 provenance；R7 证实仓库中唯一 semi fixture 可加载执行但没有 labels。
- **调用者与 blast radius：** `tests/test_external_ocr_import.py` 覆盖 schema、路径约束、输出 provenance；CLI-style script 和 semi evaluation 消费该路径。
- **当前耦合、隐含假设与 adapter seam：** adapter 接受的上游形状与 Application transport 直接对齐，提供较高 adapter leverage；写入路径受 repo_root 与 2 MB cap 约束。字段的 source_page/field_type 可选，未要求原始附件标识、bbox、候选集或 extraction run identity；当前没有 HTTP upload/ingress interface 调用该 adapter。
- **未证实项：** 未见真实外部 OCR provider、鉴权、批量导入、重复/覆盖语义、人工标注或多 document 真实样本的证据。

### A10. 实体知识 module

- **权威 owner 与 interface：** `kb/store.py:11-227` 的 `EntityKB`、`get_kb()`、`reload_kb()`；持久文件为 `configs/kb/entity_kb.json`；模块级 interface 在 `kb/__init__.py:10-45`。
- **可观察行为：** JSON 读取 address/org/plate aliases，graph 的 `same_as` 仅投影 address/org aliases；可增删 alias，地址 alias 阻止已知城市之间重映射。D1 显示当前文件有 19 个地址 alias、5 个机构 alias、7 个车牌前缀和 8/5 个 graph nodes/edges；R4 health 表明 KB 可加载。
- **调用者与 blast radius：** 地址/品牌 normalizer、HTTP KB endpoints 和 tests 使用 process-global singleton；`tests/test_web_kb.py`、`tests/test_round17.py`、`tests/test_kb_graph_project.py` 提供代表证据。
- **当前耦合、隐含假设与 locality：** alias knowledge 集中于一个 JSON owner，有对 normalizer 的 leverage；`get_kb()` 的全局 instance 与 `save()` 的直接 `write_text()` 使运行中的写入与同一路径/同一进程状态耦合。graph 的非 `same_as` 边以及非 address/org 前缀不投影到 normalizer。
- **未证实项：** 未见跨进程写入、版本治理、审批/回滚、实体链接决策或真实知识库来源的证据。

### A11. JSONL audit module

- **权威 owner 与 interface：** `audit.py:16-113`；`write_audit()`、`read_audit_tail()`、`audit_status()`，默认文件为 `out/audit.log` 或 `TASK4_AUDIT_LOG`。
- **可观察行为：** append JSONL 记录 schema_ver、时间、action、actor、ok 和 detail；读取兼容旧记录。Web 为认证拒绝、规则保存/重置、KB 增删调用写入（`web/app.py:48-90,657-831`），R4 health 返回 audit status。
- **调用者与 blast radius：** Web mutable paths 与 audit tests 是直接 callers；`tests/test_round14.py`、`tests/test_round22.py`、`tests/test_round43_audit_schema.py` 覆盖写入、尾读和 legacy normalization。
- **当前耦合、隐含假设与 locality：** audit file 和 module-level lock 为同进程 append 提供 locality；`/api/check` 的实现（`web/app.py:457-515`）不调用 `write_audit()`，故 current audit interface 记录的是部分运维动作而不是每一次校验/人工处置事实。`detail` shape 不在 module 内验证。
- **未证实项：** 未见跨进程写入、不可篡改存储、保留期、审核查询授权或与 application/check-run 的稳定关联证据。

### A12. FastAPI HTTP interface 与运行时文件路径

- **权威 owner 与 interface：** `web/app.py:33-836`，包括 health、fixture/Step2/slot 列表、单笔/批量 check、evaluation、rules、KB、audit routes；`OptionalTokenAuth` 位于 `web/app.py:48-90`。
- **可观察行为：** R4 通过 health、fixture、check 和 HTML shell；R8 通过 Step2、slot 和 batch check。单笔 check 可以选择 `rules_path`，batch 上限 50（`web/app.py:457-602`）；rules 写路径验证后用临时同级文件/`os.replace()`，KB 及 audit 均有可变文件副作用。
- **调用者与 blast radius：** 浏览器 JS、TestClient tests、Web smoke 与攻击脚本跨此 seam；`tests/test_web_kb.py`、`tests/test_step2_api.py`、`tests/test_round14.py`、`tests/test_round16.py`、`tests/test_round20.py` 是代表覆盖。
- **当前耦合、隐含假设与 depth：** 一个 HTTP interface 聚合 engine、report、KB、audit 和 filesystem runtime rules，具有调用侧 leverage，也令这些 module 的状态在同一部署 seam 汇合。未设置 `TASK4_WEB_TOKEN` 时 middleware 放行；设置时 health/static/index 保持公开、其余 routes 需 token。`rules_path` 相对路径会加 ROOT，绝对路径保留为绝对路径，随后只检查存在并由 loader 读取（`web/app.py:488-502`）。
- **未证实项：** 未见角色分工、持久化 application lifecycle、多进程文件锁、队列/取消/重试、部署环境或实际浏览器认证流的运行证据。

### A13. 浏览器工作台 module

- **权威 owner 与 interface：** `web/static/app.js:1-636`、`web/templates/index.html`、`web/static/style.css`；浏览器通过 `api(path, opts)` 跨 HTTP seam。
- **可观察行为：** UI 固定提供 5 个 scenario fixture，读取 fixture 后在 `renderDocMatrix()` 中按 fields/documents 展示矩阵，`runCheck()` 调用 `/api/check` 并渲染 report；rules、Step2、slot、KB 路径分别调用对应 HTTP routes（`app.js:33-65,91-103,148-289,421-616`）。
- **调用者与 blast radius：** 静态 JS 是这些 route response 形状的 caller；CodeGraph 对 `api()` 记录 16 个 callers。R4 验证 HTML shell 和后端 check，但 current tests 通过 TestClient；未定位浏览器自动化测试覆盖 `app.js` 渲染与交互。
- **当前耦合、隐含假设与 locality：** UI 对 report/fixture/rules/KB JSON 的字段名和本地 scenario 文件名直接耦合；其 interface 直接呈现 document raw 值和 checks。前端没有自己的 adapter layer，因此 HTTP response 改动的 locality 跨 JS/Python seam 扩散。
- **未证实项：** 未见实际浏览器、移动视口、键盘/辅助功能、身份失效、网络失败恢复或用户复核流程的运行证据。

### A14. Fixture、测试与运行 harness

- **权威 owner 与 interface：** `fixtures/applications`、`fixtures/semi`、`data/step2`、`fixtures/ocr_inbox`、`tests/` 与 `scripts/`；主要执行 interfaces 是 pytest、CLI evaluate、`scripts/attack_probes.py`、`scripts/smoke_web.py` 和 `scripts/demo.sh`。
- **可观察行为：** D1 和 R1-R8 共同证明当前 corpus/harness 可重复运行；R2 的输出文件为 `out/metrics.json`，R7 为 `out/metrics-semi-smoke.json`。main 的 159 份 fixture 全部含 embedded expected verdicts，evaluation 代码优先读取该字段（`evaluate.py:113-142,382-433`）。
- **调用者与 blast radius：** 规则、normalizer、engine、reports、HTTP routes、adapter 与 KB 都由 tests/harness 触达；该群是当前行为回归的主要可执行证据和 caller 集合。
- **当前耦合、隐含假设与 leverage：** 单一 fixture protocol 同时承担 demo、规则预期、评估分母和攻击样例，带来较高测试 leverage；它也使指标与 fixture 内嵌标签具有共同数据 owner。`scripts/run_web.sh` 启动带 reload 的 Uvicorn，`scripts/demo.sh` 写 `out/demo`；这些路径与本地文件系统和可选 Web dependencies 耦合。
- **未证实项：** 未见独立标注审计、真实跨 document application corpus、产线数据漂移、容量/延迟基线或独立浏览器端到端测试。

## 当前端到端路径（事实摘要）

1. **Synthetic demo/evaluation：** fixture JSON -> `Application` transport seam -> `RuleEngine.run()` -> `Report` -> CLI/HTTP/browser render。R2、R4、R8 证明该路径可执行。
2. **Step2 layout path：** `data/step2/*_page_order.json` -> Step2 adapter 或 Web listing -> raw-null document fields -> engine yields uncertain/skipped for unavailable values。R5 证明该路径不会自行成为跨 document字段值输入。
3. **External OCR intermediate path：** schema-v1 JSON -> validate/convert adapter -> semi fixture JSON -> engine/evaluate smoke。R6-R7 证明格式转换和执行，不证明有标签的准确率。
4. **Policy/knowledge mutation path：** browser HTTP interface -> rules/KB writer -> runtime YAML/KB JSON -> active engine / audit JSONL。源码与测试证明该 path 存在；本票据未调用其可变 HTTP routes。

## 未作出的裁决

- 本票据没有对任何 module 或端到端路径作保留、封装、重写或废弃决定。
- 本票据没有把 synthetic main 指标或 semi smoke 结果表述为真实 OCR、真实跨 document 或生产表现。
- 本票据没有创建或进入下一张决策票；由地图的后续 frontier 决定下一步。
