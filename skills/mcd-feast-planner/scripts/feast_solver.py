#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feast_solver.py —— 多人聚餐多约束组合求解器（零依赖，纯本地确定性计算）

核心主张：**数值不进 LLM**。
  - MCP 只负责取真实数据（菜单、价格、营养、券）
  - 全部算术与组合优化由本模块的纯函数完成
  => 每个方案可复现、可解释、可回归测试

解决的问题：多人聚餐时的「众口难调」
  1. 每个人的忌口/过敏（不吃牛肉、乳糖不耐、素食…）
  2. 每个人的目标（减脂控热量、增肌高蛋白、无所谓）
  3. 人均预算上限
  4. 组合后的总价、总热量、人均分摊
  5. 一次给出多个可行方案（性价比 / 健康 / 豪华），而非单一答案

工程约束（真实踩坑）：
  - 金额一律用「分」为单位的整数，避免浮点误差
  - MCP query-meals 只返回 code + tags，名称/价格需另行补全
  - calculate-price 用 productCode 字段（非 code），传错会静默返回 0
"""

from itertools import product as cartesian
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
UNLIMITED = 10 ** 9

# ---------------------------------------------------------------------------
# 忌口规则：key → 需要排除的餐品关键词（与餐品名/套餐子项名做包含匹配）
#
# ⚠️ 词库必须覆盖**不显式含肉字**的餐品，否则会漏网。实测踩坑：
#    「双层吉士汉堡」「吉士汉堡包」是牛肉，但名称里没有"牛"字；
#    「不素之霸」是双层牛肉；「麦香鱼」是鱼肉但含"鱼"可捕获；
#    「板烧鸡腿堡」是鸡肉但常被简称为"板烧"。
#    因此每个类别都同时收录「显式词」与「隐性词」。
#
# ⚠️⚠️ 2026-10-09 二次踩坑（真实事故）：素食者被推荐了「鱼牛汉堡四件套随心选」，
#     换件后选到「巨无霸」（纯牛肉）与「薄皮焦香V翅」（鸡翅）——
#     因为旧词库只写了"牛/鸡/肉"，而"巨无霸""薄皮焦香V翅"里一个字都不含。
#     结论：**隐性词必须逐条穷举**，不能靠"肉"字兜底。
#     为此抽出 _MEAT_HIDDEN / _CHICKEN_HIDDEN 等共享词表，让所有相关规则复用，
#     避免某条规则漏了某个隐性词。
# ---------------------------------------------------------------------------

# 命中「含肉」但不含任何肉字的商品名（跨规则共享）
_MEAT_HIDDEN = [
    "巨无霸", "不素之霸", "安格斯", "吉士汉堡", "吉士牛", "双层吉士",
    "芝士牛", "牛肉堡", "板烧",           # 板烧=鸡腿堡简称
]
# 鸡肉隐性词
# ⚠️ 2026-10-09 四次踩坑（自测端到端发现）：素食者被分到「酥酥多笋卷」——
#    它是**鸡肉卷**（川香风味，蛋白 14g），但名称里"酥/笋/卷"一个字都不含肉。
#    与「巨无霸」同类陷阱：**隐性词必须逐条穷举**。
#    「酥酥多笋卷」及其三/四件套都靠这个词兜住。
_CHICKEN_HIDDEN = [
    "板烧", "麦乐鸡", "麦香鸡", "脆汁", "脆鸡", "鸡排", "鸡腿", "鸡翅",
    "V翅", "鸡块", "鸡球", "鸡柳", "琵琶腿", "麦辣",   # 麦辣鸡腿堡/麦辣鸡翅
    "酥酥多笋卷", "多笋卷",                            # ⚠️ 实为鸡肉卷
]
# 牛肉隐性词
_BEEF_HIDDEN = [
    "巨无霸", "不素之霸", "安格斯", "吉士汉堡", "吉士牛", "双层吉士",
    "芝士牛", "牛肉堡", "双层牛", "厚牛",
]
# 猪肉隐性词
_PORK_HIDDEN = ["培根", "烟肉", "火腿", "猪柳", "猪肉"]
# 鱼类隐性词
_FISH_HIDDEN = ["鱼", "虾", "蟹", "鳕鱼", "麦香鱼", "海味"]
# 油炸隐性词
_FRIED_HIDDEN = ["炸", "脆", "薯条", "薯饼", "麦乐鸡", "脆汁鸡", "鸡排",
                 "鸡块", "扭扭薯", "鸡球", "鸡柳", "V翅", "鸡翅"]
# 乳制品隐性词
# ⚠️ 注意「吉士」既可能是乳制品（芝士=cheese 音译），也可能出现在肉品名里
#    （如「吉士汉堡」实为牛肉堡）—— 因此它同时出现在 _BEEF_HIDDEN 与
#    _DAIRY_HIDDEN 中，两边都不放过（宁可保守）。
_DAIRY_HIDDEN = ["奶", "芝士", "吉士", "新地", "麦旋风", "拿铁", "澳白",
                 "圆筒", "布丁", "奶铁", "圣代", "雪冰", "冰淇淋", "酸奶"]

# 蛋类隐性词（素食需排除）
# 实测菜单里「早餐全餐」「蛋堡」「麦满分」等含蛋，名称未必显式写"蛋"，
# 但「蛋」「蛋黄酱」「蛋卷」「滑蛋」「炒蛋」「太阳蛋」「蛋白」已足够覆盖常见款。
_EGG_HIDDEN = ["蛋", "蛋黄酱", "沙拉酱", "美乃滋"]

DIET_RULES = {
    "no_beef": {
        "label": "不吃牛肉",
        "keywords": ["牛"] + _BEEF_HIDDEN,
    },
    "no_pork": {
        "label": "不吃猪肉",
        "keywords": ["猪"] + _PORK_HIDDEN,
    },
    "no_chicken": {
        "label": "不吃鸡肉",
        "keywords": ["鸡"] + _CHICKEN_HIDDEN,
    },
    "no_fish": {
        "label": "不吃鱼虾",
        "keywords": _FISH_HIDDEN,
    },
    "no_fried": {
        "label": "不吃油炸",
        "keywords": _FRIED_HIDDEN,
    },
    "no_dairy": {
        "label": "不吃乳制品",
        "keywords": _DAIRY_HIDDEN,
    },
    "no_spicy": {
        "label": "不吃辣",
        "keywords": ["辣", "川香", "韩式", "麻辣", "香辣", "山葵", "芥末"],
    },
    "vegetarian": {
        "label": "素食（蛋奶素）",
        # 蛋奶素 = 不吃肉/鱼/家禽，但**允许**乳制品与蛋类。
        #
        # ⚠️ 2026-10-09 三次踩坑（Codex 审查发现）：素食规则只排了肉/鱼，
        #    却漏了**乳制品与蛋类**，导致"芝士玉米杯""新地""圆筒"等被当成素食。
        # ⚠️ Codex 复审2 轻微#1：上一轮"修法"把乳制品+蛋类也塞进了 vegetarian，
        #    于是标签写"蛋奶素"、行为却是排除蛋奶 —— 标签与行为自相矛盾，
        #    蛋奶素用户会被莫名剥夺「蛋堡/芝士玉米杯/拿铁」等可接受食物。
        #    现有两个独立规则，各行其是：
        #      · vegetarian = 蛋奶素（排肉鱼禽，**保留**乳制品与蛋）
        #      · vegan      = 严格纯素（在蛋奶素基础上再排乳制品与蛋）
        "keywords": (["牛", "猪", "鸡", "鱼", "虾", "蟹", "肉", "荤"]
                     + _BEEF_HIDDEN + _PORK_HIDDEN + _CHICKEN_HIDDEN
                     + _FISH_HIDDEN),
    },
    "vegan": {
        "label": "严格素食（纯素）",
        # 严格纯素：一切动物性成分都排除，含乳制品与蛋类。
        "keywords": (["牛", "猪", "鸡", "鱼", "虾", "蟹", "肉", "荤"]
                     + _BEEF_HIDDEN + _PORK_HIDDEN + _CHICKEN_HIDDEN
                     + _FISH_HIDDEN + _DAIRY_HIDDEN + _EGG_HIDDEN),
    },
    "no_seafood": {
        "label": "不吃海鲜",
        "keywords": _FISH_HIDDEN + ["海鲜", "贝", "蛤", "鱿"],
    },
}

# 餐品分类（用于保证一餐的结构完整性）
CATEGORY_MAIN = "main"       # 主食（汉堡/卷）
CATEGORY_SIDE = "side"       # 小食
CATEGORY_DRINK = "drink"     # 饮料
CATEGORY_DESSERT = "dessert"  # 甜品


def _choice_hits(choice: Dict, forbidden) -> bool:
    """套餐备选项是否命中忌口词（只看名称；备选无 tags/子项）。"""
    if not forbidden:
        return False
    name = choice.get("name", "") or ""
    return any(k in name for k in forbidden)


def _final_combo_text(meal: "Meal", qty: int, resolved: Dict) -> str:
    """某人这一单里套餐的**实际**组成文本（含换件结果）。

    resolved: {餐品code: [{"round":..,"name":..}, ...]}
    若无换件记录，退回默认组成。

    ⚠️ AGY 审查发现：旧实现收了 qty 却没用，导致点 2 份时默认组成显示
       "×2" 而换件后文本退化成单份。此处统一乘上份数。
    """
    picks = (resolved or {}).get(meal.code)
    if not picks:
        return meal.combo_text()
    parts = []
    for p in picks:
        if p.get("dropped"):
            continue
        parts.append(p["name"])
    if not parts:
        return meal.combo_text()
    text = " + ".join(parts)
    return "%s×%d" % (text, qty) if qty > 1 else text


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Meal:
    """归一化后的餐品。金额单位：分（整数）。"""
    code: str
    name: str
    price: int                 # 单价（分）
    category: str = CATEGORY_MAIN
    calories: Optional[int] = None      # 千卡
    protein: Optional[float] = None     # 克
    fat: Optional[float] = None
    sodium: Optional[float] = None      # 毫克
    tags: List[str] = field(default_factory=list)
    combo_items: List[Dict] = field(default_factory=list)  # 套餐组成明细（默认项）
    combo_rounds: List[Dict] = field(default_factory=list)  # 全部可换备选（换件来源）
    nutrition_estimated: bool = False   # 营养是否为估算值（套餐子项累加）

    @property
    def price_yuan(self) -> float:
        return self.price / 100.0

    @property
    def is_combo(self) -> bool:
        return bool(self.combo_items)

    @property
    def is_combo_like(self) -> bool:
        """是否为「套餐形态」的餐品 —— 基于名称判断，不依赖详情是否取到。

        用途：当有忌口的用户点餐时，若某餐品疑似套餐但组成未知，
        必须保守排除（因为我们无法确认它含什么）。
        """
        if self.combo_items:
            return True
        name = self.name
        return any(h in name for h in (
            "套餐", "件套", "随心选", "随心配", "四宫格", "两人", "双人",
            "分享餐", "拼盘", "全能盒", "全盒", "随心拼", "八件套",
            # ⚠️ AGY 审查发现：「蘸酱鸡块/蘸酱脆皮鸡柳」实为「主体 + 选蘸酱」
            #    的复合结构，名称里没有上述任何词，详情取失败时会漏判为单品。
            "蘸酱", "任选", "自选", "组合",
        ))

    @property
    def has_swappable_choices(self) -> bool:
        """是否含「可换件」结构 —— 即存在带多个备选的选择区。

        注意：这与「是套餐」不是一回事。
          1+1 随心配、三件套、四件套、随心选、全家桶这类都有选择区，
          用户可以在同价位内自行组合；而固定套餐（如「安格斯厚牛堡套餐」）
          可能只有默认项，没有可换空间。
        只要任一 round 提供了 >= 2 个备选，就认为可换件。
        """
        return any(len(r.get("choices") or []) >= 2 for r in self.combo_rounds)

    def combo_text(self) -> str:
        """套餐组成的可读文本，如 "中薯条 + 可乐中杯"。"""
        if not self.combo_items:
            return ""
        return " + ".join(
            "%s×%d" % (it["name"], it["qty"]) if it["qty"] > 1 else it["name"]
            for it in self.combo_items
        )

    # ------------------------------------------------------------------
    # 换件（combo swap）：套餐通用能力，不限 1+1
    # ------------------------------------------------------------------
    def resolve_rounds(self, forbidden, prefer_keywords=None) -> Optional[Dict]:
        """按忌口为本套餐挑选一套「合规换件方案」。

        返回：
            {
              "ok": True/False,                    # 是否找到全部合规的选法
              "picks": [ {round, code, name, diff, is_default}, ... ],
              "diff_total": int,                   # 换件后相对基准价的补差价（分）
              "swaps": [ {round, frm, to, diff}, ... ],   # 仅列被换掉的项
              "unresolvable": [ {round, reason}, ... ],   # 无法换到合规项的选择区
              "nutrition": {"calories":.., "protein":.., "fat":.., "sodium":..}
                           or None,                 # 换件后**实际**营养（缺数据则 None）
                           # ⚠️ Codex 审查发现：旧实现换件后仍沿用套餐默认营养，
                           #    导致个人热量/蛋白约束与「健康版」判断基于错误餐品。
                           #    现按「实际选中的备选」重算营养；只要有一件缺营养
                           #    数据就返回 None（表示无法验证，不谎称合规）。
            }

        语义（重要）：
          - round 的 minQuantity 表示**至少要选几份**，maxQuantity 表示最多几份。
            · 1+1 随心配：蓝区 min=1 max=1，粉区 min=1 max=1 → 各选 1 件。
            · 「堡堡随心拼（7件任选）」：主食 min=2、小食 min=3、饮料 min=2。
            · 「学生专享汉堡小食随心选」：min=2 max=2 → 必须选满 2 件。
          - 因此不能"每区只挑 1 件"，必须**按 min 数量挑满**；
            若可选件数不足 min，则该区无法满足 → 整单不可用。
          - min <= 0 的是「可选区」：默认**不选**（不自动补件），
            因为擅自补一件会改变套餐结构与价格；仅在显式要求时才补。
          - ⚠️ 若 max == 0，表示该区**不允许选任何件**，必须选 0 件。
            旧实现会强行 take=1 并选出一件，导致价格与订单组成非法。
          - diff_total = Σ(所选件 diffPrice) × 份数，计入最终价格。

        纯函数，可离线单测。
        """
        result = {"ok": True, "picks": [], "diff_total": 0,
                  "swaps": [], "unresolvable": [], "nutrition": None}
        if not self.combo_rounds:
            return result

        # 换件后的实际营养累加（只要有一件缺数据就整体置 None）
        nut_acc = {"calories": 0, "protein": 0.0, "fat": 0.0, "sodium": 0.0}
        nut_ok = True
        nut_seen = False

        for rnd in self.combo_rounds:
            # ⚠️ Codex 复审2 严重#3：必须严格区分「字段缺失」与「字段值为 0」。
            #    旧实现 `max_q = rnd.get("max") or 0` 把缺失和 0 混为一谈，
            #    又用 `if max_q == 0 and min_q == 0` 才跳过 —— 于是 min=1,max=0
            #    时 hard_max 被改写成 need(=1)，仍然选出 1 件，违反 chosen<=max。
            #    现在：显式 0 一律视为「禁止选件」；上限按 need 语义仅在缺失时放宽。
            _min = rnd.get("min")
            _max = rnd.get("max")
            min_q = _min if isinstance(_min, int) else 0
            max_q = _max if isinstance(_max, int) else None   # None = 未声明上限

            name = rnd.get("name", "")

            # 边界校验：负值或 min>max 的区间不可满足，直接判该区无效
            if min_q < 0 or (max_q is not None and max_q < 0) or \
                    (max_q is not None and min_q > max_q):
                result["ok"] = False
                result["unresolvable"].append(
                    {"round": name,
                     "reason": "选择区数量区间非法(min=%s, max=%s)"
                               % (_min, _max)})
                continue

            # max 显式为 0 → 该区不允许选任何件，无条件不选
            if max_q == 0:
                continue
            if min_q == 0 and max_q is None:
                # 可选区且未声明上限 → 默认不补件，保持套餐结构与价格不变
                continue

            need = min_q if min_q > 0 else 0
            # 上限：max 未声明时视为不限制（沿用 need 语义）
            hard_max = max_q if (max_q is not None and max_q > 0) else need
            if hard_max <= 0:
                continue
            choices = rnd.get("choices") or []
            if not choices:
                if need > 0:
                    result["ok"] = False
                    result["unresolvable"].append(
                        {"round": name, "reason": "该选择区无备选"})
                continue

            compliant = [c for c in choices
                         if not _choice_hits(c, forbidden)]
            if not compliant:
                if need > 0:
                    # 必选区且全部违禁 → 整单无法合规
                    result["ok"] = False
                    result["unresolvable"].append(
                        {"round": name, "reason": "该区所有备选都命中忌口"})
                else:
                    # 可选区 → 直接不选该件
                    result["picks"].append({
                        "round": name, "code": "", "name": "(不选)",
                        "diff": 0, "is_default": False, "dropped": True,
                    })
                continue

            # 需要挑几件：必选 min 件（不超过备选数、不超过 max 上限）。
            # min=0 的可选区默认挑 0 件（不擅自补件，保持套餐结构与价格不变）。
            take = need
            take = min(take, len(compliant), hard_max)
            if take < need:
                # 合规备选不够 min 件 → 该区满足不了
                result["ok"] = False
                result["unresolvable"].append(
                    {"round": name,
                     "reason": "合规备选(%d)少于必选数(%d)" % (len(compliant), need)})
                continue
            if take <= 0:
                # 可选区且未要求补件 → 不选
                continue

            # 排序：同价优先；同价时**偏好命中优先**，其次才轮到默认项。
            # ⚠️ Codex 复审2 中等#2：旧键 (diff, default, -pref) 让默认项压过偏好项，
            #    使「同价时优先偏好」的说明失效。改为 (diff, -pref, default)。
            def _key(c):
                pref = 0
                if prefer_keywords:
                    pref = 1 if any(k in c.get("name", "") for k in prefer_keywords) else 0
                return (c.get("diff", 0), -pref, 0 if c.get("is_default") else 1)

            chosen_list = sorted(compliant, key=_key)[:take]

            for chosen in chosen_list:
                diff = chosen.get("diff", 0)
                # ⚠️ Codex 审查：choice.quantity 是「该备选一次带几份」的真实语义
                #    （实测分布：0/1 为主，少数 2/3）。补差价与营养都必须按份数乘。
                cq = chosen.get("quantity") or 0
                if cq <= 0:
                    cq = 1              # 0 表示"未指定"，按 1 份处理
                try:
                    cq = int(cq)
                except (TypeError, ValueError):
                    cq = 1
                unit_diff = diff
                diff = unit_diff * cq
                result["picks"].append({
                    "round": rnd.get("name", ""),
                    "code": chosen.get("code", ""),
                    "name": chosen.get("name", ""),
                    "diff": diff,
                    "unit_diff": unit_diff,
                    "choice_qty": cq,
                    "is_default": bool(chosen.get("is_default")),
                    "dropped": False,
                })
                result["diff_total"] += diff

                # 换件后营养重算：备选若带 nutrition 就累加，缺则整体不可验证
                c_nut = chosen.get("nutrition")
                nut_seen = True
                if not c_nut:
                    nut_ok = False
                else:
                    nut_acc["calories"] += (c_nut.get("calories") or 0) * cq
                    nut_acc["protein"] += (c_nut.get("protein") or 0) * cq
                    nut_acc["fat"] += (c_nut.get("fat") or 0) * cq
                    nut_acc["sodium"] += (c_nut.get("sodium") or 0) * cq

            # 记录被换掉的项：默认项中未被选中的
            #
            # reason 区分两类换件（对用户解释很关键）：
            #   "diet"  = 默认项命中忌口，**不得不换**（安全性驱动）
            #   "price" = 默认项本身合规，但换成同价/更省的项（性价比驱动）
            default_codes = {c.get("code") for c in choices if c.get("is_default")}
            chosen_codes = {c.get("code") for c in chosen_list}
            for c in choices:
                if c.get("code") in default_codes and c.get("code") not in chosen_codes:
                    reason = "diet" if _choice_hits(c, forbidden) else "price"
                    result["swaps"].append({
                        "round": rnd.get("name", ""),
                        "frm": c.get("name", ""),
                        "to": ", ".join(x.get("name", "") for x in chosen_list),
                        "diff": sum((x.get("diff", 0) or 0)
                                    * max(1, x.get("quantity") or 1)
                                    for x in chosen_list),
                        "reason": reason,
                    })

        # 换件后营养：只有真正选过件（nut_seen）且全部有数据（nut_ok）才给出
        if nut_ok and nut_seen and nut_acc["calories"] > 0:
            nut_acc["protein"] = round(nut_acc["protein"], 1)
            nut_acc["fat"] = round(nut_acc["fat"], 1)
            nut_acc["sodium"] = round(nut_acc["sodium"], 1)
            result["nutrition"] = nut_acc
        return result

    def is_diet_safe(self, forbidden) -> bool:
        """本套餐能否通过换件满足全部忌口（不换件时按默认项判断）。"""
        if not self.combo_rounds:
            return not self.matches_any_keyword(forbidden)
        return self.resolve_rounds(forbidden)["ok"]

    def name_swap_note(self, forbidden) -> str:
        """当套餐的「营销名」与换件后的实际内容矛盾时，给一句更友好的提示。

        例：「牛肉堡单品精选」素食者换件后选的是「汉堡包」（无肉），
            名称仍带"牛肉堡"，会让人误以为推荐错了。此处主动澄清。
        """
        if not self.combo_rounds:
            return ""
        if not self.matches_any_keyword(forbidden):
            return ""
        res = self.resolve_rounds(forbidden)
        if not res["ok"]:
            return ""
        final = " + ".join(p["name"] for p in res["picks"]
                           if not p.get("dropped"))
        return "（该套餐为可选项组合，实际选择：%s）" % final

    def swapped_combo_text(self, forbidden) -> str:
        """换件后的组成文本（用于展示"换成什么了"）。"""
        if not self.combo_rounds:
            return self.combo_text()
        res = self.resolve_rounds(forbidden)
        if not res["ok"]:
            return self.combo_text()
        parts = []
        for p in res["picks"]:
            if p.get("dropped"):
                continue
            parts.append(p["name"])
        return " + ".join(parts)

    def matches_any_keyword(self, keywords) -> bool:
        """忌口匹配：同时检查餐品名与其套餐子项名。

        这点很重要 —— 例如「不素之霸双层牛堡三件套」名字里有"牛"，
        但其子项也可能是牛肉；而某些套餐名不含肉字，子项却含肉。
        双向检查才能不漏。
        """
        haystack = self.name + " " + " ".join(self.tags)
        if any(k in haystack for k in keywords):
            return True
        for it in self.combo_items:
            if any(k in it.get("name", "") for k in keywords):
                return True
        return False


@dataclass
class Person:
    """一位聚餐参与者。"""
    name: str
    diet: List[str] = field(default_factory=list)      # DIET_RULES 的 key
    max_calories: Optional[int] = None                 # 个人热量上限（千卡）
    min_protein: Optional[float] = None                # 个人蛋白下限（克）
    preference: Optional[str] = None                   # 口味偏好关键词

    def forbidden_keywords(self) -> set:
        kws = set()
        for d in self.diet:
            rule = DIET_RULES.get(d)
            if rule:
                kws.update(rule["keywords"])
        return kws

    def diet_labels(self) -> List[str]:
        return [DIET_RULES[d]["label"] for d in self.diet if d in DIET_RULES]


@dataclass
class FeastRequest:
    """一次聚餐需求。"""
    people: List[Person]
    budget_per_person: Optional[int] = None   # 人均预算（元，整数）
    store_code: Optional[str] = None
    store_name: Optional[str] = None
    be_type: int = 1
    order_type: int = 1
    be_code: Optional[str] = None
    # ⚠️ 优惠券说明（诚实标注，避免"未接线字段"误导）：
    #    coupon_codes / coupon_saving 目前**仅作透传与展示**，不参与求解择优。
    #    原因：券的适用条件（满减门槛、限定商品、每人限用）无法仅凭
    #    本地数据可靠推断，硬算会给出错误的"到手价"。
    #    真实核价仍以 MCP calculate-price 为准（已传 coupon_code）。
    #    若后续接入完整的券规则，再让其参与价格核验与目标函数。
    coupon_codes: List[str] = field(default_factory=list)

    @property
    def headcount(self) -> int:
        return len(self.people)

    @property
    def total_budget(self) -> Optional[int]:
        """总预算（分）。"""
        if self.budget_per_person is None:
            return None
        return self.budget_per_person * 100 * self.headcount


@dataclass
class Assignment:
    """某人分到的若干份餐品。"""
    person: str
    items: List[Tuple[Meal, int]]   # [(餐品, 份数)]
    swaps: List[Dict] = field(default_factory=list)   # 本单发生的换件记录
    swap_diff: int = 0              # 换件补差价合计（分，可为负）
    # 换件后实际选择：{餐品code: [{"round","name"}, ...]}
    resolved: Dict[str, List[Dict]] = field(default_factory=dict)
    # 换件后的**实际**营养覆盖：{餐品code: {"calories","protein",...}}
    # ⚠️ Codex 审查发现：换件后若仍用套餐默认营养，个人热量/蛋白约束会失真。
    #    有此覆盖则优先使用；无则退回套餐默认值（并在上层标记为不可验证）。
    nut_override: Dict[str, Dict] = field(default_factory=dict)

    @property
    def calories(self) -> int:
        return sum(self._nut(m).get("calories", 0) * q for m, q in self.items)

    @property
    def protein(self) -> float:
        return round(sum(self._nut(m).get("protein", 0.0) * q
                         for m, q in self.items), 1)

    @property
    def sodium(self) -> float:
        """本单钠含量（毫克）。与其他营养指标一致，尊重换件后的实测覆盖。

        ⚠️ AGY 复审2 轻微#1：此前 `_plan_metrics` 直接读静态 `m.sodium`，
            换件后（如把高钠项换成低钠项）钠指标不会更新，与热量/蛋白口径不一致。
        """
        return sum(self._nut(m).get("sodium", 0.0) * q for m, q in self.items)

    @property
    def nutrition_verified(self) -> bool:
        """本单营养是否**全部**可验证（换件项有实测营养 or 未换件）。"""
        for m, _q in self.items:
            if m.calories is None:
                return False
            if m.combo_rounds and m.code not in self.nut_override:
                # 换过件但没有实测营养 → 无法验证
                if m.code in (self.resolved or {}):
                    return False
        return True

    def _nut(self, m: Meal) -> Dict:
        """取该餐品在本单中的有效营养（换件覆盖优先）。"""
        ov = self.nut_override.get(m.code)
        if ov:
            return ov
        return {"calories": m.calories or 0, "protein": m.protein or 0.0,
                "fat": m.fat or 0.0, "sodium": m.sodium or 0.0}

    @property
    def cost(self) -> int:
        # 套餐基准价 + 换件补差价（负差价即更便宜）
        return sum(m.price * q for m, q in self.items) + self.swap_diff


@dataclass
class Plan:
    """一个完整可执行的聚餐方案。"""
    title: str
    strategy: str
    assignments: List[Assignment]
    total_cost: int            # 分
    coupon_saving: int = 0     # 分
    verified_price: Optional[int] = None   # calculate-price 核价结果（分）
    notes: List[str] = field(default_factory=list)

    # ---- 派生指标 ----
    @property
    def headcount(self) -> int:
        return len(self.assignments)

    @property
    def total_calories(self) -> int:
        return sum(a.calories for a in self.assignments)

    @property
    def per_person_cost(self) -> int:
        """人均（分）。"""
        return round(self.total_cost / self.headcount) if self.headcount else 0

    @property
    def per_person_calories(self) -> int:
        return round(self.total_calories / self.headcount) if self.headcount else 0

    def share(self) -> List[Dict]:
        """AA 精确分摊：逐人实付 + 余数分配，保证总额严格相等。

        业务口径（明确声明，避免歧义）：
            本函数按**人均均摊**（total // n），而不是按各人实际点单归集。
            这符合中国聚餐"AA 制"的通常语义——大家平摊订单总额。
            余数用「前 remainder 人各多付 1 分」的方式分配，保证
            Σshould_pay == total_cost（分毫不差，绝无四舍五入误差）。

        如果你需要**按消费归属**结算（谁点的谁付），请改用各行的
        `items[].subtotal` 与 `swap_diff`（即 Assignment.cost）——
        本行同时返回了这两项，调用方可自行归集，但那样余数/折扣
        的整数分尾差需要由调用方另行规则处理。
        """
        n = self.headcount
        if n == 0:
            return []
        base = self.total_cost // n
        remainder = self.total_cost - base * n   # 分，0 <= remainder < n

        rows = []
        for i, a in enumerate(self.assignments):
            # 余数按顺序分摊给前 remainder 个人，各自只多承担 1 分
            extra = 1 if i < remainder else 0
            rows.append({
                "person": a.person,
                "items": [
                    {
                        "name": m.name,
                        "qty": q,
                        "subtotal": m.price * q,
                        # 默认组成（静态菜单里的样子）
                        "combo": m.combo_text(),
                        # 本单实际组成（含换件后的选择）
                        "combo_final": _final_combo_text(m, q, a.resolved),
                    }
                    for m, q in a.items
                ],
                "swaps": list(a.swaps),
                "swap_diff": a.swap_diff,
                "calories": a.calories,
                "protein": a.protein,
                "nutrition_verified": a.nutrition_verified,
                "should_pay": base + extra,
            })
        return rows

    def to_dict(self) -> Dict:
        d = asdict(self)
        d.update({
            "total_cost_yuan": round(self.total_cost / 100, 2),
            "per_person_cost_yuan": round(self.per_person_cost / 100, 2),
            "total_calories": self.total_calories,
            "per_person_calories": self.per_person_calories,
            "coupon_saving_yuan": round(self.coupon_saving / 100, 2),
            "share": self.share(),
        })
        return d


@dataclass
class SolveResult:
    """求解结果：多个方案 + 诊断信息。"""
    plans: List[Plan] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    infeasible_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.plans)
