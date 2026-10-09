#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
normalize.py —— 把麦当劳 MCP 的真实返回结构归一化为求解器输入

本模块记录了实际调通 MCP 后总结的字段映射（含踩坑注释）：

    query-meals         →  {"categories":[{"name":..,"meals":[{"code":..,"tags":[..]}]}]}
                        ⚠️ 只有 code 和 tags，**没有 name 和 price**
  query-meal-detail   →  {"code":..,"name":..,"image":..,"rounds":[..]}
                        单品 rounds 为空；套餐 rounds[] 描述可选子项与补差价
                        ⚠️ 1+1「随心配」的 rounds[] 里，每个 round 有多个 choices[]，
                           携带 diffPrice（补差价，分），isDefault=1 为默认项。
                           这正是「换件」能力的来源：同一份 1+1 可以换主食/换饮料。
  list-nutrition-foods→  形如 "[160]{字段名列表}:\n  名称,描述,kj,kcal,蛋白,脂肪,碳水,钠,钙\n ..."
  calculate-price     →  {"price":分, "productList":[...], "takeWayList":[...]}
  query-store-coupons →  [{"title":..,"couponId":..,"couponCode":..,"products":[...]}]
"""

import re
from typing import Dict, List, Optional

from feast_solver import (
    Meal, CATEGORY_MAIN, CATEGORY_SIDE, CATEGORY_DRINK, CATEGORY_DESSERT,
)

# ---------------------------------------------------------------------------
# 分类推断：MCP 菜单的 category 名称 → 内部类别
# ---------------------------------------------------------------------------
_DRINK_HINTS = ("饮料", "咖啡", "饮品", "麦咖啡", "茶", "汽水")
_DESSERT_HINTS = ("甜品", "冰淇淋", "冰品", "甜点")
_SIDE_HINTS = ("小食", "配餐", "薯", "鸡块", "炸鸡", "蘸酱")

# ---------------------------------------------------------------------------
# 名称层面的类别判断（**优先级高于菜单分类名**）
#
# 原因：实际抓取发现麦当劳菜单的分类名存在「混装」现象，
#       例如「小食甜品/其他」这一分类里同时装着薯条、玉米杯、冰淇淋、
#       麦旋风、甚至周边玩具（"葱不求人按摩捶"）。
#       若按分类名直接归类，会把薯条错分成甜品、把玩具当成食物。
#       因此：**先看具体名称，名称无法判断时才退回分类名。**
# ---------------------------------------------------------------------------
_DRINK_NAME_HINTS = ("可乐", "雪碧", "芬达", "咖啡", "拿铁", "美式", "澳白", "卡布",
                     "茶", "奶昔", "豆浆", "橙汁", "柠檬", "怡泉", "麦炫酷",
                     "牛奶", "椰子水", "气泡", "果茶", "热巧", "黑巧")
_DESSERT_NAME_HINTS = ("圆筒", "新地", "麦旋风", "圣代", "冰淇淋", "布丁", "派",
                       "饼干")
_SIDE_NAME_HINTS = ("薯条", "薯饼", "鸡块", "玉米杯", "沙拉", "脆汁鸡", "鸡翅",
                    "扭扭薯", "大鸡排", "鸡排", "麦乐鸡", "蘸酱", "拉丝芝士条")

# 非食品（周边/玩具/礼盒），直接排除，避免被当成可点餐品
_NONFOOD_HINTS = ("痒痒挠", "按摩捶", "耳机包", "徽章", "贴纸", "玩具", "周边",
                  "玩偶", "抱枕", "钥匙扣", "帆布包", "联名帽", "棒球帽",
                  "百宝袋", "盲盒", "礼盒", "摆件", "挂件", "手办")

# 可食用的信号词：用于反向校验。名称里若既无这些词、又不在营养表，
# 则很可能是周边或占位商品，需人工确认（在 build_meals 中降权处理）。
_FOOD_HINTS = ("堡", "卷", "鸡", "鱼", "牛", "猪", "薯", "派", "咖啡", "可乐",
               "雪碧", "茶", "奶", "冰", "饭", "蛋", "肠", "排", "块", "条",
               "杯", "沙拉", "玉米", "豆浆", "果汁", "橙", "柠檬", "水",
               "麦旋风", "新地", "圆筒", "饼干", "麦满分", "随心配", "套餐",
               "盒", "盘", "拼", "餐", "酱", "火腿", "培根", "芝士", "包")


def is_nonfood(meal_name: str) -> bool:
    """判断是否为非食品（周边商品），这类不应参与配餐。"""
    name = meal_name or ""
    return any(h in name for h in _NONFOOD_HINTS)


def looks_like_food(meal_name: str) -> bool:
    """启发式判断名称是否像食物。"""
    name = meal_name or ""
    return any(h in name for h in _FOOD_HINTS)


def infer_category(category_name: str, meal_name: str = "") -> str:
    """推断餐品类别。

    优先级：具体名称关键词 > 菜单分类名 > 默认主食。
    （分类名可能混装，故名称优先。）
    """
    name = meal_name or ""

    # 1) 名称优先：越具体的信号越可信
    if any(h in name for h in _DESSERT_NAME_HINTS):
        return CATEGORY_DESSERT
    if any(h in name for h in _DRINK_NAME_HINTS):
        return CATEGORY_DRINK
    if any(h in name for h in _SIDE_NAME_HINTS):
        return CATEGORY_SIDE

    # 2) 退回分类名
    text = category_name or ""
    if any(h in text for h in _DRINK_HINTS):
        return CATEGORY_DRINK
    if any(h in text for h in _DESSERT_HINTS):
        return CATEGORY_DESSERT
    if any(h in text for h in _SIDE_HINTS):
        return CATEGORY_SIDE

    return CATEGORY_MAIN


def parse_menu_codes(meals_payload: Dict) -> List[Dict]:
    """解析 query-meals 返回，展开为 [{code, category, tags}]。

    输入形如: {"categories":[{"name":"人气热卖","meals":[{"code":"1100","tags":[...]}]}]}
    """
    out = []
    seen = set()
    for cat in (meals_payload or {}).get("categories", []):
        cat_name = cat.get("name", "")
        for m in cat.get("meals", []):
            code = m.get("code")
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({
                "code": code,
                "category_name": cat_name,
                "tags": m.get("tags", []) or [],
            })
    return out


def parse_nutrition(text: str) -> Dict[str, Dict]:
    """解析 list-nutrition-foods 的 TSV 文本，返回 {餐品名: 营养字典}。

    原始文本形如：
        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,
              carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          ...
    注意：nutritionDescription 实测**恒为 null**。
    """
    table: Dict[str, Dict] = {}
    if not isinstance(text, str):
        return table

    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("[") or line.endswith("}:"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        name = parts[0]
        if not name:
            continue
        # 防御：名称里带逗号会把列错位，此处要求数值列可解析
        try:
            table[name] = {
                "energy_kj": _to_float(parts[2]),
                "calories": _to_int(parts[3]),
                "protein": _to_float(parts[4]),
                "fat": _to_float(parts[5]),
                "carbohydrate": _to_float(parts[6]),
                "sodium": _to_float(parts[7]),
                "calcium": _to_float(parts[8]),
            }
        except (ValueError, IndexError):
            continue
    return table


def merge_nutrition(meal_name: str, table: Dict[str, Dict]) -> Dict:
    """为某个餐品名匹配营养数据。

    实测发现营养表与菜单的命名并不一致，例如：
        营养表「可乐中杯」    vs 菜单「可乐」
        营养表「迷你薯条」    vs 菜单「中薯条」
        营养表「麦乐鸡5块」   vs 菜单「麦乐鸡5块」（一致）
    因此采用**四级回退**匹配，从严格到宽松：
        1. 精确匹配
        2. 归一化匹配（去掉空格/括号/™/大小杯等噪声后比较）
        3. 双向包含匹配，并校验长度差
        4. 规格归一（剥离中/大/小杯后缀）后包含匹配
    命中过高风险词（如仅"可乐"匹配到"无糖可乐"）时要求规格一致。
    """
    if not table or not meal_name:
        return {}

    # 1) 精确
    if meal_name in table:
        return table[meal_name]

    target = _normalize_name(meal_name)

    # 2) 归一化精确
    norm_index = {}
    for key in table:
        norm_index.setdefault(_normalize_name(key), key)
    if target in norm_index:
        return table[norm_index[target]]

    # 剥离规格后缀后的精确
    target_no_size = _strip_size(target)
    for key in table:
        if _strip_size(_normalize_name(key)) == target_no_size and target_no_size:
            return table[key]

    # 3) 双向包含（取最长命中，并限制长度差）
    # ⚠️ Codex 复审2 中等#3：宽松的双向包含会把「可乐」匹配到「无糖可乐」，
    #    在营养表存在多个相近规格时造成热量/糖分错配。命中前先做互斥修饰词校验。
    best_key, best_len = None, 0
    for key in table:
        nk = _normalize_name(key)
        if not nk:
            continue
        if (nk in target or target in nk) and len(nk) > best_len:
            # 长度差过大说明不是一个东西（如 "派" vs "冰糖葫芦酸奶风味派"）
            if abs(len(nk) - len(target)) <= 5 and _modifiers_compatible(target, nk):
                best_key, best_len = key, len(nk)
    if best_key:
        return table[best_key]

    # 4) 规格归一后的包含
    for key in table:
        nk = _strip_size(_normalize_name(key))
        if nk and len(nk) >= 2 and (nk in target_no_size or target_no_size in nk):
            if _modifiers_compatible(target_no_size, nk):
                return table[key]

    return {}


# 互斥修饰词：这些词会实质改变营养构成，跨词匹配即视为「不是同一个东西」。
# 例：菜单「可乐」不得匹配营养表的「无糖可乐」；「拿铁」不得匹配「燕麦拿铁」。
_EXCLUSIVE_MODIFIER_GROUPS = [
    ("无糖", "零度", "原味", "有糖"),
    ("低脂", "脱脂", "全脂"),
    ("燕麦", "豆乳"),
    ("冰", "热"),
]


def _modifiers_compatible(a: str, b: str) -> bool:
    """两组修饰词是否互斥。

    对每个互斥组，若 a 与 b 在该组内命中的词不同（一个命中、另一个不命中，
    或命中了组内不同词），则认为二者不是同一商品，返回 False。
    """
    for group in _EXCLUSIVE_MODIFIER_GROUPS:
        ha = [w for w in group if w in a]
        hb = [w for w in group if w in b]
        if ha != hb:
            return False
    return True


# 名称归一化：去掉噪声字符，统一规格词
_NOISE_RE = re.compile(r"[\s()（）\[\]【】™®·・,，、\-—/\\+]")
_SIZE_ALIASES = [
    ("特大杯", ""), ("大杯", ""), ("中杯", ""), ("小杯", ""),
    ("大", ""), ("中", ""), ("小", ""), ("迷你", ""),
]


def _normalize_name(name: str) -> str:
    """归一化餐品名：去噪声、去规格、小写。"""
    s = _NOISE_RE.sub("", name or "")
    return s.strip().lower()


def _strip_size(name: str) -> str:
    """剥离规格后缀（大/中/小/迷你杯），用于跨规格兜底匹配。"""
    s = name
    for alias, _ in _SIZE_ALIASES:
        if s.endswith(alias):
            s = s[: -len(alias)]
            break
    return s


def build_meals(menu_codes: List[Dict], details: Dict[str, Dict],
                nutrition: Dict[str, Dict], strict=True) -> List[Meal]:
    """合并三路数据，产出求解器可用的 Meal 列表。

    menu_codes: parse_menu_codes 的输出
    details:    {code: query-meal-detail 返回}
    nutrition:  parse_nutrition 的输出
    strict:     True 时过滤掉「名称不像食物 且 无营养数据」的可疑项
                （实测菜单里混有按摩捶、耳机包等周边，以及"如意百宝袋"这类
                 用途不明的占位商品，它们价格低会污染最优解）
    """
    meals: List[Meal] = []
    skipped = []
    for entry in menu_codes:
        code = entry["code"]
        detail = details.get(code) or {}
        name = detail.get("name") or ""
        if not name:
            # 详情缺失，跳过（避免出现无名餐品）
            continue
        if is_nonfood(name):
            skipped.append((name, "非食品关键词"))
            continue

        price = detail.get("price")
        if price is None:
            # 单品价格可能不在 detail 中，交由 calculate-price 补齐
            price = 0
        cat = infer_category(entry.get("category_name", ""), name)
        nut = merge_nutrition(name, nutrition)

        # 严格模式：无营养数据 + 名称不像食物 + 不是套餐 → 判定为可疑，剔除
        if (strict and not nut and not looks_like_food(name)
                and not _is_combo_name(name)):
            skipped.append((name, "无营养数据且名称不像食物"))
            continue

        # 营养兜底：套餐类（含"套餐/件套/随心选/随心配/两人/拼"）若单品表未命中，
        # 尝试用其子项（detail.rounds[].choices）的营养累加估算。
        if not nut and _is_combo_name(name):
            nut = _nutrition_from_rounds(detail, nutrition)
            if nut:
                nut["_estimated"] = True

        meals.append(Meal(
            code=code,
            name=name,
            price=int(price),
            category=cat,
            calories=nut.get("calories"),
            protein=nut.get("protein"),
            fat=nut.get("fat"),
            sodium=nut.get("sodium"),
            tags=list(entry.get("tags", [])),
            combo_items=parse_combo_items(detail),
            combo_rounds=parse_combo_rounds(detail, nutrition),
            nutrition_estimated=bool(nut.get("_estimated")),
        ))
    if skipped:
        build_meals.last_skipped = skipped
    return meals


build_meals.last_skipped = []


# 套餐类名称特征：这些餐品在单品营养表里通常查不到，
# 需要靠套餐子项累加估算营养。
_COMBO_HINTS = ("套餐", "件套", "随心选", "随心配", "两人", "双人", "拼",
                "四宫格", "全家", "分享", "组合", "盒", "盘")


def _is_combo_name(name: str) -> bool:
    return any(h in (name or "") for h in _COMBO_HINTS)


def _nutrition_from_rounds(detail: Dict, nutrition: Dict[str, Dict]) -> Dict:
    """从 query-meal-detail 的 rounds[].choices[] 中累加默认子项营养。

    套餐详情的默认选项（isDefault=1）即为实际组成，据此估算总营养。
    估算值会带 _estimated 标记，供上层决定是否采用。
    """
    if not isinstance(detail, dict) or not nutrition:
        return {}
    total = {"calories": 0, "protein": 0.0, "fat": 0.0, "sodium": 0.0}
    hit = 0
    required = 0
    for rnd in detail.get("rounds") or []:
        chosen = _default_choice(rnd)
        if not chosen:
            continue
        # 该区有默认项 → 计入「必须匹配成功」的分区数
        required += 1
        qty = chosen.get("quantity") or 1
        nut = merge_nutrition(chosen.get("name", ""), nutrition)
        if nut:
            hit += 1
            try:
                q = int(qty)
            except (TypeError, ValueError):
                q = 1
            total["calories"] += (nut.get("calories") or 0) * q
            total["protein"] += (nut.get("protein") or 0) * q
            total["fat"] += (nut.get("fat") or 0) * q
            total["sodium"] += (nut.get("sodium") or 0) * q
    # ⚠️ AGY 复审2 中等#2：原实现只要**任一**子项命中就返回累加值，
    #    例如「主食+薯条+可乐」只有主食匹配成功时，会把整个套餐标成主食的热量，
    #    真实热量被低估近一半 —— 这与「不谎称合规」的原则冲突。
    #    现要求**所有带默认项的分区都匹配成功**，否则整体判为无法估算（返回 {}）。
    if required == 0 or hit < required or total["calories"] <= 0:
        return {}
    total["protein"] = round(total["protein"], 1)
    return total


def _default_choice(rnd: Dict):
    """取某一 round 的默认选项（isDefault=1），没有则取第一项。"""
    choices = rnd.get("choices") or []
    for c in choices:
        if c.get("isDefault") in (1, "1", True):
            return c
    return choices[0] if choices else None


def parse_combo_items(detail: Dict) -> List[Dict]:
    """提取套餐的组成明细，用于在方案里展开展示「这个套餐到底含什么」。

    返回 [{"round": "选择套餐内小食", "name": "中薯条", "qty": 1}, ...]
    单品（rounds 为空）返回 []。
    """
    out = []
    if not isinstance(detail, dict):
        return out
    for rnd in detail.get("rounds") or []:
        chosen = _default_choice(rnd)
        if not chosen:
            continue
        try:
            qty = int(chosen.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        out.append({
            "round": rnd.get("name", ""),
            "name": chosen.get("name", ""),
            "code": chosen.get("code", ""),
            "qty": qty,
        })
    return out


# ---------------------------------------------------------------------------
# 套餐「可换件」结构解析 —— 1+1 随心配的核心
# ---------------------------------------------------------------------------
def parse_combo_rounds(detail: Dict,
                       nutrition: Optional[Dict[str, Dict]] = None) -> List[Dict]:
    """解析套餐的全部**可换备选**，而不只是默认项。

    为什么需要它：
        1+1「随心配」（¥14.9）的结构是「蓝区任选 1 + 粉区任选 1」，
        每个区都有 8~16 个备选，且**补差价不同**（-¥1 / ¥0）。
        例如蓝区：
            双层吉士汉堡（牛肉，默认，+¥0）
            那么大鸡排（鸡肉，-¥1）        ← 不吃牛的人应换到这个
            麦香鱼（鱼肉，+¥0）
            麦乐鸡5块（-¥1）
        如果不解析备选，遇到「不吃牛」的成员就只能整单放弃这份 1+1；
        解析后可以**换件保住优惠**，这正是本项目的差异化价值点。

    nutrition: 单品营养表。**若传入，则为每个备选关联其营养数据**，
               使换件后能重算真实热量/蛋白（Codex 审查要求的严重修复）。

    返回结构：
        [{"id": 1, "name": "蓝区", "min": 1, "max": 1,
          "choices": [{"code": "1120", "name": "双层吉士汉堡",
                       "diff": 0, "is_default": True, "quantity": 1,
                       "nutrition": {...} or None}, ...]},
         ...]

    diff 单位为分（与 diffPrice 一致），负数代表**比套餐基准价便宜**。
    单品（rounds 为空）返回 []。
    """
    out: List[Dict] = []
    if not isinstance(detail, dict):
        return out

    for rnd in detail.get("rounds") or []:
        choices_out = []
        for c in (rnd.get("choices") or []):
            name = c.get("name") or ""
            if not name:
                continue
            c_nut = merge_nutrition(name, nutrition) if nutrition else {}
            choices_out.append({
                "code": str(c.get("code") or ""),
                "name": name,
                "diff": _to_int(c.get("diffPrice")) or 0,
                "is_default": c.get("isDefault") in (1, "1", True),
                "quantity": _to_int(c.get("quantity")) or 0,
                "min": _to_int(c.get("minQuantity")) or 0,
                "max": _to_int(c.get("maxQuantity")) or 0,
                # 备选营养（可能为空 {}，上层据此判断「无法验证」）
                "nutrition": ({"calories": c_nut.get("calories"),
                               "protein": c_nut.get("protein"),
                               "fat": c_nut.get("fat"),
                               "sodium": c_nut.get("sodium")} if c_nut else None),
            })
        if not choices_out:
            continue
        # 一个 round 若既有 isDefault 但 quantity=0，说明默认未选，视为
        # 「必须选满 min 份」的选择区（1+1 的蓝区/粉区即此形态）。
        out.append({
            "id": rnd.get("id"),
            "name": rnd.get("name", ""),
            "min": _to_int(rnd.get("minQuantity")) or 0,
            # ⚠️ Codex 复审3 中等#1：必须区分「字段缺失」与「显式 0」。
            #    旧写法 `_to_int(...) or 0` 把两者压成同一个 0，使下游
            #    resolve_rounds 无法分辨「未声明上限」（应放宽到 need）
            #    与「上限为 0」（禁止选件）。缺失时保留 None。
            "max": _to_int(rnd.get("maxQuantity")),
            "choices": choices_out,
        })
    return out


# ⚠️ AGY 复审2 轻微#4：此处曾保留 pick_compliant_choice() 与
#    _choice_matches_forbidden() 两个函数，但换件逻辑已全部统一到
#    feast_solver.Meal.resolve_rounds，二者已无任何调用点（死代码），
#    故删除，避免出现「两套挑选实现」的漂移隐患。


def parse_price_result(payload: Dict) -> Dict:
    """解析 calculate-price 返回。

    返回 {"price": 分, "original": 分, "discount": 分, "items": [...], "take_way": [...]}
    ⚠️ 若传入的 items 用了错误字段名（code 而非 productCode），
       接口**不会报错**但 price 会静默为 0 —— 调用方需自行校验。
    """
    payload = payload or {}
    # ⚠️ AGY 审查：统一用健壮的 _to_int，避免接口返回 "14.90" 之类字符串时
    #    原生 int() 抛 ValueError。
    return {
        "price": _to_int(payload.get("price")) or 0,
        "original": _to_int(payload.get("originalPrice")) or 0,
        "product_price": _to_int(payload.get("productPrice")) or 0,
        "discount": _to_int(payload.get("discount")) or 0,
        "items": payload.get("productList") or [],
        "take_way": payload.get("takeWayList") or [],
    }


def parse_coupons(payload) -> List[Dict]:
    """解析 query-store-coupons / query-my-coupons 返回。"""
    if isinstance(payload, dict):
        payload = payload.get("list") or payload.get("coupons") or []
    out = []
    for c in (payload or []):
        out.append({
            "title": c.get("title") or c.get("name") or "",
            "coupon_id": c.get("couponId") or "",
            "coupon_code": c.get("couponCode") or "",
            "trade_time": c.get("tradeDateTime") or "",
            "products": [
                {"code": p.get("productCode"), "name": p.get("productName")}
                for p in (c.get("products") or [])
            ],
        })
    return out


# ---------------------------------------------------------------------------
def _to_int(v):
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None
