# 麦当劳 MCP 接入细节与踩坑记录

本文档记录本项目接入麦当劳官方 MCP（`https://mcp.mcd.cn`）时
**实际调通并踩过的坑**，全部来自真实调用而非文档推测。

---

## 一、协议基础

| 项目 | 值 |
|---|---|
| 端点 | `https://mcp.mcd.cn` |
| 传输 | Streamable HTTP（非 SSE） |
| 协议 | JSON-RPC 2.0 |
| 鉴权 | `Authorization: Bearer <token>` |
| Token 申请 | https://open.mcd.cn/mcp |
| 客户端实现 | `scripts/mcd_client.py`（零依赖，`urllib` + `ssl` + `json`） |

请求体形态：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "query-meals",
    "arguments": { "storeCode": "<storeCode>", "beType": 1, "orderType": 1 }
  }
}
```

> 注意：本 MCP 的响应可能是 **SSE 风格的多行 `data:` 前缀**，需要剥壳后再 `json.loads`。
> `mcd_client.py` 中已做兼容处理。

---

## 二、本项目的 MCP 调用流程（5 步）

```
[1/5] query-nearby-stores     定位门店 → storeCode / beCode
[2/5] query-meals             拉菜单 → 只有 code + tags，**没有名称和价格**
[3/5] query-meal-detail ×N    逐个补名称/组成/换件结构
      list-nutrition-foods    营养表（一次拉全）
      calculate-price ×M      为 price=0 的餐品补价
[4/5] （纯本地）求解
[5/5] calculate-price         对最终方案做权威核价
```

---

## 三、踩坑记录（重要）

### 坑 1：`query-meals` 不返回名称和价格

**现象**：`query-meals` 的返回里每个餐品**只有 `code` 和 `tags`**，
没有 `name`、没有 `price`。

**影响**：必须再对每个 code 调 `query-meal-detail` 才能拿到名称与组成。

**应对**：本项目只对前 N 个（`--max-detail`，默认 60）取详情以控制调用量，
并在 CLI 中**显式报告覆盖率**（"另有 X 个未参与求解"）——
避免把"菜单范围外无解"误报成"预算内无解"。

### 坑 2：`calculate-price` 用 `productCode`，不是 `code`

**现象**：传 `{"code": "1120", "quantity": 1}` 时，接口**不报错**，
但 `price` **静默返回 0**。

**根因**：字段名必须是 `productCode`。

**影响**：这是最危险的静默失败模式——看起来调用成功，其实价格是 0。

**应对**（双重）：
1. 一律用 `{"productCode": ..., "quantity": ...}`
2. 本项目的核价环节**把 `price=0` 视为"核价未通过"**，
   显式标记"价格未核验"，绝不当作成功。

```python
pr = parse_price_result(cli.calculate_price(...))
if pr["price"] > 0:
    plan.total_cost = pr["price"] + swap_diff
else:
    # 静默失败：保留本地预测值，但显式告警
    plan.notes.append("⚠ 核价未返回有效价格（price=0），当前为本地预测值")
```

### 坑 3：`beCode` 的传递规则（传错会报错）

| 场景 | `beType` | 是否传 `beCode` |
|---|---|---|
| 到店自取 | 1 | **不传**（传了会报错） |
| 麦乐送 | 2 | 传（`orderType=2`） |
| 得来速 | 5 | **必传** |
| 团餐 | 6 | **必传** |

实现见 `mcd_client.py::_attach_becode`：

```python
need = be_type in (5, 6) or order_type == 2
```

> 本规则曾漏掉 `beType=6`（团餐），由 AGY 审查发现并修复。

### 坑 4：套餐可换件结构藏在 `rounds[].choices[]` 里

**现象**：`query-meal-detail` 的 `rounds[]` 描述套餐的每个"选择区"，
每个区有 `minQuantity` / `maxQuantity`，区内的 `choices[]` 才是全部备选：

```json
{
  "rounds": [
    {
      "id": 1,
      "name": "蓝区",
      "minQuantity": 1,
      "maxQuantity": 1,
      "choices": [
        { "code": "1120", "name": "双层吉士汉堡", "diffPrice": 0,
          "isDefault": 1, "quantity": 1 },
        { "code": "521156", "name": "那么大鸡排（椒盐风味）",
          "diffPrice": -100, "isDefault": 0, "quantity": 0 },
        ...
      ]
    }
  ]
}
```

**关键语义**（实测确认）：

- `diffPrice`：补差价，单位为**分**，负数表示比套餐基准价**便宜**
- `isDefault`：是否默认选中项
- `quantity`：该备选**一次带来几份**（实测分布：0/1 为主，少数 2/3；
  0 表示"未指定"，按 1 份处理）→ 补差价与营养都必须按此份数乘
- 区的 `minQuantity`：**至少要选几份**（0 = 可选，>0 = 必选）
- 区的 `maxQuantity`：最多几份；`0` 表示**不允许选**；`-1` 表示无上限

**这是本项目"换件"能力的来源**：解析全部备选后，就能为忌口用户
在同一份套餐内换到合规项，而不是整单放弃。

### 坑 5：营养表与菜单**命名不一致**

**现象**：营养表的名称和菜单的名称不是同一套：

```
营养表「可乐中杯」   vs  菜单「可乐」
营养表「迷你薯条」   vs  菜单「中薯条」
```

**应对**：`normalize.py::merge_nutrition` 做**四级回退匹配**：

1. 精确匹配
2. 归一化匹配（去空格/括号/™/规格词后比较）
3. 双向包含匹配（限制长度差 ≤5，避免"派"匹配到"冰糖葫芦酸奶风味派"）
4. 规格归一（剥离中/大/小/迷你杯）后包含匹配

### 坑 6：菜单分类名"混装"，不能按分类名归类

**现象**：「小食甜品/其他」这一分类里同时装着薯条、玉米杯、冰淇淋、
麦旋风，**甚至周边玩具**（"葱不求人按摩捶"）。

**应对**：`infer_category` **先看具体名称，名称无法判断时才退回分类名**；
另设 `is_nonfood()` 用周边关键词（按摩捶/耳机包/徽章/盲盒…）直接剔除。

### 坑 7：套餐营养查不到，需按子项累加

**现象**：套餐类餐品（"XX三件套"）在单品营养表里通常查不到。

**应对**：`_nutrition_from_rounds()` 取每个 round 的**默认项**（`isDefault=1`）
营养累加估算，并打上 `nutrition_estimated=True` 标记。

> ⚠️ 估算值与实测值必须区分对待。本项目的「健康版」策略只在
> **全部餐品营养可验证**的组合中挑选，估算值也会单独标注。

---

## 四、调用量控制

| 参数 | 默认 | 作用 |
|---|---|---|
| `--max-detail` | 60 | 最多取多少个餐品详情 |
| `--max-price-fill` | 40 | 最多补多少个餐品的价格 |
| `--no-price-fill` | off | 完全跳过价格补齐 |

真实模式一次完整跑约 **100~110 次** MCP 调用
（1 门店 + 1 菜单 + 60 详情 + 1 营养表 + 补价 + 核价）。

> 为什么默认不拉全部详情：这是个真实的**调用量与信息完整度的权衡**。
> 本项目的态度是**显式告知覆盖率**，而不是假装菜单就是全部。

---

## 五、字段映射速查表

| MCP 工具 | 关键返回字段 | 归一化后 |
|---|---|---|
| `query-nearby-stores` | `storeCode` / `storeName` / `beCode` | 门店定位 |
| `query-meals` | `categories[].meals[].code` / `.tags` | `parse_menu_codes` |
| `query-meal-detail` | `name` / `rounds[].choices[]` | `parse_combo_items` + `parse_combo_rounds` |
| `list-nutrition-foods` | 文本表（名称,描述,kj,kcal,蛋白,脂肪,碳水,钠,钙） | `parse_nutrition` |
| `calculate-price` | `price`（分）/ `productList` / `takeWayList` | `parse_price_result` |
| `query-store-coupons` | `title` / `couponId` / `couponCode` / `products` | `parse_coupons` |

> **优惠券说明**：券的适用条件（满减门槛、限定商品、每人限用）无法仅凭本地数据
> 可靠推断，故本项目**不把券算进求解择优**，仅透传 `coupon_code` 给 MCP，
> 真实到手价以 `calculate-price` 的权威核价为准。这是**诚实取舍**，不是能力缺失。

---

## 六、复现方式

```bash
# 1) 无需 Token：用内置真实快照跑通全流程
python feast_cli.py --demo --budget 60 --people "我:减脂" "小王:不吃牛"

# 2) 真实调用
export MCD_MCP_TOKEN=你的token
python feast_cli.py --city <城市> --keyword <地标名> --budget 50 \
    --people "我:减脂" "小王:不吃牛,不要辣" --verbose
```

`--verbose` 会打印每个被跳过餐品的异常原因，便于定位接口问题。
