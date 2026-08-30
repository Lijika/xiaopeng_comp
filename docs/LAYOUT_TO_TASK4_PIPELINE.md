# 登记证版面 → 任务4 衔接

登记证版面 JSON 只有页序与检测框，没有字段文本，不能单独做跨单据核验。

## 仓库产物

| 产物 | 路径 | 用途 |
|------|------|------|
| 版面 | `data/registration_layout/*_page_order.json` | 页序、页类型、检测框 |
| 槽位清单 | `fixtures/layout_slots/layout_slots_<sample_id>.json` | 字段框；`raw=null` 待填字 |
| 演示申请 | `fixtures/applications/app_demo_layout_*.json` | 绑定 `meta.layout_sample_id` 的多单据仿真字段 |
| 外部 OCR 导入 | `scripts/import_external_ocr.py` | 填字后导入 `fixtures/semi` |
| 评估分家 | `evaluate --suite main\|semi` | 主数字只认 main |

## 填字步骤

1. 按槽位 `image_filename` + `bbox` 定位登记证区域  
2. 外部 OCR 写入各 slot 的 `raw`  
3. 组装为 Application JSON（至少登记证字段；跨单据仍需保单/合同等）  
4. `import_external_ocr` → semi 套件冒烟；main 仍用标签集报指标  

## 禁止

- 用假 OCR 生成器刷任务4指标  
- 把版面 JSON 说成任务3/4 已完成  
