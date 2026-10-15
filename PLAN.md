# PLAN.md — 下班顺路吃什么

> 本文档是项目的单一事实来源（Single Source of Truth）。  
> 任何新会话读完此文档即可接手工作，无需翻阅聊天记录。

---

## 1. 项目概述

**下班顺路吃什么** 是一个面向双人约饭场景的智能推荐工具。  
目标用户：同城打工人，下班时间晚、吃完饭还需各自回家。  
核心场景：输入 A/B 两人的下班地点和回家地点，AI 优先推荐双方回家路上都顺路的就餐地点，并规划各自路线；若无顺路结果，自动 fallback 到中间点推荐。

---

## 2. 技术栈与架构

### 技术选型

| 类别 | 选型 | 备注 |
|---|---|---|
| 后端语言 | Python 3.10+ | 使用 `str \| None` 类型注解语法，不可低于 3.10 |
| Web 框架 | Flask 3.0 + flask-cors | |
| LLM | 智谱 GLM-4-Flash | 免费，兼容 OpenAI SDK，通过 `base_url` 切换 |
| 地图服务 | 高德地图 Web 服务 API + JS API 2.0 | Web 服务 Key 用于后端，JS Key 用于前端底图 |
| 前端 | 纯 HTML/CSS/JS，单文件，无构建工具 | |
| UI Kit | KABOOM! UI（`static/css/comic-ui.css`，MIT 协议） | 漫画/波普风格，直接引用原版 CSS，不二次改动 |
| 依赖管理 | pip / uv | `requirements.txt` 锁定版本 |
| 部署 | 本地开发，`bash start.sh` 或 `python app_v2.py` | |

### 整体架构

```
用户浏览器（static/index.html）
      │  HTTP / Fetch API
      ▼
Flask 后端（app_v2.py，端口 5000）
      │
      ├── GET  /api/config           → 向前端暴露 AMAP_JS_KEY / AMAP_JS_SECURITY_KEY
      ├── POST /api/geocode          → 高德 geocode（地址→坐标）
      ├── POST /api/geocode-suggest  → 高德 inputtips（地址联想下拉）
      ├── POST /api/nearby-search    → 高德 place/around（附近 POI）
      │
      └── POST /api/v2/search        → 核心搜索流水线（M2 完整实现）
            │
            ├── [M2] Python 路线计算：
            │     ① 并发计算基准路线（A下班→A家，B下班→B家）
            │     ② 搜索 POI（高德 place/around）
            │     ③ 逐 POI 计算 4 条腿路线，得出绕路时间
            │     ④ 评分 + 排序 + fallback
            │     ⑤ 写入 session 缓存
            │
            ├── [M3 ✅] Agent1（LLM）：从 query 提取 keyword / min_rating / weights
            ├── [M3 ✅] Agent2（LLM + 工具）：搜索 POI + 语义筛选，不重排序
            └── [M3 ✅] Agent3（LLM）：生成 2-4 句推荐语，不计算、不重排序
      │
      └── POST /api/v2/routes        → 路线重算（复用 session 缓存，不重新搜 POI）
          GET  /api/v2/session/<id>  → 调试用，返回 session 摘要

高德 JS API 2.0（前端直接加载，暗色主题底图）
```

### 核心算法逻辑

```
A 绕路时间 = 行程(A下班地 → 餐厅 → A家) - 行程(A下班地 → A家)
B 绕路时间 = 行程(B下班地 → 餐厅 → B家) - 行程(B下班地 → B家)

顺路标签（优先级由高到低）：
  ✅ both  双向顺路：A绕路 ≤ threshold 且 B绕路 ≤ threshold
  ⚠️ one   单向顺路：仅一方 ≤ threshold
  ❌ none  均不顺路：两方均超出 → 触发 fallback，降级为中点算法推荐

综合评分（同标签分组内排序）：
  score = w₁ × poi_rating
        - w₂ × (A_detour + B_detour)
        - w₃ × |A_detour - B_detour|

  w₁/w₂/w₃ 是独立系数，**不做归一化**（见第 5 节关键决策）。
  M2 阶段：w₁=1.0，w₂=0.3，w₃=0.2（硬编码默认值）
  M3 阶段：由 Agent1 从用户 query 中动态提取
```

---

## 3. API 与密钥清单

所有密钥通过 `.env` 注入，**严禁将实际 Key 值写入任何代码或文档**。  
参考 `.env.example` 配置，向高德控制台申请时注意类型。

| API | 用途 | .env 变量名 | 调用方 |
|---|---|---|---|
| 智谱 GLM-4-Flash | LLM（M3 Agent1/2/3） | `ZHIPU_API_KEY` | 后端 |
| 智谱 API 端点 | LLM base_url | `ZHIPU_BASE_URL` | 后端（值为 `https://open.bigmodel.cn/api/paas/v4`） |
| 高德 Web 服务 | 路线规划、POI 搜索、地理编码（后端调用） | `AMAP_KEY` | 后端 |
| 高德 JS API | 前端地图底图渲染 | `AMAP_JS_KEY` | 前端（经 `/api/config` 下发） |
| 高德 JS API 安全密钥 | JS API 2.0 要求的配套安全码 | `AMAP_JS_SECURITY_KEY` | 前端（经 `/api/config` 下发） |

**申请说明：**
- 智谱：https://open.bigmodel.cn — 免费版 GLM-4-Flash 无需付费
- 高德：https://console.amap.com
  - `AMAP_KEY`：创建「Web 服务」类型应用
  - `AMAP_JS_KEY` + `AMAP_JS_SECURITY_KEY`：创建「Web 端（JS API）」类型应用，需在控制台将 `localhost` / `127.0.0.1` 加入域名白名单

---

## 4. 已完成进度

### 文件结构（当前实际状态）

```
/Users/sunxiaoran/Desktop/grace/claude/   ← 项目根目录
├── app_v2.py            # 后端主文件（M2+M3 完整实现）
├── requirements.txt     # 依赖锁定
├── start.sh             # 一键启动
├── .env                 # API Keys（不提交 git）
├── .env.example         # Key 模板
├── PLAN.md              # 本文档
├── agents/              # M3 Agent 模块
│   ├── agent1_parser.py     # Agent1：query 解析
│   ├── agent2_searcher.py   # Agent2：POI 搜索 + LLM 筛选
│   └── agent3_summary.py    # Agent3：推荐语生成
├── core/                # 共享工具层
│   ├── __init__.py
│   └── geo_utils.py     # 地理工具函数（app_v2.py 和 agent2 共用）
├── docs/                # 测试记录
│   └── e2e_test_m3.json # M3 端到端测试请求/响应存档
├── static/
│   ├── index.html       # 前端单页（M1 完整 KABOOM! UI 骨架）
│   └── css/
│       └── comic-ui.css # KABOOM! UI 原版 CSS（MIT，直接引用）
└── meetmid-v2/          # M2 原始开发目录（保留备用，不再修改）
```

> **M2+M3 已完成：** 根目录 `app_v2.py` 是当前生产文件，包含完整 M2 路线计算逻辑和 M3 三 Agent 流水线。`meetmid-v2/` 目录保留为历史备份，不再修改。

---

### M1：项目骨架 ✅

**根目录 `app_v2.py` 已实现的接口：**

| 方法 | 路径 | 状态 | 说明 |
|---|---|---|---|
| GET | `/` | ✅ | 返回 `static/index.html` |
| GET | `/api/config` | ✅ | 返回 `{ amap_js_key }` 供前端使用 |
| POST | `/api/geocode` | ✅ | 地址→坐标，代理高德 geocode |
| POST | `/api/geocode-suggest` | ✅ | 地址联想，代理高德 inputtips |
| POST | `/api/nearby-search` | ✅ | 附近 POI，代理高德 place/around |
| POST | `/api/v2/search` | ❌ 501 | M1 骨架，核心逻辑在 meetmid-v2/ |
| POST | `/api/v2/routes` | ❌ 501 | M1 骨架，核心逻辑在 meetmid-v2/ |
| GET | `/api/v2/session/<id>` | ✅ | session 内容查看（调试用） |

**`static/index.html` 已实现的前端功能（M1 完整骨架）：**

- KABOOM! 漫画风格整体布局（黄底、粗墨线边框、CMYK 错位阴影）
- 双栏布局：左侧输入区 + 结果区 / 右侧地图，拖拽分界线可调节宽度
- A/B 两人各自的「下班地点」+「回家地点」输入卡片，含地址搜索联想下拉
- 出行方式选择（⚡最快 / 🚇公交地铁 / 🚲骑行 / 🚗驾车 / 🚶步行），A/B 独立
- 自然语言需求输入框 + 城市 + 出发时间
- 搜索按钮，调 `/api/v2/search`
- 加载动画（手写 CSS 对齐 KABOOM! 风格）
- 结果卡片区占位（待 M4 填充）
- 路线重算面板占位（待 M4 填充）
- 高德地图初始化（暗色主题，`/api/config` 获取 Key，漫画粗边框包裹地图容器）

---

### M2：后端核心路线计算 ✅

**实现文件：`meetmid-v2/app_v2.py`**

#### 核心函数清单

| 函数 | 职责 |
|---|---|
| `lnglat_to_str(loc)` | `{"lng":x,"lat":y}` → `"x,y"` 字符串，供高德 API 使用 |
| `haversine_km(loc_a, loc_b)` | 计算两点 Haversine 直线距离（km） |
| `find_midpoint(loc_a, loc_b)` | 返回两点地理中心 `{"lng":..., "lat":...}` |
| `dynamic_radius(loc_a, loc_b)` | 搜索半径 = 两点距离/2，上限 5000m |
| `_call_driving/walking/transit/cycling(origin, dest)` | 各出行方式的高德路线 API 原始调用，返回秒数，失败返回 99999 |
| `get_route_time(origin, dest, mode, city)` | 统一路线时间入口，含失败重试（sleep 0.5s 后重试一次）；`auto` 模式取 driving/transit 最小值 |
| `search_pois_nearby(center, keyword, radius, min_rating, city)` | 高德 place/around 搜索，返回最多 20 个 POI，含 `loc_str` 字段 |
| `calc_detour_for_poi(poi, a_work_str, a_home_str, b_work_str, b_home_str, baseline_a_sec, baseline_b_sec, prefer_a, prefer_b, city)` | 顺序调 4 条腿路线，计算 A/B 绕路时间，返回附带 `a_route`/`b_route` 字段的 POI dict |
| `label_detour(a_min, b_min, threshold)` | 返回顺路标签 `"both"` / `"one"` / `"none"` |
| `score_poi(poi, weights)` | 计算综合评分：`w1*rating - w2*(a+b) - w3*|a-b|` |
| `enrich_and_rank(pois, threshold, weights)` | 批量附加标签+评分，按 both>one>none 分组，组内按 score 降序排列 |

#### `/api/v2/search` 完整请求/响应结构

**请求体：**
```json
{
  "location_a_work": { "lng": 116.397, "lat": 39.908, "name": "A公司" },
  "location_a_home": { "lng": 116.320, "lat": 39.980, "name": "A的家" },
  "location_b_work": { "lng": 116.469, "lat": 39.995, "name": "B公司" },
  "location_b_home": { "lng": 116.550, "lat": 39.920, "name": "B的家" },
  "query":               "找一家安静的日料，不要太贵",
  "city":                "北京",
  "prefer_a":            "transit",
  "prefer_b":            "auto",
  "departure_time":      "20:00",
  "max_detour_minutes":  15
}
```

**响应体：**
```json
{
  "session_id":       "uuid-string",
  "center":           { "lng": 116.433, "lat": 39.952 },
  "radius":           5000,
  "baseline_a_min":   61.3,
  "baseline_b_min":   77.7,
  "total":            20,
  "results": [
    {
      "id":             "B0XXXXXX",
      "name":           "然锅料理",
      "address":        "朝阳区XX路XX号",
      "type":           "餐饮服务;...",
      "rating":         4.8,
      "cost":           "120.00",
      "location":       { "lng": 116.456, "lat": 39.948 },
      "loc_str":        "116.456,39.948",
      "detour_label":   "both" | "one" | "none",
      "score":          3.54,
      "a_route": {
        "leg1_sec":     3692,
        "leg2_sec":     3314,
        "total_sec":    7006,
        "total_min":    116.8,
        "detour_min":   55.5,
        "mode":         "transit",
        "ok":           true
      },
      "b_route":        "（同 a_route 结构）"
    }
  ],
  "fallback_results": [],
  "summary": "海底捞评分4.8，人均120元，A绕路57分钟，B绕路48分钟..."
}
```

> `summary` 字段为 M3 新增，由 Agent3 生成自然语言推荐语。M4 前端直接展示此字段内容。

#### `/api/v2/routes` 请求/响应结构

**请求体：**
```json
{
  "session_id":  "uuid-string",
  "prefer_a":    "cycling",
  "prefer_b":    "driving"
}
```

**响应体：** 同 `/api/v2/search`，只含 `session_id` / `baseline_*_min` / `results` / `total`。

#### 执行流程（`/api/v2/search`）

```
1. 解析请求，提取 4 个坐标 + prefer_a/b + threshold
2. ThreadPoolExecutor(max_workers=2) 并发计算基准路线（A→家，B→家）
3. 计算搜索中心（两工作地中点）和动态半径
4. 调高德 place/around 搜索 POI（最多 20 个）
5. ThreadPoolExecutor(max_workers=2) 并发处理各 POI：
     └── calc_detour_for_poi（内部顺序调 4 条腿，避免 QPS 限流）
6. 评分排序（enrich_and_rank），打顺路标签
7. 若全部 none → 触发 fallback：重新搜 POI，按 rating 降序，取前 10
8. 写入 sessions 缓存（key=session_id，含原始 pois 供路线重算复用）
9. 返回 JSON
```

#### `sessions` 缓存结构

```python
sessions[session_id] = {
    "created_at":     float,         # time.time()
    "request":        dict,          # 原始请求体（路线重算需要）
    "center":         dict,          # 搜索中心坐标
    "radius":         int,           # 搜索半径（米）
    "baseline_a_sec": int,           # A 基准路线（秒）
    "baseline_b_sec": int,
    "pois":           list[dict],    # 原始 POI 列表（路线重算时无需重新搜索）
    "ranked":         list[dict],    # 排序后的完整结果
    "fallback":       list[dict],    # fallback 推荐（可能为空）
    "weights":        dict,          # {"w1":1.0,"w2":0.3,"w3":0.2}
    "threshold":      int,           # 绕路时间阈值（分钟）
}
```

---

## 5. 里程碑规划

| 阶段 | 内容 | 状态 |
|---|---|---|
| M1 | 项目初始化：Flask 骨架、前端 KABOOM! UI 骨架、KABOOM! UI CSS 接入 | ✅ 完成 |
| M2 | 后端核心：四点输入 + 绕路时间计算 + 路线重算逻辑 | ✅ 完成 |
| M3 | Multi-Agent 流水线：Agent1/2/3 接入智谱 GLM-4-Flash | ✅ 完成 |
| M4 | 前端结果展示：顺路标签、结果卡片、地图标记、中间点 fallback 区 | ⬜ 未开始 |
| M5 | UI 精修：卡片选中高亮、附近搜索面板、加载动效 | ⬜ 未开始 |
| M6 | 联调测试、边界情况处理、README 补全 | ⬜ 未开始 |

### 关键决策记录

**M1**
- **KABOOM! UI 直接使用原版 CSS**：不二次改动 Kit 本身，缺失组件（地址下拉候选、加载动画）手写 CSS 补全，保持风格对齐。地图区用高德暗色主题 + 漫画粗边框包裹解决风格冲突。

**M2**
- **M2 优先于 M3**：先用 Python 直调高德 API 验证绕路计算逻辑，再接 LLM，避免 Agent 不确定性干扰核心算法调试。
- **路线调用改为顺序执行**（不用嵌套 ThreadPoolExecutor）：高德公交方向 API QPS 限制严格，嵌套线程池触发限速后全部返回 99999；改为每个 POI 内顺序调 4 条腿，外层 max_workers=2，并行量可控。
- **失败重试机制**：`get_route_time` 遇到 99999 时 sleep 0.5s 重试一次，解决偶发性空响应。
- **`auto` 模式 = min(driving, transit)**：不跑全部 4 种出行方式，节省 API 调用量；步行/骑行距离过长时结果无意义。
- **M2 阶段权重硬编码**：w1=1.0，w2=0.3，w3=0.2；M3 Agent1 接入后改为 LLM 从 query 动态提取（如用户强调"公平"则 w3 加大，强调"快"则 w2 加大）。
- **LLM 从 DeepSeek 换为智谱 GLM-4-Flash**：免费额度满足开发需求，且兼容 OpenAI SDK，只需改 `base_url` 和 `model` 参数，迁移成本为零。

**M3（已完成）**
- **Agent2 工具返回压缩摘要，完整数据存 Python 侧**：POI 原始数据体积大，全量传给 LLM 会导致 token 爆炸，只给 LLM 看「名称/地址/评分/距离」摘要即可完成规划任务。
- **"均不顺路"时 fallback 到中点算法**：不让用户空手而归，在界面上单独展示为"中间点推荐"分区，明确告知无顺路结果。
- **w1/w2/w3 保持独立系数，不做归一化**：三个权重量纲不同（w1 对应 0~5 的评分，w2/w3 对应分钟数），强制归一化到 [0,1] 会破坏原有量纲关系，且 Agent1 的 System Prompt 已按独立取值范围（w1: 0.5~2.0，w2: 0.1~0.8，w3: 0.1~0.5）指导 LLM，独立调节语义更直观，测试中效果符合预期。
- **LLM 选用智谱 GLM-4-Flash，DeepSeek 作备选**：GLM-4-Flash 免费档无需付费、兼容 OpenAI SDK（只改 `base_url` 和 `model`），满足开发阶段需求。若需切换，备选为 `deepseek-chat`（`base_url=https://api.deepseek.com/v1`），迁移成本极低。

---

## 6. 已知问题与待办

### M3 完成前必须完成（✅ 已全部解决）

1. ✅ **M2 代码已合并到根目录**（M3 会话已完成）  
   `meetmid-v2/app_v2.py` → 根目录 `app_v2.py`，`meetmid-v2/` 目录保留备用。

2. ✅ **根目录 `.env.example` 已更新**（M3 会话已完成）  
   移除 `DEEPSEEK_API_KEY`，补入 `ZHIPU_API_KEY`、`ZHIPU_BASE_URL`、`AMAP_JS_SECURITY_KEY`。

3. ✅ **`/api/config` 已返回 `amap_js_security_key`**（合并后自动修复）

4. ✅ **`app_v2.py` 重复定义已清理**（refactor commit `6315e63`）  
   `lnglat_to_str` / `haversine_km` / `find_midpoint` / `dynamic_radius` / `search_pois_nearby` 从 `app_v2.py` 删除，统一由 `core/geo_utils.py` 提供，`import math` 同步移除。

### M4 开始前产品调优点（待评估）

5. ⬜ **搜索半径 5000m 是否偏大**：当前 `dynamic_radius()` 上限固定为 5000m，对于 A/B 工作地距离较近（< 2km）的场景，半径可能命中过多无关 POI，影响 LLM 筛选质量。可考虑增加下限（如最小 500m）或根据城市密度动态调整。

6. ⬜ **Agent2 高德 POI 上限 20 个**：`place/around` 每次最多拉 20 条，LLM 筛选后最多保留 10 个。若目标区域同类型餐厅密集（如大量火锅店），20 个可能不够覆盖高分候选。可增加 page 翻页逻辑，或提高 `offset` 参数。

7. ⬜ **POI 搜索为空时的用户反馈**：当前 `fetch_pois` 返回空列表时，接口最终返回 `{total: 0, results: [], fallback_results: []}`，前端需对此情况做友好提示（"未找到相关餐厅，请尝试修改关键词或扩大范围"），该逻辑待 M4 实现。

---

## 7. M3 Agent 详细设计

### Agent2 — 搜索 Agent

**文件：** `agents/agent2_searcher.py`  
**模型：** `glm-4-flash`（语义筛选环节）  
**职责：** 给定 A/B 双方工作地坐标 + Agent1 的搜索参数，搜索候选餐厅 POI，经 LLM 语义筛选后返回供绕路计算使用的候选列表。

#### 工具清单

| 工具函数 | 调用的底层函数 | 高德 API 调用 | 说明 |
|---|---|---|---|
| `get_search_center(a_work, b_work)` | `find_midpoint()` + `dynamic_radius()` | 无 | 计算搜索中心坐标和动态半径（最大 5000m） |
| `fetch_pois(center, keyword, radius, min_rating, city)` | `search_pois_nearby()` | `place/around` | 返回最多 20 个完整 POI dict，完整数据只存 Python 侧 |

#### 主入口

```python
search_candidates(a_work, b_work, search_params, city, query) -> dict
```

**输入：**

| 参数 | 类型 | 来源 |
|---|---|---|
| `a_work` / `b_work` | `{"lng": float, "lat": float}` | 用户输入的 A/B 工作地坐标 |
| `search_params` | `{"keyword": str, "min_rating": float, "weights": {...}}` | `agent1_to_search_params()` 输出 |
| `city` | str | 用户输入的城市 |
| `query` | str | 原始用户 query（供 LLM 语义筛选） |

**输出：**

```json
{
  "pois":           [...],   // 筛选后完整 POI 列表，供绕路计算，最多 10 个
  "center":         {...},   // 搜索中心坐标
  "radius":         5000,    // 搜索半径（米）
  "total_found":    20,      // 高德原始返回数量
  "total_selected": 10,      // LLM 筛选后数量
  "llm_filtered":   true     // false = LLM 失败，降级返回全部
}
```

#### 数据分层纪律（核心设计）

- **完整 POI 数据**（坐标、营业时间、loc_str 等）：只存 Python 侧 `full_pois` 变量，不传给 LLM
- **LLM 压缩摘要**（token 最小化）：每条只含 `{idx, name, address, rating, cost, dist_to_center_m}`
- LLM 返回 `selected_idx` 整数列表，Python 用它从 `full_pois` 中取出完整数据

#### LLM 筛选降级策略

GLM-4-Flash 返回格式错误或超时时，最多重试 2 次（退避 1s/2s），全部失败后直接返回高德原始全部 POI（最多 20 个），`llm_filtered=False`。

#### Agent1 → Agent2 接力测试结果

query: `"评分高的火锅店"`，A（国贸）↔ B（中关村）

- Agent1 耗时：~2.5s，提取 `keyword="火锅"，min_rating=4.5`
- Agent2 耗时：~2.8s，高德搜到 20 个，LLM 筛选后保留 10 个（全部 ≥4.7 分）
- 总耗时：~5.25s
- 淘汰分析：LLM 保留了所有 4.8 分（7 家）+ 前 3 家 4.7 分，淘汰的 10 家均为评分相对靠后或距中心更远的 4.7 分店铺（含人均 444 元、402 元的高价异常值）

**文件：** `agents/agent1_parser.py`  
**模型：** `glm-4-flash`（智谱，免费版）  
**职责：** 将用户自然语言 query 解析为结构化搜索参数，供 M2 的 POI 搜索和评分排序函数消费。

#### 输入

| 参数 | 类型 | 说明 |
|---|---|---|
| `query` | str | 用户输入的自然语言，如"找评分高的火锅，不要太贵" |

#### 输出（`parse_query()` 返回）

| 字段 | 类型 | 说明 |
|---|---|---|
| `keyword` | str | 高德 POI 搜索关键词（如"火锅"、"日料"） |
| `min_rating` | float | 最低评分过滤阈值，0.0~5.0 |
| `w1` | float | 评分权重（0.5~2.0），强调"评分高"时取 1.5 |
| `w2` | float | 总绕路时间惩罚权重（0.1~0.8），强调"顺路优先"时取 0.6 |
| `w3` | float | 双方公平性惩罚权重（0.1~0.5），强调"两边差不多/均等"时取 0.4 |
| `_source` | str | `"llm"` 或 `"fallback"`，标记是否触发了降级 |

#### 降级兜底策略

LLM 调用失败或返回格式不合法时，最多重试 2 次（退避 1s / 2s），全部失败后使用默认值：`keyword=query原文`，`min_rating=0.0`，`w1=1.0`，`w2=0.3`，`w3=0.2`。

#### Agent1 输出 → M2 消费格式的字段映射

Agent1 返回扁平结构（`w1/w2/w3` 为顶层字段），M2 的 `enrich_and_rank()` 和 `score_poi()` 期望嵌套 `weights` dict。转换通过 `agent1_to_search_params()` 完成：

```python
# agents/agent1_parser.py
def agent1_to_search_params(agent1_result: dict) -> dict:
    return {
        "keyword":    agent1_result["keyword"],     # → search_pois_nearby(keyword=...)
        "min_rating": agent1_result["min_rating"],  # → search_pois_nearby(min_rating=...)
        "weights": {                                # → enrich_and_rank(weights=...)
            "w1": agent1_result["w1"],
            "w2": agent1_result["w2"],
            "w3": agent1_result["w3"],
        },
    }
```

| Agent1 字段 | M2 消费方 | 传入参数名 | 状态 |
|---|---|---|---|
| `keyword` | `search_pois_nearby()` | `keyword` | ✅ 直接传入 |
| `min_rating` | `search_pois_nearby()` | `min_rating` | ✅ 直接传入 |
| `w1/w2/w3` | `enrich_and_rank()` / `score_poi()` | `weights["w1/w2/w3"]` | ⚠️ 需封装为 dict，由 `agent1_to_search_params()` 完成 |

#### 串联位置（`app_v2.py` 的 `/api/v2/search`）

M3 接入后，以下两行硬编码将被替换：

```python
# 替换前（M2 硬编码）
keyword = query
weights = DEFAULT_WEIGHTS.copy()

# 替换后（M3 Agent1 驱动）
agent1_raw    = parse_query(query)
search_params = agent1_to_search_params(agent1_raw)
keyword       = search_params["keyword"]
min_rating    = search_params["min_rating"]
weights       = search_params["weights"]
```

---

### Agent3 — 总结 Agent（纯生成，无工具）

**文件：** `agents/agent3_summary.py`  
**模型：** `glm-4-flash`  
**职责：** 根据排序后候选列表 + 原始 query，生成 2~4 句自然语言推荐语。不做任何计算或重排序。

#### 输入（`generate_summary()` 参数）

| 参数 | 类型 | 说明 |
|---|---|---|
| `ranked_pois` | `list[dict]` | `enrich_and_rank()` 输出，每项含 `name/rating/cost/a_route/b_route/detour_label/score` |
| `query` | str | 原始用户 query |

**传给 LLM 的压缩摘要格式（Top 5）：**

```
#N name | 评分:X | 人均:Y元 | A绕路:Zmin | B绕路:Wmin | 顺路:标签文字
```

完整坐标等不传给 LLM。

#### 输出

纯字符串，2~4 句推荐语。要求：引用真实数字，标记为「仅一方顺路」的候选如实说明，不用营销腔。

#### 降级策略

LLM 失败或返回过短（< 20 字）时，最多重试 2 次（退避 1s/2s），全部失败使用模板兜底：`"根据「{query}」共找到 N 个候选，已按评分和绕路时间综合排序，「{name}」综合表现最佳。"`

---

### 共享工具层

**文件：** `core/geo_utils.py`  
**职责：** 存放 `app_v2.py` 和 `agents/agent2_searcher.py` 共同需要的地理工具函数，避免循环导入。  
**清理状态：** `app_v2.py` 中原有的重复定义已于 refactor commit `6315e63` 删除，两个文件均从此模块导入。

| 函数 | 说明 |
|---|---|
| `lnglat_to_str(loc)` | dict → `"lng,lat"` 字符串 |
| `haversine_km(a, b)` | 两点直线距离（公里） |
| `find_midpoint(a, b)` | 两点地理中心 |
| `dynamic_radius(a, b)` | 搜索半径（最大 5000m） |
| `search_pois_nearby(...)` | 高德 place/around，返回最多 20 个 POI |

---

### `/api/v2/search` M3 完整串联流程

```
用户请求
  │
  ├── Agent1 parse_query(query)          → keyword / min_rating / w1/w2/w3
  │     agent1_to_search_params()        → {keyword, min_rating, weights{}}
  │
  ├── 并发计算基准路线（A→家，B→家）
  │
  ├── Agent2 search_candidates()
  │     工具1 get_search_center()        → center / radius（纯数学，无API）
  │     工具2 fetch_pois()               → 高德 place/around，完整POI存Python侧
  │     LLM 语义筛选 _llm_filter()       → selected_idx（最多10个）
  │
  ├── M2 calc_detour_for_poi() × N       → A/B 绕路时间（高德路线 API）
  ├── M2 enrich_and_rank()               → 顺路标签 + 综合评分排序
  ├── fallback（若全部 none）
  │
  ├── Agent3 generate_summary()          → 2~4句推荐语
  │
  └── 返回 JSON（含 summary 字段）
```

**响应新增字段（M3 接入后）：**

```json
{
  "summary": "海底捞和南门涮肉评分都是 4.8，两人绕路都在 12 分钟以内..."
}
```

#### M3 端到端测试结果

**测试时间：** M3 会话，三 Agent 串联后

**输入：**
- A_work: 国贸 (116.4615, 39.9087) → A_home: 大兴 (116.4074, 39.8673)
- B_work: 中关村 (116.3097, 39.9835) → B_home: 上地 (116.2765, 40.0353)
- query: `"评分高的火锅店"`，prefer: transit

**结果：** HTTP 200，23.4s

- 所有 10 个结果 `detour_label: "none"`（绕路 38~88 分钟，远超 15 分钟阈值）
- fallback 正常触发，返回按评分降序的 10 家店
- `summary` 字段正确引用真实数据，无编造数字

**说明：** 测试坐标跨度较大（A/B 工作地直线距离约 12km，各自基准路线 ~61/78 分钟），导致所有结果均为 `none` 并触发 fallback，这是**预期行为**，fallback 机制运作正常。

**完整请求/响应存档：** [`docs/e2e_test_m3.json`](docs/e2e_test_m3.json)（含 request body、response 元数据、前三名 fallback 店信息及 summary 字段）。