"""
Agent1 — Query 解析器
职责：把用户自然语言 query 解析为结构化搜索参数。
输出：{"keyword": str, "min_rating": float, "w1": float, "w2": float, "w3": float}

调用方式：
    from agents.agent1_parser import parse_query
    params = parse_query("找评分高的火锅，不要太贵")

兼容 Python 3.9
"""
from __future__ import annotations

import os
import json
import time
import re
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ── LLM 客户端 ────────────────────────────────────────────────────────────────
_client = OpenAI(
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
)
MODEL = "glm-4-flash"

# ── 降级默认值 ────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "keyword":    "餐厅",
    "min_rating": 0.0,
    "w1":         1.0,   # 评分权重
    "w2":         0.3,   # 总绕路时间惩罚
    "w3":         0.2,   # 绕路公平性惩罚
}

# ── System Prompt ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """\
你是一个餐厅搜索参数提取助手。用户会用自然语言描述想吃什么，你需要从中提取以下字段，以 JSON 格式输出，不要有任何多余文字：

{
  "keyword":    string,   // 高德地图 POI 搜索关键词，尽量简洁（如"火锅"、"日料"、"轻食咖啡"）
  "min_rating": float,    // 最低评分要求，0~5 之间；用户强调"评分高"时取 4.5，"还不错"取 4.0，未提及取 0.0
  "w1":         float,    // 评分权重，0.5~2.0；用户强调"评分高"时取 1.5，否则取 1.0
  "w2":         float,    // 绕路时间惩罚权重，0.1~0.8；用户强调"不要太绕"/"顺路优先"时取 0.6，否则取 0.3
  "w3":         float     // 公平性惩罚权重，0.1~0.5；用户表达"对双方都公平"/"两边时间要差不多"/"不要一边跑太远"/"均等"/"各走一半"/"时间要平衡"/"不要差太多"时取 0.4，否则取 0.2
}

注意：
- keyword 只写核心菜系/品类词，不写"评分高""便宜"等修饰词
- 若 query 为空或无法理解，keyword 填"餐厅"，其他字段填默认值
- 只输出 JSON，不要解释
"""


def _extract_json(text: str) -> dict | None:
    """从 LLM 返回文本中提取 JSON，兼容代码块包裹的情况"""
    # 去掉 ```json ... ``` 包裹
    text = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试找第一个 { ... } 块
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _validate(data: dict) -> dict:
    """校验并修正字段类型与范围，返回合法的参数字典"""
    result = _DEFAULTS.copy()
    try:
        kw = str(data.get("keyword", "")).strip()
        result["keyword"] = kw if kw else _DEFAULTS["keyword"]
    except Exception:
        pass
    for field, lo, hi in [
        ("min_rating", 0.0, 5.0),
        ("w1",         0.1, 3.0),
        ("w2",         0.0, 1.0),
        ("w3",         0.0, 1.0),
    ]:
        try:
            val = float(data.get(field, _DEFAULTS[field]))
            result[field] = max(lo, min(hi, val))
        except Exception:
            pass
    return result


def parse_query(query: str, retries: int = 2) -> dict:
    """
    主入口：解析用户 query，返回结构化参数。
    LLM 失败或格式错误时最多重试 retries 次，最终降级到默认值。

    返回示例：
    {
      "keyword":    "火锅",
      "min_rating": 4.5,
      "w1":         1.5,
      "w2":         0.3,
      "w3":         0.2,
      "_source":    "llm"   # 或 "fallback"（表示使用了降级默认值）
    }
    """
    if not query or not query.strip():
        return {**_DEFAULTS, "_source": "fallback", "_reason": "empty query"}

    last_error = ""
    for attempt in range(1, retries + 2):   # 1, 2, 3
        try:
            resp = _client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": query},
                ],
                temperature=0.1,
                max_tokens=200,
            )
            raw = resp.choices[0].message.content or ""
            data = _extract_json(raw)
            if data is not None:
                result = _validate(data)
                result["_source"] = "llm"
                return result
            last_error = f"JSON 解析失败，原始返回：{raw[:120]}"
        except Exception as e:
            last_error = str(e)

        if attempt <= retries:
            time.sleep(1.0 * attempt)   # 指数退避：1s, 2s

    # 全部重试失败，降级
    return {**_DEFAULTS, "keyword": query, "_source": "fallback", "_reason": last_error}


# ── Agent1 输出 → M2 搜索参数转换 ────────────────────────────────────────────

def agent1_to_search_params(agent1_result: dict) -> dict:
    """
    将 Agent1 的扁平输出转换为 M2 各函数期望的参数结构。

    输入（Agent1 原始输出）：
        {"keyword": "火锅", "min_rating": 4.5, "w1": 1.5, "w2": 0.3, "w3": 0.2, "_source": "llm"}

    输出（M2 消费格式）：
        {
            "keyword":    str,    → 直接传给 search_pois_nearby(keyword=...)
            "min_rating": float,  → 直接传给 search_pois_nearby(min_rating=...)
            "weights":    dict,   → 直接传给 enrich_and_rank(weights=...) 和 score_poi(weights=...)
                {"w1": float, "w2": float, "w3": float}
        }
    """
    return {
        "keyword":    agent1_result.get("keyword",    _DEFAULTS["keyword"]),
        "min_rating": agent1_result.get("min_rating", _DEFAULTS["min_rating"]),
        "weights": {
            "w1": agent1_result.get("w1", _DEFAULTS["w1"]),
            "w2": agent1_result.get("w2", _DEFAULTS["w2"]),
            "w3": agent1_result.get("w3", _DEFAULTS["w3"]),
        },
    }


# ── 单测 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_cases = [
        "找评分高的火锅，不要太贵",
        "想吃日料，要安静一点，对两个人都公平",
        "随便，附近有什么好吃的",
        "轻食沙拉，最好是网红店",
        "",   # 空查询，测试降级
    ]

    print("=" * 55)
    print("Agent1 单测 — Query 解析器")
    print("=" * 55)
    for q in test_cases:
        print(f"\n📝 输入：{repr(q)}")
        result = parse_query(q)
        source = result.pop("_source", "?")
        reason = result.pop("_reason", "")
        print(f"   来源：{'🤖 LLM' if source == 'llm' else '⚠️  降级默认值'}" +
              (f"（原因：{reason}）" if reason else ""))
        print(f"   关键词：{result['keyword']}")
        print(f"   最低评分：{result['min_rating']}")
        print(f"   权重 w1={result['w1']}  w2={result['w2']}  w3={result['w3']}")
    print("\n" + "=" * 55)
