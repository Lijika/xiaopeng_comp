# step2 → 任务4 数据衔接说明

## 结论回顾
见 `TASK4_DATA_REQUIREMENTS_AND_STEP2_ANALYSIS.md`：step2 有框无字，不能单独跑任务4真交叉核验。

## 本仓库衔接产物

| 产物 | 路径 | 用途 |
|------|------|------|
| 框位清单 | `fixtures/ocr_inbox/step2_slots_<sample_id>.json` | 列出 VIN/发动机号等框 + `1.zip` 内路径；`raw=null` 待 OCR |
| 演示申请 | `fixtures/applications/app_demo_step2_*.json` | 绑定 `meta.step2_sample_id` 的多单据仿真字段 |
| 外部 OCR 导入 | `scripts/import_external_ocr.py` | OCR 填字后导入 `fixtures/semi` |
| 评估分家 | `evaluate --suite main\|semi` | 主数字只认 main |

## 推荐人工/外部步骤

1. 用 `step2_slots_*.json` 中的 `zip_member` + `bbox` 从 `1.zip` 裁剪  
2. 外部 OCR 写入各 slot 的 `raw`  
3. 组装为 Application JSON（至少登记证字段；跨单据仍需保单/合同等）  
4. `import_external_ocr` → semi 套件冒烟；main 仍用标签集报指标  

## 禁止

- 用假 OCR 生成器刷任务4指标  
- 把 step2 说成任务3/4 已完成  
