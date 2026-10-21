"""
Agent3 — 总结 Agent（纯生成，无工具）
职责：根据排序后的 Top 5 候选餐厅 + 原始 query，生成 2~4 句中文推荐语。
      不做任何计算或重新排序，只负责自然语言描述。

数据分层纪律：
  - 传给 LLM 的只含 Top 5 的压缩摘要（名称/评分/人均/A绕路/B绕路/顺路标签）
  - 完整 POI 数据（坐标等）不传给 LLM

调用方式：
  from agents.agent3_summary import generate_summary
  summary = generate_summary(ranked_pois, query="评分高的火锅店")
"""

import os, sys, json, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_client = OpenAI(
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
)
MODEL = "glm-4-flash"

# ── 顺路标签中文映射 ───────────────────────────────────────────────────────────
_LABEL_CN = {
    "both":     "双方都顺路",
    "one":      "仅一方顺路",
    "none":     "双方均需绕路",
    "fallback": "中间点推荐",
}

# ── System Prompt ─────────────────────────────────────────────────────────────
_SYSTEM = """\
你是一个下班约饭推荐助手。用户会给你搜索意图和几家候选餐厅的信息，请生成 2~4 句自然的中文推荐语。

要求：
1. 必须点出「为什么推荐」——引用真实数据，如绕路时间、评分、人均消费，不要编造数字
2. 标记为「仅一方顺路」的餐厅，要如实说明"只有一方比较顺路"
3. 若某项数据为「数据获取失败」，跳过该项的路线信息，不要引用、不要推测、不要用任何数字代替
4. 语气自然轻松，像朋友推荐，不要营销腔，不要用"尊享""臻选"等词
5. 只输出推荐语本身，不要 JSON，不要标题，不要序号

示例风格：
「海底捞和南门涮肉都不错，两人绕路都在 10 分钟以内，海底捞评分 4.8、人均 120，南门涮肉稍实惠一些。如果不介意 A 多绕一点，清真鸦儿李记的评价也很好，人均只要 87。」
"""


def _build_summary(ranked_pois: list[dict], top_n: int = 5) -> str:
    """
    把 Top N 候选压缩为 LLM 摘要字符串。
    只含做推荐需要的字段，完整坐标等不传给 LLM。
    """
    lines = []
    for i, p in enumerate(ranked_pois[:top_n], 1):
        a_d = p.get("a_route", {}).get("detour_min")
        b_d = p.get("b_route", {}).get("detour_min")
        label = _LABEL_CN.get(p.get("detour_label", ""), "未知")

        # 若 detour_min 为 None，明确标注"数据获取失败"，防止 LLM 用任何数字填充
        a_str = f"{a_d}分钟" if a_d is not None else "数据获取失败"
        b_str = f"{b_d}分钟" if b_d is not None else "数据获取失败"

        lines.append(
            f"#{i} {p.get('name', '?')} | "
            f"评分:{p.get('rating', '?')} | "
            f"人均:{p.get('cost') or '未知'}元 | "
            f"A绕路:{a_str} | B绕路:{b_str} | "
            f"顺路:{label}"
        )
    return "\n".join(lines)


def _fallback_text(ranked_pois: list[dict], query: str) -> str:
    """LLM 失败时的模板兜底推荐语"""
    n = len(ranked_pois)
    both = sum(1 for p in ranked_pois if p.get("detour_label") == "both")
    name = ranked_pois[0].get("name", "候选餐厅") if ranked_pois else "候选餐厅"
    if both > 0:
        return (f"根据「{query}」为你找到 {n} 个候选，"
                f"其中 {both} 家双方都比较顺路。"
                f"综合评分和绕路时间，推荐优先考虑「{name}」。")
    return (f"根据「{query}」共找到 {n} 个候选，"
            f"已按评分和绕路时间综合排序，「{name}」综合表现最佳。")


def generate_summary(ranked_pois: list[dict], query: str = "",
                     retries: int = 2) -> str:
    """
    Agent3 主入口：生成 2~4 句中文推荐语。
    LLM 失败时降级到模板文本，不抛异常。

    输入：
        ranked_pois : enrich_and_rank() 输出，每项含
                      name / rating / cost / a_route / b_route / detour_label / score
        query       : 原始用户 query

    输出：str  — 推荐语文本
    """
    if not ranked_pois:
        return "暂未找到符合条件的餐厅，建议调整搜索词或扩大绕路时间阈值。"

    summary_text = _build_summary(ranked_pois, top_n=5)
    user_msg     = f"搜索意图：{query or '附近好吃的'}\n\n候选餐厅：\n{summary_text}"

    last_err = ""
    for attempt in range(1, retries + 2):
        try:
            resp = _client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.7,   # 推荐语需要一点自然感，略高于解析类任务
                max_tokens=300,
            )
            text = (resp.choices[0].message.content or "").strip()
            if len(text) >= 20:   # 太短说明返回异常
                return text
            last_err = f"返回内容过短: {repr(text)}"
        except Exception as e:
            last_err = str(e)

        if attempt <= retries:
            time.sleep(1.0 * attempt)

    # 全部重试失败，降级
    print(f"[agent3] LLM 失败（{last_err}），使用模板兜底")
    return _fallback_text(ranked_pois, query)


# ── 单测 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 构造 mock 排序结果（模拟 enrich_and_rank 输出）
    mock_ranked = [
        {
            "name": "海底捞火锅(牡丹园店)", "rating": 4.8, "cost": "120.00",
            "detour_label": "both", "score": 2.1,
            "a_route": {"detour_min": 8.0,  "ok": True},
            "b_route": {"detour_min": 9.0,  "ok": True},
        },
        {
            "name": "南门涮肉(河边店)", "rating": 4.8, "cost": "95.00",
            "detour_label": "both", "score": 1.9,
            "a_route": {"detour_min": 11.0, "ok": True},
            "b_route": {"detour_min": 12.0, "ok": True},
        },
        {
            "name": "清真·鸦儿李记·涮肉(和平里店)", "rating": 4.8, "cost": "87.00",
            "detour_label": "one", "score": 0.8,
            "a_route": {"detour_min": 7.0,  "ok": True},
            "b_route": {"detour_min": 22.0, "ok": True},
        },
        {
            "name": "嫩牛家潮汕牛肉火锅(交大店)", "rating": 4.7, "cost": "91.00",
            "detour_label": "both", "score": 1.5,
            "a_route": {"detour_min": 13.0, "ok": True},
            "b_route": {"detour_min": 14.0, "ok": True},
        },
        {
            "name": "汕头八合里海记牛肉店(牡丹园店)", "rating": 4.8, "cost": "116.00",
            "detour_label": "both", "score": 1.7,
            "a_route": {"detour_min": 10.0, "ok": True},
            "b_route": {"detour_min": 11.0, "ok": True},
        },
    ]

    query = "评分高的火锅店"
    print("=" * 60)
    print("Agent3 单测 — 推荐语生成")
    print("=" * 60)
    print(f"\nquery: {repr(query)}")
    print("\n[传给 LLM 的压缩摘要]")
    print(_build_summary(mock_ranked))
    print()

    import time as _t
    t0 = _t.time()
    result = generate_summary(mock_ranked, query=query)
    elapsed = _t.time() - t0

    print(f"[耗时] {elapsed:.2f}s")
    print("\n[生成的推荐语]")
    print(result)
    print("\n" + "=" * 60)
