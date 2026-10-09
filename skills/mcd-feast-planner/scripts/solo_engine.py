#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
solo_engine.py —— 麦麦 1+1 自助（单人自选 + 自动推荐）

与 feast_engine（多人聚餐求解）并列的**单人**能力，互不干扰。

两种玩法：
  1. **手动自选**（list_choices + validate_selection）
     用户自己从「蓝区」「粉区」各挑 1 件，工具负责：
       - 列出全部备选（标出默认项、补差价、忌口冲突）
       - 实时校验：选中的项是否与本人忌口冲突
       - 冲突时不阻止，但**给出合规替换建议**（把选择权留给用户）
       - 实时算价（基准价 + 补差价，整数分）

  2. **自动推荐**（recommend_solo）
     输入个人忌口/热量上限/预算，自动给出多套 1+1（含换件），
     复用 feast_solver.Meal.resolve_rounds 的换件能力。

设计原则（与主项目一致）：
  - 零依赖、纯函数、可离线单测
  - 金额一律整数分
  - **数值不进 LLM**：所有算术在本地完成
  - 只读：不修改任何文件、不调用任何写操作的 MCP 工具
"""

import re
from typing import Dict, List, Optional, Tuple

from feast_solver import (
    Meal, Person, DIET_RULES,
    _choice_hits,
)
# 复用多人路径的偏好关键词解析，确保「偏好」在单/多两条路径语义一致
# （feast_engine 不反向导入本模块，无循环依赖）
from feast_engine import _prefer_keywords


# ---------------------------------------------------------------------------
# 1+1 形态识别
# ---------------------------------------------------------------------------
# 「1+1 随心配」的典型结构：恰好 2 个必选区，每区 min=max=1，
# 区名多含"蓝区""粉区"（个别门店叫"红区""黄区"或直接"A区/B区"）。
_BLUE_HINTS = ("蓝区", "红区", "A区", "a区", "甲区")
_PINK_HINTS = ("粉区", "黄区", "B区", "b区", "乙区")


def is_one_plus_one(meal: Meal) -> bool:
    """判断是否为「1+1」形态（两个必选区，各选 1 件）。

    注意：不依赖名称里是否写"随心配"——以**结构**为准更稳。
    """
    rounds = meal.combo_rounds or []
    required = [r for r in rounds if (r.get("min") or 0) >= 1]
    if len(required) != 2:
        return False
    for r in required:
        # max 缺失（None）不视为 1，避免把「未声明上限」误判成 1+1 的严格 1 件区
        if (r.get("min") or 0) != 1 or r.get("max") != 1:
            return False
        if len(r.get("choices") or []) < 2:
            return False
    return True


def zone_label(rnd: Dict, index: int) -> str:
    """给选择区一个友好名称：优先用 MCP 原名，其次按序号命名。"""
    name = (rnd.get("name") or "").strip()
    if name:
        return name
    return "第%d区" % (index + 1)


def list_choices(meal: Meal, person: Optional[Person] = None) -> List[Dict]:
    """列出该套餐每个区的全部备选，并标注「是否与本人忌口冲突」。

    返回：
        [{"zone": "蓝区", "index": 0, "min": 1, "max": 1,
          "choices": [{"code":..,"name":..,"diff":..,"is_default":bool,
                       "conflict":bool,"conflict_with":[忌口标签...]}, ...]},
         ...]

    conflict 标注是本模式的核心价值：**帮你一眼看出哪几个选项你不能吃**。
    """
    forbidden = person.forbidden_keywords() if person else set()
    out = []
    for i, rnd in enumerate(meal.combo_rounds or []):
        cs = []
        for c in (rnd.get("choices") or []):
            hit_labels = []
            if forbidden:
                hits = [k for k in forbidden if k in (c.get("name") or "")]
                if hits:
                    hit_labels = _labels_for_hits(person, hits)
            cs.append({
                "code": c.get("code", ""),
                "name": c.get("name", ""),
                "diff": c.get("diff", 0),
                "is_default": bool(c.get("is_default")),
                "conflict": bool(hit_labels),
                "conflict_with": hit_labels,
            })
        out.append({
            "zone": zone_label(rnd, i),
            "index": i,
            "min": rnd.get("min") or 0,
            # max 缺失保留 None（区别于显式 0 = 不可选区）
            "max": rnd.get("max"),
            "choices": cs,
        })
    return out


def _labels_for_hits(person: Person, hits: List[str]) -> List[str]:
    """把命中的关键词反查成可读的忌口标签（如 '牛' → '不吃牛肉'）。"""
    labels = []
    for d in person.diet:
        rule = DIET_RULES.get(d)
        if not rule:
            continue
        if any(k in hits for k in rule["keywords"]):
            labels.append(rule["label"])
    # 去重保序
    seen = set()
    uniq = []
    for lb in labels:
        if lb not in seen:
            seen.add(lb)
            uniq.append(lb)
    return uniq


# ---------------------------------------------------------------------------
# 手动自选：校验 + 算价
# ---------------------------------------------------------------------------
def validate_selection(meal: Meal, codes: List[str],
                       person: Optional[Person] = None) -> Dict:
    """校验用户手动选择的一组备选（按 code 指定）。

    codes: 每个区选 1 件的 code 列表，顺序与方法返回的区顺序一致；
           也允许传"区名:code"形式（见 parse_pick_spec）。

    返回：
        {
          "ok": bool,                    # 是否能构成一份合法订单
          "picks": [{zone, code, name, diff}],
          "price": int,                  # 最终价（分）= 基准价 + 补差价
          "base_price": int,
          "diff_total": int,
          "conflicts": [ {zone, name, with:[忌口标签]} ],   # 与忌口冲突的项
          "suggestions": [ {zone, name, diff, reason} ],    # 冲突项的合规替代
          "errors": [str],
        }

    注意：**冲突不阻止下单**（用户有最终选择权），只如实提示 + 给替代；
          errors 只用于"选不满/选错区"这类结构性问题。
    """
    rounds = meal.combo_rounds or []
    result = {
        "ok": True, "picks": [], "price": meal.price,
        "base_price": meal.price, "diff_total": 0,
        "conflicts": [], "suggestions": [], "errors": [],
    }
    if not rounds:
        result["ok"] = False
        result["errors"].append("该餐品不是可选组合（无可换件结构）。")
        return result

    # 按区逐个解析用户选择
    # ⚠️ AGY 复审2 严重#1：原实现用**全局**扁平字典 by_code + setdefault 把每个
    #    code 永久绑定到「第一个遇到的分区」。真实套餐中多个分区常提供同一商品
    #    （如「全明星双人分享餐八件套」两个"选择小食"区都含 code=1700），
    #    导致所有重复 code 都被算进第一区 → 第一区爆上限、第二区报漏选，
    #    连套餐自身的默认配置都校验不过。
    #    改为「按区消耗式匹配」：每个 code 只在本区取用一次，取用后从待选池移除。

    forbidden = person.forbidden_keywords() if person else set()
    diff_total = 0
    picks = []
    # ⚠️ AGY 复审2 严重#1：真实套餐里同一 code 可能出现在多个区（如「全明星双人
    #    分享餐八件套」的 1700 既是某区默认项、又是另一区的备选）。若按「遍历顺序」
    #    消耗 code，前面的区会抢走后区唯一可用的默认项，导致合法配置也报错。
    #    正确做法：**先按「默认项」逐区认领**（每个默认项归属它所在的区），
    #    再用剩余 code 补足非默认选择，避免跨区抢占。
    remaining = [c for c in codes]
    assigned = [[] for _ in rounds]         # 每区已认领的 code 列表
    zone_default_codes = []
    for rnd in rounds:
        defaults = [c.get("code", "") for c in (rnd.get("choices") or [])
                    if c.get("is_default")]
        zone_default_codes.append(defaults)

    def _zone_codes(rnd):
        return {c.get("code", "") for c in (rnd.get("choices") or [])}

    def _need(rnd):
        # max 显式为 0 → 该区不允许选件，认领/补足阶段都跳过
        if rnd.get("max") == 0:
            return 0
        return rnd.get("min") or 0

    # 第一轮：每个区先认领自己的默认项（可重复，代表 quantity>1 的份数）。
    #         但「认领数量」不得超过本区应选件数，避免某区把后区的同名项吃光。
    for i, rnd in enumerate(rounds):
        need = _need(rnd)
        if need <= 0:
            continue
        got = []
        for code in zone_default_codes[i]:
            if len(got) >= need:
                break
            if code in remaining:
                remaining.remove(code)
                got.append(code)
        assigned[i] = got

    # 第二轮：用剩余 code 依次补足各区，只补本区仍需的件数
    for i, rnd in enumerate(rounds):
        need = _need(rnd)
        if need <= 0:
            continue
        have = len(assigned[i])
        if have >= need:
            continue
        zc = _zone_codes(rnd)
        for code in list(remaining):
            if have >= need:
                break
            if code in zc:
                remaining.remove(code)
                assigned[i].append(code)
                have += 1

    for i, rnd in enumerate(rounds):
        need = rnd.get("min") or 0
        # 该区应选的件数（min；min=0 视为可选，默认挑 1 件以展示）
        take = need if need > 0 else 0
        zone_name = zone_label(rnd, i)
        _max = rnd.get("max")

        # ⚠️ Codex 复审2 中等#4：max 显式为 0 表示「该区不可选任何件」。
        #    若用户仍为本区提供了 code，必须报错（不能静默忽略）。
        #    注意：认领阶段已不向该区分配任何 code，故此处只需判断
        #    「用户原始输入里是否包含本区备选」。
        if _max == 0:
            _zc0 = {c.get("code", "") for c in (rnd.get("choices") or [])}
            _attempted = [c for c in codes if (c or "").strip() in _zc0]
            if _attempted:
                result["ok"] = False
                result["errors"].append(
                    "「%s」为不可选区（上限 0 件），不应选择任何备选。" % zone_name)
            continue

        # 本区认领到的 code 允许重复（quantity>1），但同一 code 至多按 need 份
        chosen = _resolve_codes_for_round(assigned[i], rnd)
        if not chosen:
            if take > 0:
                result["ok"] = False
                result["errors"].append("「%s」未选择（该区必选）。" % zone_name)
            continue
        if take > 0 and len(chosen) < take:
            result["ok"] = False
            result["errors"].append(
                "「%s」需选 %d 件，实际 %d 件。" % (zone_name, take, len(chosen)))
        if isinstance(_max, int) and len(chosen) > _max:
            result["ok"] = False
            result["errors"].append(
                "「%s」最多选 %d 件，实际 %d 件。"
                % (zone_name, _max, len(chosen)))

        for c in chosen:
            # ⚠️ Codex 复审3 中等#2：备选可能一次带 2/3 份（choice.quantity），
            #    补差价应按份数乘算，否则手选价格会少算（与多人路径语义不一致）。
            #    与 feast_solver.resolve_rounds 内部 `unit_diff * cq` 保持同一口径。
            unit_diff = c.get("diff", 0) or 0
            cq = c.get("quantity") or 1
            if cq < 1:
                cq = 1
            diff = unit_diff * cq
            diff_total += diff
            picks.append({
                "zone": zone_name, "code": c.get("code", ""),
                "name": c.get("name", ""), "diff": diff,
                "unit_diff": unit_diff, "quantity": cq,
            })
            # 忌口冲突检查
            if forbidden:
                hits = [k for k in forbidden if k in (c.get("name") or "")]
                if hits:
                    result["conflicts"].append({
                        "zone": zone_name, "name": c.get("name", ""),
                        "with": _labels_for_hits(person, hits),
                    })
                    # 给出该区的合规替代（同区里不冲突且最省的）
                    alt = _best_alternative(rnd, forbidden)
                    if alt:
                        result["suggestions"].append({
                            "zone": zone_name,
                            "name": alt.get("name", ""),
                            "code": alt.get("code", ""),
                            "diff": alt.get("diff", 0) or 0,
                            "reason": "该区合规且补差价最小",
                        })
                    else:
                        result["suggestions"].append({
                            "zone": zone_name, "name": "(该区无合规备选)",
                            "code": "", "diff": 0,
                            "reason": "本区所有备选都命中忌口，建议整单换套餐",
                        })

    result["picks"] = picks
    result["diff_total"] = diff_total
    result["price"] = meal.price + diff_total
    return result


def _resolve_codes_for_round(codes: List[str], rnd: Dict) -> List[Dict]:
    """从用户给的 code 列表里，挑出属于本区的那些（返回对应的 choice dict）。

    说明：只按「本区自己的备选表」匹配，不再依赖跨区的全局 code 字典 ——
    真实套餐里多个区可能提供同一商品，全局字典会把它们错误地并到首区
    （AGY 复审2 严重#1）。

    去重规则（Codex 复审3 中等#3 + AGY 复审2 严重#1 的共同结论）：
      · 同一 code 重复出现通常代表「同一备选选多份」（如 min=max=2 的区
        传两次同一 code）→ **允许重复**，但最多到本区应选件数 need。
      · 超出 need 的重复视为无效输入，丢弃，避免伪造「选满」。
    """
    zone_choices = {}
    for c in (rnd.get("choices") or []):
        zone_choices.setdefault(c.get("code", ""), c)
    need = rnd.get("min") or 0
    cap = need if need > 0 else 1
    out = []
    counts = {}
    for code in codes:
        code = (code or "").strip()
        if not code:
            continue
        c2 = zone_choices.get(code)
        if c2 is None:
            continue
        if counts.get(code, 0) >= cap:
            continue
        counts[code] = counts.get(code, 0) + 1
        out.append(c2)
    return out


def _best_alternative(rnd: Dict, forbidden) -> Optional[Dict]:
    """在本区内找合规且补差价最小的备选（用于给冲突项提替代建议）。"""
    best = None
    for c in (rnd.get("choices") or []):
        if _choice_hits(c, forbidden):
            continue
        if best is None or (c.get("diff", 0) or 0) < (best.get("diff", 0) or 0):
            best = c
    return best


def parse_pick_spec(spec: str) -> List[str]:
    """解析用户手选输入。

    支持两种写法（可混用逗号分隔）：
        "1120,3050"            → 按顺序给各区挑 code
        "蓝区=521156,粉区=4437" → 按区名指定（更直观，推荐）
        也接受中文等号"＝"和冒号"："。
    返回 code 列表（供 validate_selection 使用）。
    """
    if not spec:
        return []
    spec = spec.replace("：", ":").replace("＝", "=")
    parts = [p.strip() for p in re.split(r"[,，;；]", spec) if p.strip()]
    codes = []
    for p in parts:
        if "=" in p:
            _zone, code = p.split("=", 1)
            codes.append(code.strip())
        else:
            codes.append(p)
    return codes


# ---------------------------------------------------------------------------
# 自动推荐（单人）
# ---------------------------------------------------------------------------
def recommend_solo(meals: List[Meal], person: Person,
                   budget_per_person: Optional[int] = None,
                   prefer_one_plus_one: bool = True,
                   limit: int = 5) -> List[Dict]:
    """为单人推荐多套方案（优先 1+1，其次其他可选组合与单品套餐）。

    返回按「是否合规 → 价格」排序的方案列表，每项：
        {"meal": Meal, "name":.., "price":int, "diff_total":int,
         "picks":[...], "safe":bool, "conflicts":[...], "calories":..}

    prefer_one_plus_one=True 时，1+1 形态排在最前（用户点名要的玩法）。
    """
    forbidden = person.forbidden_keywords()
    # ⚠️ Codex 复审3 中等#4：单人推荐原先未接入 Person.preference，
    #    且无忌口时直接跳过换件 —— 于是 `--solo --me "我:prefer=咖啡"` 既不会
    #    按偏好选咖啡，展示的 picks 也是空的。这里与多人路径对齐：
    #    只要有忌口**或**有偏好，就解析换件（无约束时 resolve_rounds 自然选回默认项）。
    prefer = _prefer_keywords(person) or []
    budget_cents = budget_per_person * 100 if budget_per_person else None
    out = []

    for m in meals:
        if not m.combo_rounds:
            continue
        # 换件解析（决定合规性、实际价格、实际营养与偏好选法）
        res = m.resolve_rounds(forbidden, prefer_keywords=prefer)
        if forbidden and not res["ok"]:
            # 换不掉 → 跳过（推荐场景只给能吃到的）
            continue
        eff_price = m.price + (res["diff_total"] if res.get("ok") else 0)
        if budget_cents is not None and eff_price > budget_cents:
            continue

        # ⚠️ Codex 复审2 严重#2：必须用**换件后的实际营养**判断约束，
        #    不能用套餐默认值，也不能把缺失营养当成 0。
        #    旧实现 `(m.calories or 0)` 会让「默认 400 千卡、实际换到 800 千卡」
        #    的套餐通过 max_calories=600 的检查，并展示错误的默认营养。
        have_nut_constraint = (person.max_calories is not None
                               or person.min_protein is not None)
        eff_nut = res.get("nutrition")
        if eff_nut:
            eff_cal = eff_nut.get("calories")
            eff_prot = eff_nut.get("protein")
        elif forbidden:
            # 有换件但营养数据不全 → 无法验证，按缺失处理（不得当作 0）
            eff_cal, eff_prot = None, None
        else:
            # 无换件 → 套餐默认组成即实际组成
            eff_cal, eff_prot = m.calories, m.protein

        if have_nut_constraint and (eff_cal is None or eff_prot is None):
            # 设有营养约束却拿不到实际营养 → 拒绝（不谎称合规）
            continue
        if person.max_calories is not None and eff_cal > person.max_calories:
            continue
        if person.min_protein is not None and eff_prot < person.min_protein:
            continue

        picks = []
        for p in res.get("picks", []):
            if p.get("dropped"):
                continue
            picks.append({"zone": p.get("round", ""), "name": p.get("name", ""),
                          "code": p.get("code", ""), "diff": p.get("diff", 0)})
        out.append({
            "meal": m,
            "code": m.code,
            "name": m.name,
            "price": eff_price,
            "base_price": m.price,
            "diff_total": res.get("diff_total", 0),
            "picks": picks,
            "swaps": res.get("swaps", []),
            "safe": True,
            # 展示的也必须是**实际**营养，而非套餐默认值
            "calories": eff_cal,
            "protein": eff_prot,
            "is_one_plus_one": is_one_plus_one(m),
        })

    # 排序：1+1 优先 → 价格升序 → 热量升序
    out.sort(key=lambda x: (
        0 if (prefer_one_plus_one and x["is_one_plus_one"]) else 1,
        x["price"],
        x["calories"] or 0,
    ))
    return out[:limit]


def find_one_plus_one(meals: List[Meal]) -> List[Meal]:
    """筛出菜单里所有 1+1 形态的套餐（可能有多款：经典/超值/早餐）。"""
    return [m for m in meals if is_one_plus_one(m)]
