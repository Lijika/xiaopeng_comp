"""Address normalizer with lightweight alias table."""

from __future__ import annotations

import re

from task4_consistency.normalize.base import basic_clean

# Lightweight geo alias / expansion table (knowledge-graph lite)
_ALIAS_MAP: dict[str, str] = {
    "北京市": "北京",
    "北京": "北京",
    "上海市": "上海",
    "上海": "上海",
    "天津市": "天津",
    "天津": "天津",
    "重庆市": "重庆",
    "重庆": "重庆",
    "江苏省": "江苏",
    "江苏": "江苏",
    "浙江省": "浙江",
    "浙江": "浙江",
    "广东省": "广东",
    "广东": "广东",
    "山东省": "山东",
    "山东": "山东",
    "河南省": "河南",
    "河南": "河南",
    "四川省": "四川",
    "四川": "四川",
    "湖北省": "湖北",
    "湖北": "湖北",
    "湖南省": "湖南",
    "湖南": "湖南",
    "安徽省": "安徽",
    "安徽": "安徽",
    "福建省": "福建",
    "福建": "福建",
    "江西省": "江西",
    "江西": "江西",
    "河北省": "河北",
    "河北": "河北",
    "山西省": "山西",
    "山西": "山西",
    "陕西省": "陕西",
    "陕西": "陕西",
    "辽宁省": "辽宁",
    "辽宁": "辽宁",
    "吉林省": "吉林",
    "吉林": "吉林",
    "黑龙江省": "黑龙江",
    "黑龙江": "黑龙江",
    "云南省": "云南",
    "云南": "云南",
    "贵州省": "贵州",
    "贵州": "贵州",
    "广西壮族自治区": "广西",
    "广西": "广西",
    "内蒙古自治区": "内蒙古",
    "内蒙古": "内蒙古",
    "新疆维吾尔自治区": "新疆",
    "新疆": "新疆",
    "西藏自治区": "西藏",
    "西藏": "西藏",
    "宁夏回族自治区": "宁夏",
    "宁夏": "宁夏",
    "海南省": "海南",
    "海南": "海南",
    "南京市": "南京",
    "南京": "南京",
    "苏州市": "苏州",
    "苏州": "苏州",
    "无锡市": "无锡",
    "无锡": "无锡",
    "杭州市": "杭州",
    "杭州": "杭州",
    "广州市": "广州",
    "广州": "广州",
    "深圳市": "深圳",
    "深圳": "深圳",
    "成都市": "成都",
    "成都": "成都",
    "武汉市": "武汉",
    "武汉": "武汉",
}


def _apply_aliases(text: str) -> str:
    # Longer keys first
    for key in sorted(_ALIAS_MAP.keys(), key=len, reverse=True):
        if key in text:
            text = text.replace(key, _ALIAS_MAP[key])
    return text


def normalize_address(raw: str) -> str | None:
    text = basic_clean(raw)
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    text = text.replace("　", "")
    # Drop common noise
    text = text.replace("中国", "")
    text = _apply_aliases(text)
    # Maintainable KB aliases — address_aliases ONLY (ADV-K9: never apply org_aliases here)
    try:
        from task4_consistency.kb.store import get_kb

        text = get_kb().apply_aliases(text, "address_aliases")
    except Exception:
        pass
    # Unify some punctuation
    text = text.replace("－", "-").replace("—", "-")
    return text if text else None
