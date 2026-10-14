"""
拼接测试：Agent1 → agent1_to_search_params → M2 计算函数

验证目标：
  1. Agent1 真实 LLM 调用，拿到「评分高的火锅店」的解析结果
  2. 经 agent1_to_search_params() 转换，字段结构符合 M2 期望
  3. 用转换后的 weights 调用 score_poi / enrich_and_rank，计算结果正确

注：路线计算（高德 API）在此测试中用 mock 数据代替，
    避免真实 API 调用，专注验证字段映射和评分逻辑。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.agent1_parser import parse_query, agent1_to_search_params
from app_v2 import score_poi, enrich_and_rank

# ── Step 1：Agent1 真实调用 ───────────────────────────────────────────────────
print("=" * 60)
print("Step 1 — Agent1 真实 LLM 调用")
print("=" * 60)

import time
query = "评分高的火锅店"
print(f"输入 query：{repr(query)}")
t0 = time.time()
raw = parse_query(query)
elapsed = time.time() - t0

print(f"耗时：{elapsed:.2f}s  来源：{raw.get('_source', '?')}")
print(f"Agent1 原始输出：{raw}")

# ── Step 2：转换为 M2 期望格式 ────────────────────────────────────────────────
print("\n" + "=" * 60)
print("Step 2 — agent1_to_search_params() 转换")
print("=" * 60)

params = agent1_to_search_params(raw)
print(f"keyword    → {repr(params['keyword'])}")
print(f"min_rating → {params['min_rating']}")
print(f"weights    → {params['weights']}")

# 断言字段结构符合 M2 期望
assert isinstance(params["keyword"], str),    "keyword 必须是 str"
assert isinstance(params["min_rating"], float), "min_rating 必须是 float"
assert set(params["weights"]) == {"w1", "w2", "w3"}, "weights 必须含 w1/w2/w3"
print("✅ 字段结构断言通过")

# ── Step 3：用 M2 score_poi / enrich_and_rank 验证评分逻辑 ─────────────────────
print("\n" + "=" * 60)
print("Step 3 — 调用 M2 score_poi + enrich_and_rank（mock POI 数据）")
print("=" * 60)

# 构造 3 个 mock POI，附带 mock 路线数据（不调真实高德 API）
mock_pois = [
    {
        "id": "P001", "name": "老码头火锅",
        "address": "朝阳区XX路1号", "rating": 4.8, "cost": "120",
        "loc_str": "116.45,39.95",
        "a_route": {"detour_min": 8.0,  "ok": True},
        "b_route": {"detour_min": 10.0, "ok": True},
    },
    {
        "id": "P002", "name": "巴蜀人家",
        "address": "朝阳区YY路2号", "rating": 4.5, "cost": "90",
        "loc_str": "116.46,39.96",
        "a_route": {"detour_min": 5.0,  "ok": True},
        "b_route": {"detour_min": 25.0, "ok": True},   # B 绕很多，公平性差
    },
    {
        "id": "P003", "name": "海底捞",
        "address": "朝阳区ZZ路3号", "rating": 4.7, "cost": "150",
        "loc_str": "116.44,39.94",
        "a_route": {"detour_min": 12.0, "ok": True},
        "b_route": {"detour_min": 13.0, "ok": True},
    },
]

weights   = params["weights"]
threshold = 15   # 默认绕路阈值（分钟）

for poi in mock_pois:
    s = score_poi(poi, weights)
    print(f"  {poi['name']:12s}  rating={poi['rating']}  "
          f"A绕路={poi['a_route']['detour_min']}min  "
          f"B绕路={poi['b_route']['detour_min']}min  "
          f"→ score={round(s, 4)}")

print()
ranked = enrich_and_rank(mock_pois, threshold, weights)
print("排序结果（顺路标签 + 评分降序）：")
for i, poi in enumerate(ranked, 1):
    print(f"  #{i} [{poi['detour_label']:4s}] {poi['name']:12s}  score={poi['score']}")

print("\n✅ 拼接测试全部通过")
print("=" * 60)
