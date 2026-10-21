"""
下班顺路吃饭地点推荐 - 后端主文件
M2：四点输入 + 绕路时间计算 + 评分排序
M3：Multi-Agent 流水线（待填充）

兼容 Python 3.9
"""
from __future__ import annotations

import os
import uuid
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv
from openai import OpenAI
from agents.agent1_parser import parse_query, agent1_to_search_params
from agents.agent2_searcher import search_candidates
from agents.agent3_summary import generate_summary
from core.geo_utils import (lnglat_to_str, haversine_km, find_midpoint,
                             dynamic_radius, search_pois_nearby)

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

# ── 配置 ──────────────────────────────────────────────────────────────────────
AMAP_KEY           = os.getenv("AMAP_KEY")
AMAP_JS_KEY        = os.getenv("AMAP_JS_KEY")
AMAP_JS_SECURITY_KEY = os.getenv("AMAP_JS_SECURITY_KEY")
ZHIPU_API_KEY      = os.getenv("ZHIPU_API_KEY")
ZHIPU_BASE_URL     = os.getenv("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL          = "glm-4-flash"

llm_client = OpenAI(api_key=ZHIPU_API_KEY, base_url=ZHIPU_BASE_URL)

# Session 缓存（内存）
sessions: dict[str, dict] = {}

# 默认绕路权重（M3 由 Agent1 动态调整）
DEFAULT_WEIGHTS = {"w1": 1.0, "w2": 0.3, "w3": 0.2}
DEFAULT_MAX_DETOUR = 15   # 分钟
ROUTE_TIMEOUT = 6         # 秒


# ══════════════════════════════════════════════════════════════════════════════
# 高德路线 API
# ══════════════════════════════════════════════════════════════════════════════

def _call_driving(origin: str, dest: str) -> int:
    r = requests.get(
        "https://restapi.amap.com/v3/direction/driving",
        params={"key": AMAP_KEY, "origin": origin, "destination": dest, "extensions": "base"},
        timeout=ROUTE_TIMEOUT,
    ).json()
    paths = r.get("route", {}).get("paths", [])
    return int(paths[0]["duration"]) if paths else 99999


def _call_walking(origin: str, dest: str) -> int:
    r = requests.get(
        "https://restapi.amap.com/v3/direction/walking",
        params={"key": AMAP_KEY, "origin": origin, "destination": dest},
        timeout=ROUTE_TIMEOUT,
    ).json()
    paths = r.get("route", {}).get("paths", [])
    return int(paths[0]["duration"]) if paths else 99999


def _call_transit(origin: str, dest: str, city: str) -> int:
    r = requests.get(
        "https://restapi.amap.com/v3/direction/transit/integrated",
        params={"key": AMAP_KEY, "origin": origin, "destination": dest,
                "city": city, "cityd": city},
        timeout=ROUTE_TIMEOUT,
    ).json()
    transits = r.get("route", {}).get("transits", [])
    return int(transits[0]["duration"]) if transits else 99999


def _call_cycling(origin: str, dest: str) -> int:
    r = requests.get(
        "https://restapi.amap.com/v4/direction/bicycling",
        params={"key": AMAP_KEY, "origin": origin, "destination": dest},
        timeout=ROUTE_TIMEOUT,
    ).json()
    paths = r.get("data", {}).get("paths", [])
    return int(paths[0]["duration"]) if paths else 99999


def get_route_time(origin: str, dest: str, mode: str, city: str = "北京") -> int:
    """
    获取路线时间（秒），失败返回 99999。带一次重试，规避高德公交 API QPS 限制。
    mode: transit | driving | cycling | walking | auto
    注意：transit 模式要求 city 非空；city 为空时自动降级为 driving。
    """
    import time as _time

    # 公交规划必须有 city；city 为空时降级为 driving，避免必然失败
    effective_mode = mode
    if mode == "transit" and not city:
        effective_mode = "driving"
    elif mode == "auto" and not city:
        effective_mode = "driving"

    def _once():
        if effective_mode == "driving":
            return _call_driving(origin, dest)
        elif effective_mode == "walking":
            return _call_walking(origin, dest)
        elif effective_mode == "transit":
            return _call_transit(origin, dest, city)
        elif effective_mode == "cycling":
            return _call_cycling(origin, dest)
        elif effective_mode == "auto":
            t_drive   = _call_driving(origin, dest)
            t_transit = _call_transit(origin, dest, city)
            times = [t for t in [t_drive, t_transit] if t < 99999]
            return min(times) if times else 99999
        return 99999

    try:
        result = _once()
        if result == 99999:
            _time.sleep(0.5)   # 遇到限速，等半秒重试一次
            result = _once()
        return result
    except Exception:
        return 99999


def get_route_summary(origin: str, dest: str, mode: str, city: str = "北京") -> dict:
    """返回路线摘要 {duration_sec, duration_min, mode}"""
    t = get_route_time(origin, dest, mode, city)
    return {
        "duration_sec": t,
        "duration_min": round(t / 60, 1) if t < 99999 else None,
        "mode": mode,
        "ok": t < 99999,
    }


# ══════════════════════════════════════════════════════════════════════════════
# POI 搜索与绕路计算


# ══════════════════════════════════════════════════════════════════════════════
# 绕路时间计算核心
# ══════════════════════════════════════════════════════════════════════════════

def calc_detour_for_poi(
    poi: dict,
    a_work_str: str, a_home_str: str,
    b_work_str: str, b_home_str: str,
    baseline_a_sec: int, baseline_b_sec: int,
    prefer_a: str, prefer_b: str,
    city: str,
) -> dict:
    """
    对单个 POI 计算 A/B 绕路时间（顺序拉 4 条路线，避免嵌套线程池触发 QPS 限制）
    返回包含路线摘要和绕路时间的字典
    """
    poi_str = poi["loc_str"]

    a1 = get_route_time(a_work_str, poi_str,  prefer_a, city)
    a2 = get_route_time(poi_str,  a_home_str, prefer_a, city)
    b1 = get_route_time(b_work_str, poi_str,  prefer_b, city)
    b2 = get_route_time(poi_str,  b_home_str, prefer_b, city)

    a_via_sec = (a1 + a2) if (a1 < 99999 and a2 < 99999) else 99999
    b_via_sec = (b1 + b2) if (b1 < 99999 and b2 < 99999) else 99999

    a_detour_min = (
        round((a_via_sec - baseline_a_sec) / 60, 1)
        if a_via_sec < 99999 and baseline_a_sec < 99999
        else None
    )
    b_detour_min = (
        round((b_via_sec - baseline_b_sec) / 60, 1)
        if b_via_sec < 99999 and baseline_b_sec < 99999
        else None
    )

    a_route = {
        "leg1_sec": a1, "leg2_sec": a2,
        "total_sec": a_via_sec,
        "total_min": round(a_via_sec / 60, 1) if a_via_sec < 99999 else None,
        "detour_min": a_detour_min,
        "mode": prefer_a,
        "ok": a_via_sec < 99999,
    }
    b_route = {
        "leg1_sec": b1, "leg2_sec": b2,
        "total_sec": b_via_sec,
        "total_min": round(b_via_sec / 60, 1) if b_via_sec < 99999 else None,
        "detour_min": b_detour_min,
        "mode": prefer_b,
        "ok": b_via_sec < 99999,
    }

    return {**poi, "a_route": a_route, "b_route": b_route}


def label_detour(a_min: float | None, b_min: float | None, threshold: int) -> str:
    """返回顺路标签: both | one | none"""
    a_ok = a_min is not None and a_min <= threshold
    b_ok = b_min is not None and b_min <= threshold
    if a_ok and b_ok:
        return "both"
    if a_ok or b_ok:
        return "one"
    return "none"


def score_poi(poi: dict, weights: dict) -> float:
    """综合评分：越高越好"""
    w1, w2, w3 = weights["w1"], weights["w2"], weights["w3"]
    rating = poi.get("rating", 0) or 0
    a_d = poi["a_route"]["detour_min"]
    b_d = poi["b_route"]["detour_min"]

    if a_d is None or b_d is None:
        return -9999.0

    return w1 * rating - w2 * (a_d + b_d) - w3 * abs(a_d - b_d)


def enrich_and_rank(pois: list[dict], threshold: int, weights: dict) -> list[dict]:
    """
    给每个 POI 附上顺路标签和综合评分，按规则排序：
      1. both（双向顺路）> one（单向）> none（均不顺路）
      2. 同级内按 score 降序
    """
    order = {"both": 0, "one": 1, "none": 2}

    for poi in pois:
        a_d = poi["a_route"]["detour_min"]
        b_d = poi["b_route"]["detour_min"]
        poi["detour_label"] = label_detour(a_d, b_d, threshold)
        poi["score"] = round(score_poi(poi, weights), 4)

    pois.sort(key=lambda p: (order[p["detour_label"]], -p["score"]))
    return pois


# ══════════════════════════════════════════════════════════════════════════════
# 前端入口
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ── 配置接口 ──────────────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config():
    return jsonify({
        "amap_js_key":          AMAP_JS_KEY,
        "amap_js_security_key": AMAP_JS_SECURITY_KEY,
    })


# ── 地理编码 ──────────────────────────────────────────────────────────────────
@app.post("/api/geocode")
def geocode():
    data    = request.json or {}
    address = data.get("address", "")
    city    = data.get("city", "")
    if not address:
        return jsonify({"error": "address is required"}), 400
    resp = requests.get(
        "https://restapi.amap.com/v3/geocode/geo",
        params={"key": AMAP_KEY, "address": address, "city": city},
        timeout=5,
    )
    return jsonify(resp.json())


# ── 地点搜索下拉候选 ──────────────────────────────────────────────────────────
@app.post("/api/geocode-suggest")
def geocode_suggest():
    data     = request.json or {}
    keywords = data.get("keywords", "")
    city     = data.get("city", "")
    if not keywords:
        return jsonify({"tips": []})
    resp = requests.get(
        "https://restapi.amap.com/v3/assistant/inputtips",
        params={"key": AMAP_KEY, "keywords": keywords, "city": city, "datatype": "all"},
        timeout=5,
    )
    return jsonify(resp.json())


# ── 附近 POI ─────────────────────────────────────────────────────────────────
@app.post("/api/nearby-search")
def nearby_search():
    data     = request.json or {}
    location = data.get("location", "")
    keywords = data.get("keywords", "")
    radius   = data.get("radius", 1000)
    if not location:
        return jsonify({"error": "location is required"}), 400
    resp = requests.get(
        "https://restapi.amap.com/v3/place/around",
        params={
            "key": AMAP_KEY, "location": location,
            "keywords": keywords, "radius": radius,
            "sortrule": "rating", "extensions": "base", "offset": 20,
        },
        timeout=5,
    )
    return jsonify(resp.json())


# ══════════════════════════════════════════════════════════════════════════════
# 核心搜索接口（M2 完整实现，M3 接入 Agent 后替换权重提取部分）
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v2/search")
def search():
    """
    请求体示例：
    {
      "location_a_work": {"lng": 116.397, "lat": 39.908, "name": "A公司"},
      "location_a_home": {"lng": 116.320, "lat": 39.980, "name": "A的家"},
      "location_b_work": {"lng": 116.469, "lat": 39.995, "name": "B公司"},
      "location_b_home": {"lng": 116.550, "lat": 39.920, "name": "B的家"},
      "query":           "找一家安静的日料，不要太贵",
      "city":            "北京",
      "prefer_a":        "transit",
      "prefer_b":        "auto",
      "departure_time":  "20:00",
      "max_detour_minutes": 15
    }
    """
    body = request.json or {}

    # ── 1. 解析四个坐标 ────────────────────────────────────────────────────
    try:
        a_work = body["location_a_work"]
        a_home = body["location_a_home"]
        b_work = body["location_b_work"]
        b_home = body["location_b_home"]
    except KeyError as e:
        return jsonify({"error": f"missing field: {e}"}), 400

    query     = body.get("query", "餐厅")
    city      = body.get("city", "北京")
    prefer_a  = body.get("prefer_a", "transit")
    prefer_b  = body.get("prefer_b", "transit")
    threshold = int(body.get("max_detour_minutes", DEFAULT_MAX_DETOUR))

    # ── M3 Agent1：从 query 提取关键词 / 最低评分 / 排序权重 ──────────────
    agent1_raw    = parse_query(query)
    search_params = agent1_to_search_params(agent1_raw)
    keyword       = search_params["keyword"]
    min_rating    = search_params["min_rating"]
    weights       = search_params["weights"]

    a_work_str = lnglat_to_str(a_work)
    a_home_str = lnglat_to_str(a_home)
    b_work_str = lnglat_to_str(b_work)
    b_home_str = lnglat_to_str(b_home)

    # ── 2. 计算基准路线（A下班地→A家，B下班地→B家），并发 ────────────────
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_base_a = ex.submit(get_route_time, a_work_str, a_home_str, prefer_a, city)
        f_base_b = ex.submit(get_route_time, b_work_str, b_home_str, prefer_b, city)
    baseline_a_sec = f_base_a.result()
    baseline_b_sec = f_base_b.result()

    # 基准路线失败时提前报错，避免后续计算出负数绕路时间
    if baseline_a_sec >= 99999:
        return jsonify({
            "error": "A 的直达路线计算失败，请确认城市填写正确，或切换出行方式（公交需要填城市）"
        }), 400
    if baseline_b_sec >= 99999:
        return jsonify({
            "error": "B 的直达路线计算失败，请确认城市填写正确，或切换出行方式（公交需要填城市）"
        }), 400
    # ── 3. M3 Agent2：搜索中心 + 高德 POI + LLM 语义筛选 ────────────────
    agent2_result = search_candidates(a_work, b_work, search_params, city=city, query=query)
    pois   = agent2_result["pois"]
    center = agent2_result["center"]
    radius = agent2_result["radius"]

    if not pois:
        return jsonify({"error": "未找到符合条件的餐厅，请调整搜索词或扩大范围"}), 404

    # ── 4. 并发计算每个 POI 的绕路时间（max_workers=2，规避高德 QPS 限制）───
    enriched = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(
                calc_detour_for_poi,
                poi,
                a_work_str, a_home_str,
                b_work_str, b_home_str,
                baseline_a_sec, baseline_b_sec,
                prefer_a, prefer_b, city,
            ): poi
            for poi in pois
        }
        for fut in as_completed(futures):
            try:
                enriched.append(fut.result())
            except Exception:
                pass

    # ── 5. 评分 & 排序 ────────────────────────────────────────────────────
    ranked = enrich_and_rank(enriched, threshold, weights)

    # ── 6. Fallback：若全部均不顺路，补充一个"中间点推荐"分区 ──────────
    fallback_results = []
    if all(p["detour_label"] == "none" for p in ranked):
        fb_pois = search_pois_nearby(center, search_params.get("keyword", query), radius, city=city)
        for p in fb_pois:
            p["detour_label"] = "fallback"
            p["score"] = p.get("rating", 0)
            p["a_route"] = {}
            p["b_route"] = {}
        fallback_results = sorted(fb_pois, key=lambda p: -p["score"])[:10]

    # ── M3 Agent3：生成自然语言推荐语 ────────────────────────────────────
    summary = generate_summary(ranked or fallback_results, query=query)

    # ── 7. 存 Session ────────────────────────────────────────────────────
    session_id = str(uuid.uuid4())
    sessions[session_id] = {
        "created_at":     time.time(),
        "request":        body,
        "center":         center,
        "radius":         radius,
        "baseline_a_sec": baseline_a_sec,
        "baseline_b_sec": baseline_b_sec,
        "pois":           pois,
        "ranked":         ranked,
        "fallback":       fallback_results,
        "weights":        weights,
        "threshold":      threshold,
    }

    return jsonify({
        "session_id":       session_id,
        "center":           center,
        "radius":           radius,
        "baseline_a_min":   round(baseline_a_sec / 60, 1) if baseline_a_sec < 99999 else None,
        "baseline_b_min":   round(baseline_b_sec / 60, 1) if baseline_b_sec < 99999 else None,
        "results":          ranked,
        "fallback_results": fallback_results,
        "total":            len(ranked),
        "summary":          summary,
    })


# ══════════════════════════════════════════════════════════════════════════════
# 路线重算接口（切换出行方式，不重新搜索 POI）
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/v2/routes")
def recalculate_routes():
    """
    请求体示例：
    {
      "session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "prefer_a": "cycling",
      "prefer_b": "driving",
      "departure_time": null
    }
    """
    body       = request.json or {}
    session_id = body.get("session_id")
    if not session_id or session_id not in sessions:
        return jsonify({"error": "session not found"}), 404

    sess     = sessions[session_id]
    orig_req = sess["request"]
    pois     = sess["pois"]

    prefer_a  = body.get("prefer_a", orig_req.get("prefer_a", "transit"))
    prefer_b  = body.get("prefer_b", orig_req.get("prefer_b", "transit"))
    city      = orig_req.get("city", "北京")
    threshold = sess["threshold"]
    weights   = sess["weights"]

    a_work_str = lnglat_to_str(orig_req["location_a_work"])
    a_home_str = lnglat_to_str(orig_req["location_a_home"])
    b_work_str = lnglat_to_str(orig_req["location_b_work"])
    b_home_str = lnglat_to_str(orig_req["location_b_home"])

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_base_a = ex.submit(get_route_time, a_work_str, a_home_str, prefer_a, city)
        f_base_b = ex.submit(get_route_time, b_work_str, b_home_str, prefer_b, city)
    baseline_a_sec = f_base_a.result()
    baseline_b_sec = f_base_b.result()

    enriched = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futures = {
            ex.submit(
                calc_detour_for_poi,
                poi,
                a_work_str, a_home_str,
                b_work_str, b_home_str,
                baseline_a_sec, baseline_b_sec,
                prefer_a, prefer_b, city,
            ): poi
            for poi in pois
        }
        for fut in as_completed(futures):
            try:
                enriched.append(fut.result())
            except Exception:
                pass

    ranked = enrich_and_rank(enriched, threshold, weights)

    sessions[session_id]["ranked"]         = ranked
    sessions[session_id]["baseline_a_sec"] = baseline_a_sec
    sessions[session_id]["baseline_b_sec"] = baseline_b_sec

    return jsonify({
        "session_id":     session_id,
        "baseline_a_min": round(baseline_a_sec / 60, 1) if baseline_a_sec < 99999 else None,
        "baseline_b_min": round(baseline_b_sec / 60, 1) if baseline_b_sec < 99999 else None,
        "results":        ranked,
        "total":          len(ranked),
    })


# ── Session 调试查看 ──────────────────────────────────────────────────────────
@app.get("/api/v2/session/<session_id>")
def get_session(session_id: str):
    sess = sessions.get(session_id)
    if not sess:
        return jsonify({"error": "session not found"}), 404
    return jsonify({
        "session_id":     session_id,
        "created_at":     sess["created_at"],
        "center":         sess["center"],
        "radius":         sess["radius"],
        "total_results":  len(sess.get("ranked", [])),
        "weights":        sess["weights"],
        "threshold":      sess["threshold"],
        "baseline_a_min": round(sess["baseline_a_sec"] / 60, 1),
        "baseline_b_min": round(sess["baseline_b_sec"] / 60, 1),
    })


# ── 启动 ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("下班顺路吃饭推荐 - M2 版本")
    print(f"AMAP_KEY:    {'✅' if AMAP_KEY else '❌ 未配置'}")
    print(f"AMAP_JS_KEY: {'✅' if AMAP_JS_KEY else '❌ 未配置'}")
    print(f"ZHIPU_KEY:   {'✅' if ZHIPU_API_KEY else '❌ 未配置'}")
    print("=" * 50)
    print("访问 http://localhost:5000")
    app.run(debug=True, port=5000)
