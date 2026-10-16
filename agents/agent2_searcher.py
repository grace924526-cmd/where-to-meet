"""
Agent2 — 搜索 Agent
职责：给定 A/B 双方工作地坐标 + Agent1 输出的搜索参数，
      找出合适的候选餐厅 POI 列表，供后续绕路时间计算使用。

流程：
  1. 工具1 get_search_center()  → 计算搜索中心点和半径（纯 Python，无 API）
  2. 工具2 fetch_pois()         → 调高德 place/around 拿原始 POI（最多 20 个）
  3. LLM 语义筛选               → 只看压缩摘要，返回 selected_ids（最多 10 个）
  4. Python 用 selected_ids 从完整 POI 中筛选，返回供绕路计算的候选列表

数据分层纪律：
  - 完整 POI 数据（坐标/营业时间等）只存 Python 侧
  - LLM 只看压缩摘要：{序号, name, address, rating, cost, dist_to_center_m}

调用方式：
  from agents.agent2_searcher import search_candidates
  pois = search_candidates(a_work, b_work, search_params, city="北京", query="火锅")

兼容 Python 3.9
"""
from __future__ import annotations

import os, sys, math, json, time, re
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from openai import OpenAI
from dotenv import load_dotenv
from core.geo_utils import find_midpoint, dynamic_radius, search_pois_nearby, haversine_km

load_dotenv()

# ── LLM 客户端 ────────────────────────────────────────────────────────────────
_client = OpenAI(
    api_key=os.getenv("ZHIPU_API_KEY"),
    base_url=os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
)
MODEL = "glm-4-flash"


# ══════════════════════════════════════════════════════════════════════════════
# 工具1：计算搜索中心点和半径
# ══════════════════════════════════════════════════════════════════════════════

def get_search_center(a_work: dict, b_work: dict) -> dict:
    """
    工具1：根据 A/B 双方工作地，计算 POI 搜索的中心点和动态半径。
    直接复用 app_v2.py 的 find_midpoint() / dynamic_radius()，零 API 调用。

    输入：
        a_work / b_work: {"lng": float, "lat": float, "name": str}
    输出：
        {"center": {"lng": float, "lat": float},
         "radius": int,          # 米，最大 5000
         "distance_km": float}   # A/B 直线距离，供调试
    """
    center      = find_midpoint(a_work, b_work)
    radius      = dynamic_radius(a_work, b_work)
    distance_km = haversine_km(a_work, b_work)
    return {
        "center":      center,
        "radius":      radius,
        "distance_km": round(distance_km, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 工具2：高德周边 POI 搜索
# ══════════════════════════════════════════════════════════════════════════════

def fetch_pois(center: dict, keyword: str, radius: int,
               min_rating: float = 0.0, city: str = "") -> list[dict]:
    """
    工具2：以 center 为圆心搜索周边 POI。
    直接复用 app_v2.py 的 search_pois_nearby()，返回完整 POI 列表（最多 20 个）。
    搜索失败或结果为空时返回空列表，不崩溃。

    完整 POI 数据（含坐标）只在 Python 侧使用，不传给 LLM。
    """
    try:
        pois = search_pois_nearby(center, keyword, radius, min_rating, city)
        return pois if pois else []
    except Exception as e:
        print(f"[agent2] fetch_pois 异常: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# 压缩摘要：完整 POI → LLM 可见的轻量字段
# ══════════════════════════════════════════════════════════════════════════════

def _build_summary(pois: list[dict], center: dict) -> list[dict]:
    """
    把完整 POI 压缩为 LLM 摘要，只保留做决策需要的字段。
    距离以米为单位，四舍五入到整数。
    """
    summaries = []
    for i, p in enumerate(pois):
        dist_m = round(haversine_km(p["location"], center) * 1000)
        summaries.append({
            "idx":            i,                           # Python 侧索引，用于还原完整数据
            "name":           p["name"],
            "address":        p["address"],
            "rating":         p["rating"],
            "cost":           p["cost"] or "未知",
            "dist_to_center": dist_m,                      # 到搜索中心的距离（米）
        })
    return summaries


# ══════════════════════════════════════════════════════════════════════════════
# LLM 语义筛选
# ══════════════════════════════════════════════════════════════════════════════

_FILTER_SYSTEM = """\
你是一个餐厅筛选助手。用户会给你一段搜索意图和一批候选餐厅摘要，
请根据意图选出最相关的餐厅，返回它们的 idx 列表（最多10个，按相关度降序），
只输出合法 JSON，格式为：{"selected_idx": [0, 3, 5, ...]}
不要有任何解释文字。
"""

def _llm_filter(query: str, summaries: list[dict], retries: int = 2) -> list[int] | None:
    """
    调 GLM-4-Flash，根据 query 语义从摘要中筛选最相关的 idx 列表。
    失败时返回 None（调用方负责降级）。
    """
    # 把摘要格式化为紧凑文本，控制 token
    lines = []
    for s in summaries:
        lines.append(
            f"[{s['idx']}] {s['name']} | 评分:{s['rating']} | "
            f"人均:{s['cost']}元 | 距中心:{s['dist_to_center']}m | 地址:{s['address']}"
        )
    summary_text = "\n".join(lines)
    user_msg = f"搜索意图：{query}\n\n候选餐厅：\n{summary_text}"

    last_err = ""
    for attempt in range(1, retries + 2):
        try:
            resp = _client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _FILTER_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=0.1,
                max_tokens=150,
            )
            raw = resp.choices[0].message.content or ""
            # 解析 JSON
            raw_clean = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
            data = json.loads(raw_clean)
            idx_list = data.get("selected_idx", [])
            if isinstance(idx_list, list) and all(isinstance(x, int) for x in idx_list):
                return idx_list[:10]
            last_err = f"字段类型异常: {raw_clean[:80]}"
        except Exception as e:
            last_err = str(e)

        if attempt <= retries:
            time.sleep(1.0 * attempt)

    print(f"[agent2] LLM 筛选失败（{last_err}），降级返回全部")
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════════════════

def search_candidates(
    a_work: dict,
    b_work: dict,
    search_params: dict,   # agent1_to_search_params() 的输出
    city: str = "北京",
    query: str = "",
) -> dict:
    """
    Agent2 主入口。

    输入：
        a_work / b_work  : {"lng": float, "lat": float}
        search_params    : {"keyword": str, "min_rating": float, "weights": {...}}
        city             : 城市名（供高德公交 API 使用）
        query            : 原始用户 query（供 LLM 语义筛选）

    输出：
        {
            "pois":        list[dict],   # 筛选后完整 POI 列表，供绕路计算
            "center":      dict,         # 搜索中心坐标
            "radius":      int,          # 搜索半径（米）
            "total_found": int,          # 高德原始返回数量
            "total_selected": int,       # LLM 筛选后数量
            "llm_filtered": bool,        # 是否经过 LLM 筛选（False = 降级）
        }
    """
    keyword    = search_params.get("keyword",    "餐厅")
    min_rating = search_params.get("min_rating", 0.0)

    # ── 工具1：计算搜索中心 ──────────────────────────────────────────────────
    geo = get_search_center(a_work, b_work)
    center, radius = geo["center"], geo["radius"]

    # ── 工具2：搜索周边 POI ──────────────────────────────────────────────────
    full_pois = fetch_pois(center, keyword, radius, min_rating, city)
    total_found = len(full_pois)

    if not full_pois:
        return {
            "pois": [], "center": center, "radius": radius,
            "total_found": 0, "total_selected": 0, "llm_filtered": False,
        }

    # ── LLM 语义筛选 ────────────────────────────────────────────────────────
    summaries  = _build_summary(full_pois, center)
    idx_list   = _llm_filter(query or keyword, summaries)
    llm_filtered = idx_list is not None

    if llm_filtered:
        # 用 LLM 返回的 idx 从完整数据中取出，保持 LLM 排序
        selected = [full_pois[i] for i in idx_list if 0 <= i < len(full_pois)]
    else:
        selected = full_pois   # 降级：全部原样返回

    return {
        "pois":           selected,
        "center":         center,
        "radius":         radius,
        "total_found":    total_found,
        "total_selected": len(selected),
        "llm_filtered":   llm_filtered,
    }
