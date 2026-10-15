"""
core/geo_utils.py — 地理计算 + 高德 POI 搜索工具函数
被 app_v2.py 和 agents/agent2_searcher.py 共同引用，避免循环导入。
"""

import os
import math
import requests
from dotenv import load_dotenv

load_dotenv()

AMAP_KEY = os.getenv("AMAP_KEY")


def lnglat_to_str(loc: dict) -> str:
    """{'lng': 116.4, 'lat': 39.9} → '116.4,39.9'"""
    return f"{loc['lng']},{loc['lat']}"


def haversine_km(loc_a: dict, loc_b: dict) -> float:
    """计算两点间 Haversine 直线距离（公里）"""
    R = 6371
    lat1, lon1 = math.radians(loc_a["lat"]), math.radians(loc_a["lng"])
    lat2, lon2 = math.radians(loc_b["lat"]), math.radians(loc_b["lng"])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def find_midpoint(loc_a: dict, loc_b: dict) -> dict:
    """返回两点地理中心"""
    return {
        "lng": (loc_a["lng"] + loc_b["lng"]) / 2,
        "lat": (loc_a["lat"] + loc_b["lat"]) / 2,
    }


def dynamic_radius(loc_a: dict, loc_b: dict) -> int:
    """搜索半径：两点距离的 1/2，最大 5000m"""
    km = haversine_km(loc_a, loc_b)
    return min(int(km * 1000 / 2), 5000)


def search_pois_nearby(center: dict, keyword: str, radius: int,
                       min_rating: float = 0.0, city: str = "") -> list[dict]:
    """搜索中心点附近 POI，返回最多 20 个，过滤评分不达标的"""
    try:
        r = requests.get(
            "https://restapi.amap.com/v3/place/around",
            params={
                "key":        AMAP_KEY,
                "location":   lnglat_to_str(center),
                "keywords":   keyword or "餐厅",
                "radius":     radius,
                "sortrule":   "rating",
                "extensions": "all",
                "offset":     25,
                "page":       1,
                "city":       city,
            },
            timeout=8,
        ).json()

        pois = r.get("pois", [])
        results = []
        for p in pois:
            loc_str = p.get("location", "")
            if not loc_str or "," not in loc_str:
                continue
            lng, lat = loc_str.split(",")
            biz    = p.get("biz_ext") or {}
            rating = float(biz.get("rating") or 0) if isinstance(biz, dict) else 0.0
            if rating < min_rating:
                continue
            results.append({
                "id":       p.get("id", ""),
                "name":     p.get("name", ""),
                "address":  p.get("address", ""),
                "type":     p.get("type", ""),
                "rating":   rating,
                "cost":     biz.get("cost") if isinstance(biz, dict) else None,
                "location": {"lng": float(lng), "lat": float(lat)},
                "loc_str":  loc_str,
            })
            if len(results) >= 20:
                break
        return results
    except Exception:
        return []
