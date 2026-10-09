#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feast_engine.py —— 多约束聚餐求解引擎

策略：
  1. 先为每个人筛出「本人可吃」的餐品（忌口过滤 + 个人约束）
  2. 为每个人生成若干候选「套餐组合」（主食 + 小食 + 饮料 + 甜品，结构完整）
  3. 在人均预算约束下，对多人候选做笛卡尔积枚举，按不同目标函数择优
  4. 输出多个方案：性价比最优 / 健康最优 / 豪华版

复杂度控制：枚举前对每人候选做 Top-K 截断，保证组合数可控（与限流无关，
纯本地计算）。
"""

from itertools import product as cartesian
from typing import List, Dict, Optional, Tuple

from feast_solver import (
    Meal, Person, FeastRequest, Assignment, Plan, SolveResult,
    CATEGORY_MAIN, CATEGORY_SIDE, CATEGORY_DRINK, CATEGORY_DESSERT,
    UNLIMITED,
)

# 每人候选套餐数量上限（防止组合爆炸）
CANDIDATES_PER_PERSON = 24


# ---------------------------------------------------------------------------
# 第一步：忌口过滤（含「换件救单」）
# ---------------------------------------------------------------------------
def _prefer_keywords(person: Person) -> Optional[List[str]]:
    """把 Person.preference 转成换件时用的偏好关键词列表。

    ⚠️ AGY 审查：preference 字段此前从未接线，是死代码。
       现贯通到 resolve_rounds(prefer_keywords=...)：同价时优先选中口味偏好的备选。
    """
    pref = (person.preference or "").strip()
    if not pref:
        return None
    # 支持 "咖啡,拿铁" 多关键词
    kws = [k.strip() for k in pref.replace("，", ",").split(",") if k.strip()]
    return kws or None


def filter_meals_for_person(meals: List[Meal], person: Person) -> List[Meal]:
    """筛掉该人不能吃的餐品 —— 但只要还能通过**换件**救回来，就不排除。

    判定顺序（**顺序本身就是设计**）：

      1) 有可换件结构（combo_rounds 非空）
         → 用 resolve_rounds() 尝试为每个必选区换到合规项。
           能换到 → **保留**（具体换成什么在求解阶段按人计算）
           换不到 → 排除（内含违禁且无法替换）
         ⚠️ 必须先判换件：套餐名/默认项常写着默认主料
            （如 1+1 默认"双层吉士汉堡"是牛肉），先判名称会把可救的套餐误杀。

      2) 名称/标签/默认子项命中忌口词 → 排除（无换件空间，不可救）

      3) 疑似套餐但组成未知（combo_items 为空）→ 保守排除
         （无从得知它含什么，不能赌）

      4) 单品 → 保留

    这一步只做「能不能吃」的可行性判断；具体换成什么、补差价多少，
    在 build_person_candidates / solve 阶段按人计算。
    """
    forbidden = person.forbidden_keywords()
    if not forbidden:
        return list(meals)

    prefer = _prefer_keywords(person)
    out = []
    for m in meals:
        # 判定 1：有可换件结构 → 以「换件后」的选法为准
        #
        # ⚠️ 顺序很关键：必须先判换件，再判名称。
        #    因为套餐名/默认组成往往写着默认主料（如 1+1 默认"双层吉士汉堡"是牛肉），
        #    若不先换件，就会被名称命中直接排除，换件能力永远用不上。
        if m.combo_rounds:
            if m.resolve_rounds(forbidden, prefer_keywords=prefer)["ok"]:
                out.append(m)          # 换件可救，保留
            continue                    # 换不掉 → 排除
        # 判定 2：名称/标签/默认子项命中忌口词 → 排除
        if m.matches_any_keyword(forbidden):
            continue
        # 判定 3：疑似套餐但组成未知 → 保守排除
        if m.is_combo_like and not m.combo_items:
            continue
        out.append(m)
    return out


def build_person_swap(m: Meal, person: Person) -> Dict:
    """为某人计算某套餐的换件方案（单品返回空方案）。"""
    if not m.combo_rounds:
        return {"ok": True, "picks": [], "diff_total": 0, "swaps": [],
                "unresolvable": [], "nutrition": None}
    return m.resolve_rounds(person.forbidden_keywords(),
                            prefer_keywords=_prefer_keywords(person))


# ---------------------------------------------------------------------------
# 第二步：为单人生成候选套餐组合
# ---------------------------------------------------------------------------
def build_person_candidates(meals: List[Meal], person: Person,
                            budget_cents: int,
                            limit: int = CANDIDATES_PER_PERSON) -> List[Tuple[Meal, ...]]:
    """为单人枚举「结构完整」的套餐组合。

    结构定义：1 份主食 + 可选 1 份小食 + 可选 1 份饮料 + 可选 1 份甜品。
    这样每个组合都像一顿「正经饭」，而不是随机堆砌。

    ⚠️ 价格考量：套餐若发生换件，实际价格 = 基准价 + 换件补差价。
       因此预算判断必须用**换件后**的有效单价，而不是标签价，
       否则会出现"以为 ¥14.9 实际 ¥13.9"或"以为能买其实超预算"的偏差。

    返回：按 (比预算省得多, 热量适中) 排序后的前 limit 个组合。
    """
    mains = [m for m in meals if m.category == CATEGORY_MAIN]
    sides = [m for m in meals if m.category == CATEGORY_SIDE]
    drinks = [m for m in meals if m.category == CATEGORY_DRINK]
    desserts = [m for m in meals if m.category == CATEGORY_DESSERT]

    if not mains:
        return []

    # 预先算好每个餐品对「这个人」的有效单价（含换件补差价）与**有效营养**
    #
    # ⚠️ Codex 审查发现：换件后仍沿用套餐默认营养，会让个人热量/蛋白约束失真
    #    （默认低卡 → 换到高卡却被判合规）。故此处预解析每人的换件方案，
    #    并用换件后的实测营养参与约束判断。
    forbidden = person.forbidden_keywords()
    prefer = _prefer_keywords(person)
    eff_price: Dict[str, int] = {}
    eff_nut: Dict[str, Optional[Dict]] = {}
    for m in meals:
        if m.combo_rounds:
            res = m.resolve_rounds(forbidden, prefer_keywords=prefer)
            eff_price[m.code] = m.price + (res["diff_total"] if res["ok"] else 0)
            nut = res.get("nutrition")
            # ⚠️ AGY 复审2 严重#2 的兜底：若备选营养缺失，但**未发生换件**
            #    （仍按默认组成）且套餐本身有整体营养，则复用整体营养，
            #    避免把「套餐有营养、只是备选表没落数据」误判为不可验证，
            #    进而使控卡/增肌用户完全选不到任何换件套餐。
            if nut is None and not res.get("swaps") and m.calories is not None:
                nut = {"calories": m.calories, "protein": m.protein}
            eff_nut[m.code] = nut
        else:
            eff_price[m.code] = m.price
            eff_nut[m.code] = ({"calories": m.calories, "protein": m.protein}
                               if m.calories is not None else None)

    def _nut_of(m: Meal) -> Optional[Dict]:
        """该餐品对本人的有效营养；None 表示数据缺失（不可验证）。"""
        return eff_nut.get(m.code)

    # 小食/饮品/甜品分别加上「不选」选项（None 表示不加）
    side_opts: List[Optional[Meal]] = [None] + sides
    drink_opts: List[Optional[Meal]] = [None] + drinks
    dessert_opts: List[Optional[Meal]] = [None] + desserts

    combos: List[Tuple[Meal, ...]] = []
    for main, side, drink, dessert in cartesian(mains, side_opts, drink_opts, dessert_opts):
        combo = tuple(x for x in (main, side, drink, dessert) if x is not None)
        cost = sum(eff_price.get(m.code, m.price) for m in combo)
        if cost > budget_cents:
            continue

        # 个人热量上限
        #
        # ⚠️ Codex 审查：营养缺失不能当 0 千卡（会把「无数据」误判为「低卡」）。
        #    对设了 upper 约束的人，缺数据 = 无法验证 → 拒绝该候选。
        if person.max_calories is not None:
            n_missing = any(_nut_of(m) is None for m in combo)
            if n_missing:
                continue
            cal = sum((_nut_of(m) or {}).get("calories") or 0 for m in combo)
            if cal > person.max_calories:
                continue
        if person.min_protein is not None:
            if any(_nut_of(m) is None for m in combo):
                continue
            prot = sum((_nut_of(m) or {}).get("protein") or 0 for m in combo)
            if prot < person.min_protein:
                continue

        combos.append(combo)

    if not combos:
        # 退一步：只给主食（可能预算很紧）
        #
        # ⚠️ Codex 审查【严重】：旧回退只检查价格，完全绕过了 max_calories /
        #    min_protein，会产出违反个人营养约束的方案。
        #    现改为**复用同一套个人约束**；若无合规主食则返回空，由上层报不可行。
        fallback = []
        for m in mains:
            if eff_price.get(m.code, m.price) > budget_cents:
                continue
            nut = _nut_of(m)
            if person.max_calories is not None:
                if nut is None or (nut.get("calories") or 0) > person.max_calories:
                    continue
            if person.min_protein is not None:
                if nut is None or (nut.get("protein") or 0) < person.min_protein:
                    continue
            fallback.append((m,))
        combos = fallback

    # 排序 + **多路保留**（而不是单纯按价格截断）
    #
    # ⚠️ Codex 审查【中等】：旧实现只按价格升序取前 limit，导致"健康版/豪华版"
    #    其实只在"最便宜的那批"里挑，高价的低热量餐或更丰盛的餐根本没进候选，
    #    "最优"名不副实。现按多个维度各保留一批，再按价格补足：
    #      · 低价路：价格升序（性价比）
    #      · 低卡路：热量升序（健康）
    #      · 高蛋白路：蛋白降序（增肌）
    #      · 高规格路：价格降序（豪华，预算内最贵）
    def _price(c):
        return sum(eff_price.get(m.code, m.price) for m in c)

    def _cal(c):
        return sum((_nut_of(m) or {}).get("calories") or 0 for m in c)

    def _prot(c):
        return sum((_nut_of(m) or {}).get("protein") or 0 for m in c)

    # ⚠️ AGY 复审2 中等#1：营养缺失的餐品 `_cal` 会算成 0 千卡，于是含缺数据餐品的
    #    组合在「低卡路」排序里抢占最前排，把真正低热量的组合挤出候选；
    #    而它们随后又会被健康策略的 `_all_have_nutrition` 过滤掉，导致健康方案劣化。
    #    修法：低卡/高蛋白两路排序对「营养不全」的组合降权（移到末尾），
    #    而不是让它们凭 0 值冒领前排。
    def _nut_complete(c):
        return all(_nut_of(m) is not None for m in c)

    def _cal_key(c):
        # 营养不全 → 排到最后，仅用价格作次序
        return (0, _cal(c), _price(c)) if _nut_complete(c) else (1, _price(c), 0)

    def _prot_key(c):
        return ((-_prot(c), _price(c)) if _nut_complete(c) else (0, _price(c)))

    combos.sort(key=lambda c: (_price(c), _cal(c)))
    lanes = [
        sorted(combos, key=lambda c: (_price(c), _cal(c))),        # 低价
        sorted(combos, key=_cal_key),                              # 低卡（营养完整者优先）
        sorted(combos, key=_prot_key),                             # 高蛋白（同上）
        sorted(combos, key=lambda c: (-_price(c), _cal(c))),       # 豪华
    ]
    quota = max(limit // len(lanes), 1)
    picked: List[Tuple[Meal, ...]] = []
    seen_sig = set()
    for lane in lanes:
        taken = 0
        for c in lane:
            if taken >= quota:
                break
            sig = tuple(m.code for m in c)
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            picked.append(c)
            taken += 1
    # 价格补足到 limit
    for c in combos:
        if len(picked) >= limit:
            break
        sig = tuple(m.code for m in c)
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        picked.append(c)
    return picked[:limit]


# ---------------------------------------------------------------------------
# 第三步：多人组合搜索
# ---------------------------------------------------------------------------
def _plan_metrics(assignments: List[Assignment]) -> Tuple[int, int, float, float]:
    """返回 (总价分, 总热量, 平均蛋白, 人均钠)。"""
    cost = sum(a.cost for a in assignments)
    cal = sum(a.calories for a in assignments)
    prot = sum(a.protein for a in assignments)
    # 走 Assignment.sodium，与热量/蛋白一致地尊重换件覆盖（AGY 复审2 轻微#1）
    sodium = sum(a.sodium for a in assignments)
    n = max(len(assignments), 1)
    return cost, cal, prot / n, sodium / n


def _all_have_nutrition(assignments: List[Assignment]) -> bool:
    """该组合里的每份餐品营养是否**都可验证**。

    用于「健康策略」的候选筛选：避免把「营养数据缺失」误当成「零热量」。
    ⚠️ Codex 审查：换件后的营养也必须是实测值，否则同样不可信。
    """
    return all(a.nutrition_verified for a in assignments)


def solve(req: FeastRequest, meals: List[Meal],
          max_total_plans: int = 3) -> SolveResult:
    """求解聚餐方案。

    返回多个方案（性价比 / 健康 / 豪华），每个方案含：
      - 每人吃什么
      - 总价、人均、总热量
      - AA 精确分摊表（保证分毫不差）
    """
    result = SolveResult()
    people = req.people
    if not people:
        result.infeasible_reason = "没有参与者，无法规划。"
        return result

    headcount = req.headcount
    # 个人预算（分）：给单人留出的预算空间
    per_person_budget_cents = (
        req.budget_per_person * 100 if req.budget_per_person else UNLIMITED
    )

    # ---- 为每个人准备候选 ----
    per_person_candidates: List[List[Tuple[Meal, ...]]] = []
    for p in people:
        allowed = filter_meals_for_person(meals, p)
        cands = build_person_candidates(allowed, p, per_person_budget_cents)
        if not cands:
            labels = "、".join(p.diet_labels()) or "未知限制"
            result.infeasible_reason = (
                "「%s」在当前菜单+预算下无可用餐品（限制：%s）。"
                "建议放宽预算或减少忌口。" % (p.name, labels)
            )
            return result
        per_person_candidates.append(cands)

    # ---- 若组数过大，按价格做上限截断 ----
    #
    # ⚠️ Codex 审查【严重】：旧实现用 `c[:max(4, len(c)*3//4)]` 收缩，
    #    当每人候选降到 4 后**下界卡死在 4**，10 人时 4^10 = 1,048,576 > 600,000，
    #    循环条件永久成立 → **死循环**（已实测复现：15 秒不返回）。
    #    修法：保证 cap 严格下降 —— 下界降到 2，并在极端情况直接截到 1，
    #    同时设最大收缩轮数兜底，绝不允许无限循环。
    MAX_COMBOS = 600000
    cap = 1
    for c in per_person_candidates:
        cap *= len(c)
    shrink_guard = 0
    while cap > MAX_COMBOS and shrink_guard < 64:
        shrink_guard += 1
        new_list = []
        changed = False
        for c in per_person_candidates:
            n = len(c)
            # 严格下降：每次至少缩 1，且下界逐步从 4 降到 2、再到 1
            floor = 1 if shrink_guard > 32 else 2
            target = max(floor, min(n - 1, n * 3 // 4))
            if target < n:
                changed = True
            new_list.append(c[:target] if target < n else c)
        per_person_candidates = new_list
        cap = 1
        for c in per_person_candidates:
            cap *= len(c)
        if not changed:
            break
    if cap > MAX_COMBOS:
        # 最后手段：每人只留最优 1 个候选（组合数 = 1），保证有界
        result.warnings.append(
            "参与人数较多，候选空间过大（%d 种），已收敛为每人 1 个最优候选。" % cap)
        per_person_candidates = [c[:1] for c in per_person_candidates]

    # ---- 枚举所有组合，收集可行解 ----
    feasible: List[Tuple[int, int, float, List[Assignment]]] = []
    total_budget_cents = req.total_budget
    scanned = 0

    # ⚠️ AGY 审查：resolve_rounds 在笛卡尔积里被重复调用数十万次是纯浪费。
    #    参与者与餐品集固定 → 结果完全可缓存。
    # ⚠️ Codex 复审2 严重#1：缓存键原为 (person.name, m.code)，**不可**用显示姓名
    #    当身份键 —— 同名参与者若忌口/偏好不同，第二人会复用第一人的换件结果，
    #    可能被分配违禁餐品且计错价与营养。改用「完整决策输入」作键。
    _swap_cache: Dict[Tuple, Dict] = {}

    def _cache_key(person: Person, m: Meal) -> Tuple:
        return (m.code,
                tuple(sorted(person.forbidden_keywords())),
                tuple(_prefer_keywords(person) or ()))

    def _cached_resolve(person: Person, m: Meal) -> Dict:
        k = _cache_key(person, m)
        if k not in _swap_cache:
            _swap_cache[k] = m.resolve_rounds(
                person.forbidden_keywords(),
                prefer_keywords=_prefer_keywords(person))
        return _swap_cache[k]

    for combo in cartesian(*per_person_candidates):
        scanned += 1
        assignments = []
        for person, items in zip(people, combo):
            # 合并同名餐品
            merged: Dict[str, Tuple[Meal, int]] = {}
            for m in items:
                if m.code in merged:
                    old_m, q = merged[m.code]
                    merged[m.code] = (old_m, q + 1)
                else:
                    merged[m.code] = (m, 1)

            # 换件：按此人忌口解析每份套餐的换件方案，累计补差价
            forbidden = person.forbidden_keywords()
            prefer = _prefer_keywords(person) or []
            swaps: List[Dict] = []
            resolved: Dict[str, List[Dict]] = {}
            nut_override: Dict[str, Dict] = {}
            swap_diff = 0
            # ⚠️ Codex 复审3 严重#1：候选评估（build_person_candidates）对**所有**
            #    套餐都用换件后的价格/营养，但此处原先只在「有忌口或有偏好」时才
            #    应用换件 —— 于是仅设 maxcal/protein 的用户，会以「换件后的低价、
            #    低卡组合」通过候选筛选，最终却按默认价与默认营养进入方案，
            #    两条路径口径不一致，可能绕过预算或营养约束。
            #    修法：只要套餐有选择区，就统一解析并应用换件（结果已缓存，开销可控）；
            #    无约束时 resolve_rounds 自然选回默认项、diff=0，不改变原行为。
            for m, q in merged.values():
                if not m.combo_rounds:
                    continue
                res = _cached_resolve(person, m)
                if not res["ok"]:
                    swap_diff = None     # 标记为不可用
                    break
                swap_diff += res["diff_total"] * q
                # 记录实际选法（供展示「换成了什么」）
                resolved[m.code] = [
                    {"round": p["round"], "name": p["name"],
                     "dropped": p.get("dropped", False)}
                    for p in res["picks"]
                ]
                # 换件后实测营养覆盖（None 表示某些件缺数据 → 不可验证）
                if res.get("nutrition"):
                    nut_override[m.code] = res["nutrition"]
                for s in res["swaps"]:
                    swaps.append(dict(s, combo=m.name, qty=q))
                # 名称与实际内容矛盾时附澄清说明（如「牛肉堡单品精选」选到汉堡包）
                iso = m.name_swap_note(forbidden)
                if iso:
                    swaps.append({"round": "说明", "frm": "", "to": "",
                                  "diff": 0, "combo": m.name, "note": iso})
            if swap_diff is None:
                break            # 此人这份套餐换不掉，整个组合不可行

            assignments.append(Assignment(
                person=person.name, items=list(merged.values()),
                swaps=swaps, swap_diff=swap_diff or 0, resolved=resolved,
                nut_override=nut_override,
            ))
        else:
            cost, cal, prot, sodium = _plan_metrics(assignments)
            if total_budget_cents is not None and cost > total_budget_cents:
                continue
            feasible.append((cost, cal, prot, assignments))

    if not feasible:
        result.infeasible_reason = (
            "在人均 %s 元预算内无法为 %d 人配出符合忌口的方案。"
            "建议提高预算或减少忌口限制。"
            % (req.budget_per_person, headcount)
        )
        return result

    result.warnings.append("已扫描 %d 个候选组合，筛出 %d 个可行解。" % (scanned, len(feasible)))

    # ---- 三种策略择优 ----
    picked: List[Tuple[str, str, Tuple]] = []

    # 策略 1：性价比最优（总价最低）
    best_value = min(feasible, key=lambda x: (x[0], x[1]))
    picked.append(("性价比最优", "value", best_value))

    # 策略 2：健康最优（人均热量最低且蛋白优先）
    #
    # ⚠️ 重要：营养数据缺失的餐品 calories 为 None，会被当成 0 千卡，
    #    从而在「求最小热量」时被误选 —— 这是错误的「伪健康」。
    #    因此健康策略**只在全部餐品都有营养数据的组合中挑选**。
    health_candidates = [
        x for x in feasible
        if _all_have_nutrition(x[3])
    ]

    def health_key(x):
        cost, cal, prot, _ = x
        avg_cal = cal / headcount
        return (avg_cal, -prot, cost)

    if health_candidates:
        best_health = min(health_candidates, key=health_key)
        label = "轻负担健康版"
    else:
        best_health = min(feasible, key=health_key)
        label = "轻负担版（部分餐品缺营养数据）"
    picked.append((label, "healthy", best_health))

    # 策略 3：豪华版（在预算内热量/规格最高，即吃得最丰盛）
    if total_budget_cents is not None:
        def feast_key(x):
            cost, cal, prot, _ = x
            # 预算利用率优先，其次总热量
            return (-cost, -cal)
        best_feast = min(feasible, key=feast_key)
    else:
        def feast_key(x):
            cost, cal, prot, _ = x
            return (-cal, -cost)
        best_feast = min(feasible, key=feast_key)
    picked.append(("丰盛豪华版", "feast", best_feast))

    # ---- 去重（不同策略可能选中同一组合）----
    seen = set()
    plans: List[Plan] = []
    for title, strategy, (cost, cal, prot, assignments) in picked:
        # 去重签名：含餐品 code+份数，以及换件后实际选的 code（否则
        # 两个餐品相同但换件不同的方案会被误判为重复）
        sig = tuple(sorted(
            (a.person,
             tuple(sorted((m.code, q) for m, q in a.items)),
             tuple(sorted((s.get("round", ""), s.get("to", ""), s.get("diff", 0))
                          for s in a.swaps)))
            for a in assignments))
        if sig in seen:
            continue
        seen.add(sig)

        plan = Plan(title=title, strategy=strategy,
                    assignments=assignments, total_cost=cost)
        plan.notes = _explain(plan, req, prot)
        plans.append(plan)

    # 按总价升序展示
    plans.sort(key=lambda p: p.total_cost)
    result.plans = plans[:max_total_plans]
    return result


def _explain(plan: Plan, req: FeastRequest, avg_protein: float) -> List[str]:
    """为方案生成可解释的说明文字。"""
    notes = []
    n = plan.headcount
    notes.append("共 %d 人，总计 ¥%.2f，人均 ¥%.2f"
                 % (n, plan.total_cost / 100, plan.per_person_cost / 100))
    notes.append("人均热量约 %d 千卡，人均蛋白约 %.1f 克"
                 % (plan.per_person_calories, avg_protein))

    if req.budget_per_person:
        limit = req.budget_per_person * 100 * n
        used = plan.total_cost / limit * 100 if limit else 0
        notes.append("预算利用率 %.0f%%（人均上限 ¥%d）"
                     % (used, req.budget_per_person))

    # 提示忌口处理情况
    constrained = [p for p in req.people if p.diet]
    if constrained:
        detail = "；".join("%s(%s)" % (p.name, "、".join(p.diet_labels()))
                          for p in constrained)
        notes.append("已按忌口过滤：%s" % detail)

    # 换件说明：把「本来会被排除的套餐靠换件救回来」这件事讲清楚
    # 只强调「为忌口」而换的（性价比优选无需赘述）
    all_swaps = [s for a in plan.assignments for s in a.swaps
                 if not s.get("note") and s.get("reason") == "diet"]
    if all_swaps:
        parts = []
        seen = set()
        for s in all_swaps:
            diff = s.get("diff", 0)
            if diff < 0:
                diff_txt = "省¥%.2f" % (-diff / 100)
            elif diff > 0:
                diff_txt = "加¥%.2f" % (diff / 100)
            else:
                diff_txt = "同价"
            key = (s.get("combo"), s.get("round"), s.get("frm"), s.get("to"))
            if key in seen:
                continue
            seen.add(key)
            parts.append("%s「%s」%s→%s(%s)"
                         % (s.get("round", ""), s.get("combo", ""),
                            s.get("frm", ""), s.get("to", ""), diff_txt))
        notes.append("为满足忌口自动换件：%s" % "；".join(parts))
        total_diff = sum(a.swap_diff for a in plan.assignments)
        if total_diff < 0:
            notes.append("换件后共节省 ¥%.2f" % (-total_diff / 100))
        elif total_diff > 0:
            notes.append("换件后共增加 ¥%.2f" % (total_diff / 100))
    return notes
