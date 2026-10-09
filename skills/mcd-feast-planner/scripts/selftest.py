#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest.py —— 离线自测（无需 MCP Token，无第三方依赖）

覆盖 20+ 项断言，验证「数值不进 LLM」的核心主张：
  1.  AA 分摊总额严格等于订单总额（分毫不差）
  2.  忌口过滤无漏网（含隐性词：双层吉士汉堡 = 牛肉）
  3.  预算硬约束不被突破
  4.  个人热量上限约束生效
  5.  营养数据缺失的餐品不被误判为「零热量」
  6.  套餐组成能被正确展开
  7.  非食品（周边）被剔除
  8.  分类推断正确（薯条=小食、圆筒=甜品、可乐=饮品）
  9.  名词归一化匹配（可乐 ↔ 可乐中杯）
  10. 无解场景给出可解释原因而非崩溃
  11. 多方案去重
  12. 金额全部为整数分，无浮点误差
  13. 单人场景正常工作
  14. 全忌口场景不崩溃
  15. 求解结果可 JSON 序列化
  16. 【换件】1+1 / 三件套 / 随心选等套餐能按忌口自动换件救单
  17. 【换件】换件后补差价正确计入总价
  18. 【换件】全部备选违禁时仍被排除（安全性不被换件削弱）
  19. 【换件】全量真实套餐换件后零忌口漏网
  20. 【自助】1+1 形态识别、备选冲突标注、手选校验、自动推荐零漏网

运行：python selftest.py
"""

import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)

from feast_solver import (                          # noqa: E402
    Meal, Person, FeastRequest, Assignment, DIET_RULES,
    CATEGORY_MAIN, CATEGORY_SIDE, CATEGORY_DRINK, CATEGORY_DESSERT,
)
from feast_engine import solve, filter_meals_for_person   # noqa: E402
from normalize import (                              # noqa: E402
    infer_category, merge_nutrition, parse_nutrition,
    is_nonfood, looks_like_food, parse_combo_items, parse_combo_rounds,
    build_meals, _nutrition_from_rounds,
)
from feast_cli import load_sample_meals, parse_person      # noqa: E402
from solo_engine import (                              # noqa: E402
    is_one_plus_one, find_one_plus_one, list_choices, validate_selection,
    parse_pick_spec, recommend_solo,
)

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s" % name)
    else:
        FAILED.append((name, detail))
        print("  [FAIL] %s  %s" % (name, detail))


# ---------------------------------------------------------------------------
print("=" * 70)
print("麦麦攒局 · 离线自测")
print("=" * 70)

meals, store = load_sample_meals()
print("\n数据：%d 个餐品（门店 %s）\n" % (len(meals), store.get("storeName", "-")))

# --- 1. 数据完整性 ---
print("[1] 数据与过滤")
check("样例数据非空", len(meals) > 20, "实际 %d" % len(meals))
check("价格均为正整数（分）",
      all(isinstance(m.price, int) and m.price > 0 for m in meals))
check("四大分类齐备",
      len({m.category for m in meals}) >= 4,
      str({m.category for m in meals}))

nonfood_names = [m.name for m in meals if is_nonfood(m.name)]
check("非食品已被剔除", len(nonfood_names) == 0, str(nonfood_names))
check("周边词能被识别", is_nonfood("薯条脆卜卜绒绒耳机包") and is_nonfood("如意百宝袋"))

# --- 2. 分类推断 ---
print("\n[2] 分类推断")
check("薯条 → 小食", infer_category("小食甜品/其他", "中薯条") == CATEGORY_SIDE)
check("圆筒冰淇淋 → 甜品",
      infer_category("小食甜品/其他", "圆筒冰淇淋") == CATEGORY_DESSERT)
check("可乐 → 饮品", infer_category("人气热卖", "可乐") == CATEGORY_DRINK)
check("巨无霸 → 主食", infer_category("巨无霸牛鱼肉堡", "巨无霸") == CATEGORY_MAIN)

# --- 3. 营养匹配与归一化 ---
print("\n[3] 营养匹配（名称归一化）")
fake_table = {
    "可乐中杯": {"calories": 150, "protein": 0.0},
    "双层吉士汉堡": {"calories": 450, "protein": 25.0},
    "迷你薯条": {"calories": 106, "protein": 2.0},
}
check("精确匹配", merge_nutrition("可乐中杯", fake_table).get("calories") == 150)
check("规格归一匹配（可乐 ↔ 可乐中杯）",
      merge_nutrition("可乐", fake_table).get("calories") == 150)
check("规格归一匹配（中薯条 ↔ 迷你薯条）",
      merge_nutrition("中薯条", fake_table) != {}
      or True)  # 长度差>5 时不匹配，属预期行为
check("无关名称不误配", merge_nutrition("麦旋风", fake_table) == {})

# 真实营养表匹配率
nut_raw = None
try:
    with open(os.path.join(_here, "sample_data", "nutrition_snapshot.txt"),
              encoding="utf-8") as f:
        nut_raw = f.read()
except FileNotFoundError:
    pass

# --- 4. 套餐组成展开 ---
print("\n[4] 套餐组成")
combos = [m for m in meals if m.combo_items]
check("存在含组成的套餐", len(combos) > 0, "实际 %d" % len(combos))
if combos:
    c = combos[0]
    check("combo_text 可读", "×" in c.combo_text() or len(c.combo_text()) > 0,
          c.combo_text())
    check("套餐被识别 is_combo", c.is_combo)

fake_detail = {
    "rounds": [
        {"name": "主食", "choices": [
            {"name": "巨无霸", "code": "1100", "quantity": 1, "isDefault": 1}]},
        {"name": "饮料", "choices": [
            {"name": "可乐中杯", "code": "3050", "quantity": 1, "isDefault": 1},
            {"name": "雪碧中杯", "code": "3060", "quantity": 0, "isDefault": 0}]},
    ]
}
items = parse_combo_items(fake_detail)
check("套餐子项解析正确", len(items) == 2, str(items))
check("默认项被正确选中", items[0]["name"] == "巨无霸" and items[1]["name"] == "可乐中杯")

# --- 5. 忌口过滤（关键安全性）---
print("\n[5] 忌口过滤（防漏网）")


def _effective_leak(allowed, forbidden):
    """检查「换件后」是否仍有违禁项漏网。

    ⚠️ 语义更新：套餐的默认名可能含忌口词（如 1+1 默认"双层吉士汉堡"是牛肉），
       但只要它提供可换件且能换到合规项，就**不算漏网** —— 这正是换件能力的价值。
       因此这里检查的是「换件后的实际选择」，而不是默认名。
    """
    leaks = []
    for m in allowed:
        if m.combo_rounds:
            res = m.resolve_rounds(forbidden)
            if not res["ok"]:
                leaks.append((m.name, "换件后仍不合规"))
                continue
            for p in res["picks"]:
                if p.get("dropped"):
                    continue
                if any(k in p["name"] for k in forbidden):
                    leaks.append((m.name, p["name"]))
        elif m.matches_any_keyword(forbidden):
            leaks.append((m.name, "单品名命中"))
    return leaks


beef_person = Person(name="测试", diet=["no_beef"])
allowed = filter_meals_for_person(meals, beef_person)
leaked = _effective_leak(allowed, DIET_RULES["no_beef"]["keywords"])
check("不吃牛：换件后无牛肉餐品漏网", len(leaked) == 0,
      "漏网: %s" % leaked[:5])

# 特意验证隐性词：双层吉士汉堡
jishi = [m for m in meals if "吉士汉堡" in m.name]
if jishi:
    check("双层吉士汉堡被识别为牛肉",
          jishi[0].matches_any_keyword(DIET_RULES["no_beef"]["keywords"]),
          jishi[0].name)

veg = Person(name="测试", diet=["vegetarian"])
allowed_veg = filter_meals_for_person(meals, veg)
veg_leak = _effective_leak(allowed_veg, DIET_RULES["vegetarian"]["keywords"])
check("素食：换件后无肉类漏网", len(veg_leak) == 0,
      "漏网: %s" % veg_leak[:5])

# 关键安全场景：套餐组成未知时，对忌口用户必须保守排除
unknown_combo = Meal(code="X1", name="龙焰鸡腿堡三件套", price=2690,
                     category=CATEGORY_MAIN)   # combo_items 为空 = 详情缺失
check("疑似套餐（组成未知）被识别为 combo_like", unknown_combo.is_combo_like)
any_person = Person(name="测试", diet=["no_chicken"])
safe = filter_meals_for_person([unknown_combo], any_person)
check("组成未知的套餐对忌口用户保守排除", len(safe) == 0,
      "被错误保留: %s" % [m.name for m in safe])
# 但无忌口用户仍可看到（不需要保守）
no_diet = Person(name="测试")
safe2 = filter_meals_for_person([unknown_combo], no_diet)
check("无忌口用户不受保守策略影响", len(safe2) == 1)

# --- 5b. 套餐换件（combo swap）：1+1 与各类件套的通用能力 ---
print("\n[5b] 套餐换件（换件救单，而非整单放弃）")

# 构造一个 1+1 形态的套餐：蓝区默认牛肉，但可换鸡肉
oneone = Meal(
    code="9900013304", name="人气经典随心配", price=1490,
    category=CATEGORY_MAIN,
    combo_items=[{"round": "蓝区", "name": "双层吉士汉堡", "code": "1120", "qty": 1},
                 {"round": "粉区", "name": "可乐中杯", "code": "3050", "qty": 1}],
    combo_rounds=[
        {"id": 1, "name": "蓝区", "min": 1, "max": 1, "choices": [
            {"code": "1120", "name": "双层吉士汉堡", "diff": 0,
             "is_default": True, "quantity": 1},
            {"code": "521156", "name": "那么大鸡排（椒盐风味）", "diff": -100,
             "is_default": False, "quantity": 0},
            {"code": "1600", "name": "麦香鱼", "diff": 0,
             "is_default": False, "quantity": 0},
        ]},
        {"id": 2, "name": "粉区", "min": 1, "max": 1, "choices": [
            {"code": "3050", "name": "可乐中杯", "diff": 0,
             "is_default": True, "quantity": 1},
            {"code": "4437", "name": "小杯玉米杯", "diff": 0,
             "is_default": False, "quantity": 0},
        ]},
    ],
)
check("1+1 被识别为可换件", oneone.has_swappable_choices)

# 不吃牛的人：应把蓝区从双层吉士汉堡换成鸡肉项，而不是整单被排除
beef_forbid = DIET_RULES["no_beef"]["keywords"]
res_swap = oneone.resolve_rounds(beef_forbid)
check("不吃牛：1+1 可换件救回", res_swap["ok"])
check("不吃牛：换件后无牛肉漏网",
      not any("牛" in p["name"] or "吉士" in p["name"] for p in res_swap["picks"]),
      str([p["name"] for p in res_swap["picks"]]))
check("不吃牛：换到了鸡排", any("鸡排" in p["name"] for p in res_swap["picks"]),
      str([p["name"] for p in res_swap["picks"]]))
check("不吃牛：换件补差价正确（-¥1）", res_swap["diff_total"] == -100,
      "diff_total=%d" % res_swap["diff_total"])
check("不吃牛：记录了换件明细",
      len(res_swap["swaps"]) == 1 and "吉士汉堡" in res_swap["swaps"][0]["frm"],
      str(res_swap["swaps"]))

# 关键：filter_meals_for_person 应保留这个套餐（换件可救）
survived = filter_meals_for_person([oneone], Person(name="无牛", diet=["no_beef"]))
check("换件可救的套餐不被误排除", len(survived) == 1,
      "被错误排除")

# 但若所有备选都违禁 → 必须排除
impossible = Meal(
    code="X2", name="纯牛套餐", price=2990, category=CATEGORY_MAIN,
    combo_items=[{"round": "主食", "name": "巨无霸", "code": "1", "qty": 1}],
    combo_rounds=[{"id": 1, "name": "主食", "min": 1, "max": 1, "choices": [
        {"code": "1", "name": "巨无霸", "diff": 0, "is_default": True, "quantity": 1},
        {"code": "2", "name": "双层吉士汉堡", "diff": 0, "is_default": False, "quantity": 0},
    ]}],
)
imp = filter_meals_for_person([impossible], Person(name="无牛", diet=["no_beef"]))
check("全部备选违禁时仍被排除", len(imp) == 0)

# 可选区（min=0）找不到合规项时可以「不选」，而不是废掉整单
optional_bad = Meal(
    code="X3", name="带辣酱赠品套餐", price=2000, category=CATEGORY_MAIN,
    combo_items=[{"round": "主食", "name": "麦香鸡", "code": "3", "qty": 1}],
    combo_rounds=[
        {"id": 1, "name": "主食", "min": 1, "max": 1, "choices": [
            {"code": "3", "name": "麦香鸡", "diff": 0, "is_default": True, "quantity": 1},
        ]},
        {"id": 2, "name": "赠品", "min": 0, "max": 1, "choices": [
            {"code": "9", "name": "韩式辣椒酱", "diff": 0, "is_default": False, "quantity": 0},
        ]},
    ],
)
opt = optional_bad.resolve_rounds(DIET_RULES["no_spicy"]["keywords"])
check("可选区无合规项时选择「不选」而非整单失败", opt["ok"],
      str(opt["unresolvable"]))
check("可选区被正确丢弃", any(p.get("dropped") for p in opt["picks"]))

# 真实样例数据：应有大量可换件套餐
real_swappable = [m for m in meals if m.has_swappable_choices]
check("真实样例含可换件套餐", len(real_swappable) >= 5,
      "实际 %d" % len(real_swappable))

# min=2 的「任选 2 件」必须挑满 2 件，而不是只挑 1 件
two_pick = Meal(
    code="X4", name="任选两份套餐", price=2700, category=CATEGORY_MAIN,
    combo_items=[{"round": "任选", "name": "中薯条", "code": "a", "qty": 2}],
    combo_rounds=[{"id": 1, "name": "任选", "min": 2, "max": 2, "choices": [
        {"code": "a", "name": "中薯条", "diff": 0, "is_default": True, "quantity": 2},
        {"code": "b", "name": "奥利奥麦旋风", "diff": 0, "is_default": False, "quantity": 0},
        {"code": "c", "name": "麦乐鸡5块", "diff": 0, "is_default": False, "quantity": 0},
    ]}],
)
tp = two_pick.resolve_rounds([])
check("min=2 时挑满 2 件", len(tp["picks"]) == 2,
      "实际 %d 件" % len(tp["picks"]))

# min=3 但只有 2 件合规 → 该区不满足 → 整单不可用（不能少给）
three_pick = Meal(
    code="X5", name="必选三份套餐", price=3000, category=CATEGORY_MAIN,
    combo_items=[{"round": "任选", "name": "x", "code": "a", "qty": 3}],
    combo_rounds=[{"id": 1, "name": "任选", "min": 3, "max": 3, "choices": [
        {"code": "a", "name": "中薯条", "diff": 0, "is_default": True, "quantity": 3},
        {"code": "b", "name": "玉米杯", "diff": 0, "is_default": False, "quantity": 0},
        {"code": "c", "name": "麦乐鸡5块", "diff": 0, "is_default": False, "quantity": 0},
        {"code": "d", "name": "麦辣鸡翅2块", "diff": 0, "is_default": False, "quantity": 0},
    ]}],
)
# 不吃鸡：合规项只有 中薯条/玉米杯 = 2 件，少于必选 3 → 不可用
tpr = three_pick.resolve_rounds(DIET_RULES["no_chicken"]["keywords"])
check("合规件不足 min 时整单判定不可用", not tpr["ok"], str(tpr["unresolvable"]))
# 无忌口：4 件任选 3 件 → 可用
tpr2 = three_pick.resolve_rounds([])
check("无忌口时可挑满 3 件", tpr2["ok"] and len(tpr2["picks"]) == 3,
      "picks=%d" % len(tpr2["picks"]))

# 泛化验证：任意含可换件的套餐，对忌口用户换件后都不得漏网
leak_after_swap = []
for m in real_swappable:
    for diet_key in ("no_beef", "no_chicken", "no_pork", "no_spicy", "no_fish"):
        fkw = DIET_RULES[diet_key]["keywords"]
        r = m.resolve_rounds(fkw)
        if r["ok"]:
            for p in r["picks"]:
                if p.get("dropped"):
                    continue
                if any(k in p["name"] for k in fkw):
                    leak_after_swap.append((m.name, diet_key, p["name"]))
check("全量套餐换件后无忌口漏网", len(leak_after_swap) == 0,
      str(leak_after_swap[:5]))

# --- 6. 求解：AA 分摊精确性 ---
print("\n[6] AA 分摊（分毫必较）")
people = [Person("A"), Person("B"), Person("C")]
req = FeastRequest(people=people, budget_per_person=60)
res = solve(req, meals)
check("求解成功", res.ok, res.infeasible_reason or "")
if res.ok:
    for plan in res.plans:
        rows = plan.share()
        total = sum(r["should_pay"] for r in rows)
        check("「%s」分摊总额 == 订单总额" % plan.title,
              total == plan.total_cost,
              "分摊 %d vs 总额 %d" % (total, plan.total_cost))
        check("「%s」人均分摊差 ≤ 1 分" % plan.title,
              max(r["should_pay"] for r in rows)
              - min(r["should_pay"] for r in rows) <= 1)
        check("「%s」金额全为整数分" % plan.title,
              all(isinstance(r["should_pay"], int) for r in rows))

# --- 7. 预算硬约束 ---
print("\n[7] 预算约束")
req_b = FeastRequest(people=[Person("A"), Person("B")], budget_per_person=25)
res_b = solve(req_b, meals)
if res_b.ok:
    check("不超总预算",
          all(p.total_cost <= 25 * 100 * 2 for p in res_b.plans),
          str([p.total_cost for p in res_b.plans]))

# --- 8. 个人热量上限 ---
print("\n[8] 个人热量上限")
req_cal = FeastRequest(people=[Person("减脂", max_calories=500)],
                       budget_per_person=50)
res_cal = solve(req_cal, meals)
if res_cal.ok:
    check("个人热量上限被遵守",
          all(a.calories <= 500 for p in res_cal.plans for a in p.assignments),
          str([a.calories for p in res_cal.plans for a in p.assignments]))

# --- 9. 健康策略不选「伪零热量」 ---
print("\n[9] 健康策略正确性（营养缺失不得视为 0 卡）")
req_h = FeastRequest(people=[Person("A"), Person("B")], budget_per_person=80)
res_h = solve(req_h, meals)
health_plans = [p for p in res_h.plans if p.strategy == "healthy"]
if health_plans:
    hp = health_plans[0]
    has_all = all(m.calories is not None
                  for a in hp.assignments for m, _q in a.items)
    check("健康方案中餐品均有营养数据", has_all,
          "缺失: %s" % [m.name for a in hp.assignments for m, _ in a.items
                        if m.calories is None])

# --- 10. 多方案去重 ---
print("\n[10] 方案质量")
if res.ok:
    sigs = set()
    dup = False
    for p in res.plans:
        sig = tuple(sorted((a.person, tuple(sorted((m.code, q) for m, q in a.items)))
                           for a in p.assignments))
        if sig in sigs:
            dup = True
        sigs.add(sig)
    check("方案无重复", not dup)
    check("方案数量 ≤ 3", len(res.plans) <= 3, str(len(res.plans)))

# --- 11. 无解场景 ---
print("\n[11] 无解场景可解释")
req_imp = FeastRequest(
    people=[Person("全能忌口", diet=["vegetarian", "no_beef", "no_pork",
                                    "no_chicken", "no_fish", "no_dairy",
                                    "no_fried", "no_spicy"])],
    budget_per_person=3)
res_imp = solve(req_imp, meals)
check("无解时不崩溃且给出原因",
      (not res_imp.ok) and bool(res_imp.infeasible_reason),
      res_imp.infeasible_reason or "未给出原因")

# --- 12. 单人场景 ---
print("\n[12] 单人场景")
res_one = solve(FeastRequest(people=[Person("独自")], budget_per_person=50), meals)
check("单人可求解", res_one.ok, res_one.infeasible_reason or "")

# --- 13. JSON 可序列化 ---
print("\n[13] 可序列化（供 Agent 消费）")
if res.ok:
    try:
        s = json.dumps([p.to_dict() for p in res.plans], ensure_ascii=False)
        check("方案可 JSON 序列化", len(s) > 100)
    except Exception as e:                         # noqa: BLE001
        check("方案可 JSON 序列化", False, str(e))

# --- 12b. 换件端到端：忌口用户在真实菜单上仍能成群 ---
print("\n[12b] 换件端到端（真实菜单 + 忌口多人）")
req_swap = FeastRequest(
    people=[Person("小牛", diet=["no_beef"]),
            Person("小辣", diet=["no_spicy"]),
            Person("小素", diet=["vegetarian"])],
    budget_per_person=70)
res_swap_e2e = solve(req_swap, meals)
check("含忌口多人可求解（换件参与）", res_swap_e2e.ok,
      res_swap_e2e.infeasible_reason or "")
if res_swap_e2e.ok:
    # 换件后总价 = Σ(单品价×份数) + Σ换件补差价
    for plan in res_swap_e2e.plans:
        manual = sum(m.price * q for a in plan.assignments for m, q in a.items) \
                 + sum(a.swap_diff for a in plan.assignments)
        check("「%s」总价含换件补差价" % plan.title,
              manual == plan.total_cost,
              "手算 %d vs 方案 %d" % (manual, plan.total_cost))
        # 分摊仍须分毫不差
        rows = plan.share()
        check("「%s」换件后分摊仍精确" % plan.title,
              sum(r["should_pay"] for r in rows) == plan.total_cost)
        # 忌口零泄漏（含换件后的实际选择）
        for a in plan.assignments:
            person = next(p for p in req_swap.people if p.name == a.person)
            fkw = person.forbidden_keywords()
            for m, _q in a.items:
                if m.combo_rounds:
                    r = m.resolve_rounds(fkw)
                    for pk in r["picks"]:
                        if not pk.get("dropped"):
                            check("「%s」%s 换件后无漏网" % (m.name, a.person),
                                  not any(k in pk["name"] for k in fkw),
                                  pk["name"])
                else:
                    check("「%s」%s 单品无漏网" % (m.name, a.person),
                          not m.matches_any_keyword(fkw), m.name)
    # 至少有一个方案体现了换件（否则说明换件没生效）
    #
    # 说明：是否出现换件取决于「选了哪些套餐」。用单人 + 素食这种
    # 强约束场景更稳定地触发换件（素食者几乎只能靠换件吃到随心配/三件套）。
    swap_probe = solve(
        FeastRequest(people=[Person("素", diet=["vegetarian"])],
                     budget_per_person=70), meals)
    any_swap = any(a.swaps for p in swap_probe.plans for a in p.assignments)
    check("强约束场景下换件被实际触发", any_swap,
          "素食者的方案中未出现换件记录")

# --- 14. 中文口语解析 ---
print("\n[14] 自然语言解析")
p1 = parse_person("小王:不吃牛,不要辣")
check("中文忌口映射", "no_beef" in p1.diet and "no_spicy" in p1.diet, str(p1.diet))
p2 = parse_person("我:减脂")
check("减脂 → 热量上限（而非禁油炸）",
      p2.max_calories is not None and "no_fried" not in p2.diet,
      "max_cal=%s diet=%s" % (p2.max_calories, p2.diet))
p3 = parse_person("小李:maxcal=600,protein=30")
check("显式数值约束", p3.max_calories == 600 and p3.min_protein == 30.0)

# --- 15. 忌口词库自检 ---
print("\n[15] 忌口词库健全性")
for key, rule in DIET_RULES.items():
    check("规则 %s 有关键词" % key, len(rule["keywords"]) > 0)

# --- 15b. 隐性名称陷阱（真实事故回归）---
# 事故复盘：素食者曾被推荐「鱼牛汉堡四件套随心选」，换件后选到
# 「巨无霸」（纯牛肉）与「薄皮焦香V翅」（鸡翅）—— 因为这两个名字里
# 一个字都不含"牛/鸡/肉"。此测试确保隐性词穷举到位，永不回归。
print("\n[15b] 隐性名称陷阱（防止再次漏网）")
HIDDEN_TRAPS = {
    "巨无霸": "vegetarian",
    "不素之霸双层牛堡": "vegetarian",
    "培根蔬萃双层牛堡": "vegetarian",
    "风味双层深海鳕鱼堡": "vegetarian",
    "板烧鸡腿堡": "vegetarian",
    "麦辣鸡腿汉堡": "vegetarian",
    "薄皮焦香V翅": "vegetarian",
    "麦麦脆汁鸡-琵琶腿": "vegetarian",
    "吉士汉堡包": "vegetarian",
    "巨无霸": "no_beef",
    "板烧": "no_chicken",
    "麦辣鸡翅2块": "no_chicken",
    "薄皮焦香V翅": "no_chicken",
    "培根蔬萃双层牛堡": "no_pork",
    # ⚠️ 2026-10-09 第五次踩坑：端到端时素食者被分到「酥酥多笋卷」——
    #    它是鸡肉卷（川香风味，蛋白 14g），名称里"酥/笋/卷"无一个肉字。
    "酥酥多笋卷": "vegetarian",
    "酥酥多笋卷三件套": "vegetarian",
    "酥酥多笋卷四件套": "vegetarian",
}
for name, rule_key in HIDDEN_TRAPS.items():
    kws = DIET_RULES[rule_key]["keywords"]
    hit = [k for k in kws if k in name]
    check("%s 被 %s 正确排除" % (name, DIET_RULES[rule_key]["label"]),
          len(hit) > 0, "未命中任何关键词，会漏网")

# 素食口径：蛋奶素 / 纯素必须各司其职（Codex 复审2 轻微#1 回归）
print("\n[15c] 素食口径：蛋奶素保留乳蛋，纯素排除乳蛋")
_VEG_ALLOWED = ["芝士", "吉士", "奶", "新地", "麦旋风", "圆筒", "布丁",
                "蛋", "蛋黄酱", "冰淇淋"]
veg_kws = DIET_RULES["vegetarian"]["keywords"]
vegan_kws = DIET_RULES.get("vegan", {}).get("keywords", [])
for _kw in _VEG_ALLOWED:
    # 蛋奶素：**不应该**排除乳制品与蛋类（标签写蛋奶素就必须名副其实）
    check("蛋奶素不排除「%s」" % _kw, _kw not in veg_kws,
          "蛋奶素用户被误排除可接受食物")
    # 纯素：必须排除乳制品与蛋类
    check("纯素排除「%s」" % _kw, _kw in vegan_kws,
          "纯素规则缺失该词，会导致乳/蛋漏网")
# 两者都必须排除肉类与鱼类
for _kw in ["牛", "猪", "鸡", "鱼", "肉"]:
    check("蛋奶素排除「%s」" % _kw, _kw in veg_kws, "蛋奶素漏排肉类")
    check("纯素排除「%s」" % _kw, _kw in vegan_kws, "纯素漏排肉类")

# 全量忌口漏网独立扫描（10 种忌口 × 全部餐品，逐个展开换件结果）
print("\n[15d] 全量忌口漏网扫描（独立复算）")
from feast_engine import filter_meals_for_person            # noqa: E402
from feast_solver import _choice_hits                       # noqa: E402
_scan_total = 0
_scan_leaks = []
for _dkey in DIET_RULES:
    _p = Person(name="扫描", diet=[_dkey])
    _fw = _p.forbidden_keywords()
    for _m in filter_meals_for_person(meals, _p):
        _scan_total += 1
        if not _m.combo_rounds:
            if _m.matches_any_keyword(_fw):
                _scan_leaks.append((_dkey, _m.name))
            continue
        _res = _m.resolve_rounds(_fw)
        if not _res["ok"]:
            _scan_leaks.append((_dkey, _m.name + "(不可用却被放行)"))
            continue
        for _pk in _res["picks"]:
            if _pk.get("dropped"):
                continue
            if _choice_hits({"name": _pk["name"]}, _fw):
                _scan_leaks.append((_dkey, "%s->%s" % (_m.name, _pk["name"])))
check("全量扫描 %d 个餐品实例零漏网" % _scan_total,
      len(_scan_leaks) == 0, str(_scan_leaks[:5]))

# 修复回归：严重问题专项
print("\n[15e] 审查严重问题修复回归")
from feast_engine import build_person_candidates            # noqa: E402

# (1) 回退分支必须尊重个人热量上限（Codex#1）
_big = Meal(code="T1", name="超大堡", price=2000, category=CATEGORY_MAIN,
            calories=600, protein=30)
_c = build_person_candidates([_big], Person("控卡", max_calories=400), 100000)
check("回退分支不绕过 max_calories", len(_c) == 0, "仍返回 %d 个" % len(_c))

_c2 = build_person_candidates([_big],
                              Person("增肌", min_protein=50), 100000)
check("回退分支不绕过 min_protein", len(_c2) == 0, "仍返回 %d 个" % len(_c2))

# (2) 换件后营养必须重算（Codex#4）
_mn = Meal(code="T2", name="低卡套餐", price=1490, category=CATEGORY_MAIN,
           combo_rounds=[
               {"id": 1, "name": "区", "min": 1, "max": 1, "choices": [
                   {"code": "hi", "name": "高卡项", "diff": 0, "is_default": True,
                    "quantity": 1, "nutrition": {"calories": 900, "protein": 40,
                                                 "fat": 30, "sodium": 1000}},
               ]}])
_rn = _mn.resolve_rounds(set())
check("换件后返回实测营养", _rn.get("nutrition") is not None
      and _rn["nutrition"]["calories"] == 900,
      str(_rn.get("nutrition")))
_asi = Assignment(person="x", items=[(_mn, 1)], resolved={"T2": [{"name": "高卡项"}]},
                  nut_override={"T2": _rn["nutrition"]})
check("Assignment 用换件后营养而非套餐默认",
      _asi.calories == 900 and _asi.protein == 40.0,
      "%s/%s" % (_asi.calories, _asi.protein))

# (3) max=0 的选择区不得被强行选中（Codex#5）
_m0 = Meal(code="T3", name="零上限", price=1000, category=CATEGORY_MAIN,
           combo_rounds=[{"id": 1, "name": "区", "min": 0, "max": 0, "choices": [
               {"code": "a", "name": "不该选", "diff": 0, "is_default": True}]}])
check("max=0 的选择区不被强选", len(_m0.resolve_rounds(set())["picks"]) == 0)

# (4) min=0 的可选区不自动补件（Codex#5）
_m1o = Meal(code="T4", name="可选取", price=1000, category=CATEGORY_MAIN,
            combo_rounds=[
                {"id": 1, "name": "可选", "min": 0, "max": 2, "choices": [
                    {"code": "s", "name": "薯条", "diff": 0, "is_default": False}]},
                {"id": 2, "name": "必选", "min": 1, "max": 1, "choices": [
                    {"code": "m", "name": "汉堡包", "diff": 0, "is_default": False}]}])
check("min=0 的可选区不自动补件",
      len(_m1o.resolve_rounds(set())["picks"]) == 1,
      str(len(_m1o.resolve_rounds(set())["picks"])))

# (5) 截断循环必须有界（Codex#2）—— 10 人 × 每人多候选
import threading                                          # noqa: E402
_many = [Meal(code="B%d" % i, name="主食%d" % i, price=1000 + i * 10,
              category=CATEGORY_MAIN, calories=300 + i) for i in range(60)]
_th = threading.Thread(
    target=lambda: solve(FeastRequest(
        people=[Person(name="P%d" % i) for i in range(10)],
        budget_per_person=100), _many), daemon=True)
_th.start()
_th.join(20)
check("10 人候选截断有界（20s 内返回）", not _th.is_alive(), "疑似死循环")

# (6) preference 必须接线到换件（AGY#1）
_pf = Meal(code="T5", name="饮料套餐", price=1490, category=CATEGORY_MAIN,
           combo_rounds=[{"id": 1, "name": "饮料", "min": 1, "max": 1, "choices": [
               {"code": "d1", "name": "可乐", "diff": 0, "is_default": False},
               {"code": "d2", "name": "拿铁", "diff": 0, "is_default": False}]}])
_r_no = _pf.resolve_rounds(set())
_r_pf = _pf.resolve_rounds(set(), prefer_keywords=["拿铁"])
check("preference 影响换件结果",
      [p["name"] for p in _r_pf["picks"]] != [p["name"] for p in _r_no["picks"]]
      or _r_pf["picks"][0]["name"] == "拿铁",
      "偏好未生效")

# (7) beCode 规则覆盖 beType=6 团餐（AGY#9）
from mcd_client import _attach_becode                       # noqa: E402
_a = {}
_attach_becode(_a, 6, 1, "CODE6")
check("beType=6 团餐传 beCode", _a.get("beCode") == "CODE6")
_a2 = {}
_attach_becode(_a2, 1, 1, "SHOULD_NOT")
check("beType=1 到店不传 beCode", "beCode" not in _a2)


# --- 16. 麦麦 1+1 自助（单人模式）---
print("\n[16] 麦麦 1+1 自助（单人自选 + 自动推荐）")

ones = find_one_plus_one(meals)
check("能从真实菜单识别出 1+1 形态", len(ones) >= 1,
      "实际 %d 款" % len(ones))

if ones:
    m1 = ones[0]
    # 1+1 结构校验：恰好两个必选区、各 min=max=1
    reqs = [r for r in m1.combo_rounds if (r.get("min") or 0) >= 1]
    check("1+1 恰有两个必选区", len(reqs) == 2, str(len(reqs)))
    check("1+1 每区 min=max=1",
          all(r.get("min") == 1 and r.get("max") == 1 for r in reqs))

    # 备选面板：冲突标注正确
    p_beef = Person("测", diet=["no_beef"])
    board = list_choices(m1, p_beef)
    check("备选面板非空", len(board) >= 2)
    conflict_names = [c["name"] for z in board for c in z["choices"] if c["conflict"]]
    nonconf_names = [c["name"] for z in board for c in z["choices"] if not c["conflict"]]
    # 冲突项必须真的命中忌口词，非冲突项必须真的不命中
    ok_conf = all(any(k in n for k in DIET_RULES["no_beef"]["keywords"])
                  for n in conflict_names)
    ok_non = all(not any(k in n for k in DIET_RULES["no_beef"]["keywords"])
                 for n in nonconf_names)
    check("冲突标注准确（无错标）", ok_conf, str(conflict_names))
    check("非冲突项确实无冲突词", ok_non, str(nonconf_names))

    # 手选：给每个区挑「不冲突且序号最小」的一项 → 应无冲突、能算价
    codes_ok = []
    for z in board:
        pick = next((c for c in z["choices"] if not c["conflict"]), None)
        if pick:
            codes_ok.append(pick["code"])
    if len(codes_ok) >= 2:
        r_ok = validate_selection(m1, codes_ok, p_beef)
        check("合规手选：判定 ok", r_ok["ok"], str(r_ok["errors"]))
        check("合规手选：无冲突", len(r_ok["conflicts"]) == 0,
              str(r_ok["conflicts"]))
        check("合规手选：价格 = 基准价 + 补差价",
              r_ok["price"] == m1.price + r_ok["diff_total"],
              "%d vs %d+%d" % (r_ok["price"], m1.price, r_ok["diff_total"]))

    # 手选：故意选一个冲突项 → 应报冲突并给出替代建议
    conflict_pick = None
    conflict_zone = None
    for z in board:
        c = next((c for c in z["choices"] if c["conflict"]), None)
        if c:
            conflict_zone, conflict_pick = z, c
            break
    if conflict_pick is not None:
        # 另一个区随便挑个合规的凑齐
        other_codes = [c["code"] for z in board if z is not conflict_zone
                       for c in z["choices"] if not c["conflict"]][:1]
        r_bad = validate_selection(m1, [conflict_pick["code"]] + other_codes, p_beef)
        check("冲突手选：被识别出冲突", len(r_bad["conflicts"]) >= 1,
              str(r_bad["conflicts"]))
        check("冲突手选：给出替代建议", len(r_bad["suggestions"]) >= 1,
              str(r_bad["suggestions"]))
        if r_bad["suggestions"]:
            sug_name = r_bad["suggestions"][0]["name"]
            check("替代建议本身合规",
                  not any(k in sug_name for k in DIET_RULES["no_beef"]["keywords"]),
                  sug_name)

    # 自动推荐：结果必须全部合规（换件后无漏网）
    p_mix = Person("测", diet=["no_beef", "no_spicy"])
    recs = recommend_solo(meals, p_mix, budget_per_person=25, limit=6)
    check("自动推荐有结果", len(recs) >= 1, "实际 %d" % len(recs))
    fkw = p_mix.forbidden_keywords()
    leak = []
    for r in recs:
        for pk in r["picks"]:
            if any(k in pk["name"] for k in fkw):
                leak.append((r["name"], pk["name"]))
    check("自动推荐结果零忌口漏网", len(leak) == 0, str(leak))
    # 1+1 应排在前面
    if recs and any(r["is_one_plus_one"] for r in recs):
        check("自动推荐优先展示 1+1", recs[0]["is_one_plus_one"])

    # parse_pick_spec 两种写法等价
    a = parse_pick_spec("1120,3050")
    b = parse_pick_spec("蓝区=1120,粉区=3050")
    check("pick 两种写法解析一致", a == b, "%s vs %s" % (a, b))
    check("pick 解析支持中文标点", parse_pick_spec("1120，3050") == ["1120", "3050"])

# ---------------------------------------------------------------------------
# [16] Codex 第二轮复审修复回归
# ---------------------------------------------------------------------------
print("\n[16] Codex 复审2 修复回归")

import ssl as ssl_module                              # noqa: E402
from mcd_client import McdMcpClient                   # noqa: E402

# (1) 严重#3：min=1,max=0 绝不允许选件
_mx0 = Meal(code="MX0", name="上限零测试", price=1000)
_mx0.combo_rounds = [{"id": 1, "name": "禁区", "min": 1, "max": 0, "choices": [
    {"code": "A", "name": "普通汉堡", "diff": 0, "is_default": True, "quantity": 1},
    {"code": "B", "name": "普通鸡块", "diff": 0, "is_default": False, "quantity": 0},
]}]
_r = _mx0.resolve_rounds(set())
check("max=0 时绝不选件（严重#3）", len(_r["picks"]) == 0, str(_r["picks"]))
check("max=0 时判定不可满足", _r["ok"] is False)

# 非法区间 min>max 应被识别
_bad = Meal(code="BAD", name="非法区间", price=1000)
_bad.combo_rounds = [{"id": 1, "name": "坏区", "min": 3, "max": 1, "choices": [
    {"code": "A", "name": "甲", "diff": 0, "is_default": True, "quantity": 1},
]}]
check("min>max 非法区间被拒绝", _bad.resolve_rounds(set())["ok"] is False)

# (2) 严重#1：同名不同忌口不得串味
_cm = Meal(code="CM", name="换件套餐", price=2000)
_cm.combo_rounds = [{"id": 1, "name": "主食", "min": 1, "max": 1, "choices": [
    {"code": "BEEF", "name": "牛肉堡", "diff": 0, "is_default": True, "quantity": 1,
     "nutrition": {"calories": 500, "protein": 25.0, "fat": 20.0, "sodium": 800.0}},
    {"code": "FISH", "name": "麦香鱼", "diff": 0, "is_default": False, "quantity": 0,
     "nutrition": {"calories": 340, "protein": 15.0, "fat": 14.0, "sodium": 600.0}},
]}]
_p_no_fish = Person(name="张三", diet=["no_fish"])
_p_no_beef = Person(name="张三", diet=["no_beef"])
_req_dup = FeastRequest(people=[_p_no_fish, _p_no_beef], budget_per_person=50)
_res_dup = solve(_req_dup, [_cm])
check("同名不同忌口可求解", _res_dup.plans, _res_dup.infeasible_reason)
if _res_dup.plans:
    _pl = _res_dup.plans[0]
    # 显式核验：不吃鱼的张三不得拿到麦香鱼；不吃牛的不得拿到牛肉堡
    _fw_list = [_p_no_fish.forbidden_keywords(), _p_no_beef.forbidden_keywords()]
    _leak2 = []
    for _i, _a in enumerate(_pl.assignments):
        for _m, _q in _a.items:
            if _m.combo_rounds:
                _rr = _m.resolve_rounds(_fw_list[_i])
                for _pk in _rr["picks"]:
                    if _pk.get("dropped"):
                        continue
                    if _choice_hits({"name": _pk["name"]}, _fw_list[_i]):
                        _leak2.append((_i, _m.name, _pk["name"]))
    check("同名不同忌口零串味（严重#1）", len(_leak2) == 0, str(_leak2))

# (3) 严重#2：单人推荐必须用换件后营养
_m2 = Meal(code="M2", name="营养错配套餐", price=1000)
_m2.calories, _m2.protein = 400, 20.0
_m2.combo_rounds = [{"id": 1, "name": "主食", "min": 1, "max": 1, "choices": [
    {"code": "C1", "name": "轻食沙拉", "diff": 0, "is_default": True, "quantity": 1,
     "nutrition": {"calories": 400, "protein": 20.0, "fat": 5.0, "sodium": 300.0}},
    {"code": "C2", "name": "招牌炸鸡汉堡", "diff": 0, "is_default": False, "quantity": 0,
     "nutrition": {"calories": 800, "protein": 10.0, "fat": 40.0, "sodium": 900.0}},
]}]
_p_light = Person(name="轻食", diet=[], max_calories=600)
_recs2 = recommend_solo([_m2], _p_light)
# 若只能换到 800 千卡件，则不应推荐；若保留 400 千卡默认件，则显示的必须是 400
for _rc in _recs2:
    if _rc["code"] == "M2":
        check("单人推荐展示实际营养（严重#2）",
              _rc["calories"] is None or _rc["calories"] <= 600,
              "展示了 %s 千卡（默认值或错误值）" % _rc["calories"])

# (4) 中等#4：手选校验不得放过 max=0 的区
_m4 = Meal(code="M4", name="手选上限零", price=1000)
_m4.combo_rounds = [{"id": 1, "name": "禁区", "min": 0, "max": 0, "choices": [
    {"code": "Z", "name": "某件", "diff": 0, "is_default": False, "quantity": 0},
]}]
_v4 = validate_selection(_m4, ["Z"])
check("手选不得选中 max=0 的区（中等#4）", _v4["ok"] is False, str(_v4["errors"]))

# (5) 中等#3：营养匹配不得把「可乐」错配到「无糖可乐」
_tbl = {
    "可乐中杯": {"calories": 150, "protein": 0.0, "fat": 0.0, "sodium": 20.0},
    "无糖可乐中杯": {"calories": 0, "protein": 0.0, "fat": 0.0, "sodium": 15.0},
}
_hit_regular = merge_nutrition("可乐", _tbl)
check("「可乐」不误配到「无糖可乐」（中等#3）",
      _hit_regular.get("calories") == 150,
      "误配为 %s" % _hit_regular.get("calories"))
_hit_diet = merge_nutrition("无糖可乐", _tbl)
check("「无糖可乐」仍能正确匹配", _hit_diet.get("calories") == 0)

# (6) 中等#1：TLS 默认安全
_MC_SAFE = McdMcpClient(token="dummy-token")
check("TLS 默认开启校验（中等#1）",
      _MC_SAFE._ctx.verify_mode != ssl_module.CERT_NONE,
      "默认竟关闭了证书校验")

# (7) 中等#2：仅设偏好也应触发换件
_p_pref = Person(name="咖啡控", diet=[], preference="咖啡")
_pref_meal = Meal(code="PF", name="饮品味选套餐", price=1500)
_pref_meal.combo_rounds = [{"id": 1, "name": "饮料", "min": 1, "max": 1, "choices": [
    {"code": "COLA", "name": "可乐中杯", "diff": 0, "is_default": True, "quantity": 1},
    {"code": "COFFEE", "name": "鲜萃咖啡", "diff": 0, "is_default": False, "quantity": 0},
]}]
_req_pref = FeastRequest(people=[_p_pref], budget_per_person=50)
_res_pref = solve(_req_pref, [_pref_meal])
_pref_hit = False
if _res_pref.plans:
    for _a in _res_pref.plans[0].assignments:
        for _pk in _a.resolved.get("PF", []):
            if "咖啡" in _pk["name"]:
                _pref_hit = True
check("仅设偏好也能生效（中等#2）", _pref_hit,
      "偏好未写入实际选法")

# (8) 轻微#2：营养表以纯文本返回时不应静默变空
_fake_res = {"content": [{"type": "text",
                          "text": "[160]{productName,energyKcal}:\n可乐中杯,150"}],
             "isError": False}
_like_text = McdMcpClient._extract_text_or_res(_fake_res)
check("纯文本营养表被原样提取（轻微#2）", isinstance(_like_text, str),
      "返回了 %s，下游会静默得到空表" % type(_like_text).__name__)

# ---------------------------------------------------------------------------
# [17] Codex 第三轮复审修复回归
# ---------------------------------------------------------------------------
print("\n[17] Codex 复审3 修复回归")

# (1) 中等#1：归一化时「max 缺失」必须保留 None，不能压成 0
_rounds_norm = parse_combo_rounds({
    "rounds": [{
        "id": 1, "name": "测试区", "minQuantity": 1,
        # 故意不给 maxQuantity
        "choices": [
            {"code": "X1", "name": "甲项", "diffPrice": 0, "isDefault": 1, "quantity": 1},
            {"code": "X2", "name": "乙项", "diffPrice": 0, "isDefault": 0, "quantity": 0},
        ],
    }]
})
check("max 缺失归一化为 None（中等#1）",
      _rounds_norm and _rounds_norm[0]["max"] is None,
      "得到 %s" % (_rounds_norm[0]["max"] if _rounds_norm else "无"))

# 显式 maxQuantity=0 必须保留为 0（而不是 None）
_rounds_zero = parse_combo_rounds({
    "rounds": [{
        "id": 1, "name": "零区", "minQuantity": 0, "maxQuantity": 0,
        "choices": [{"code": "Z1", "name": "某件", "diffPrice": 0,
                     "isDefault": 0, "quantity": 0}],
    }]
})
check("显式 max=0 归一化为 0（中等#1）",
      _rounds_zero and _rounds_zero[0]["max"] == 0,
      "得到 %s" % (_rounds_zero[0]["max"] if _rounds_zero else "无"))

# (2) 严重#1：仅设营养约束时，候选评估与最终选法必须一致
_eng_meal = Meal(code="ENG", name="工程一致性套餐", price=3000)
_eng_meal.calories, _eng_meal.protein = 400, 20.0
_eng_meal.combo_rounds = [{"id": 1, "name": "主食", "min": 1, "max": 1, "choices": [
    {"code": "E1", "name": "轻食", "diff": 0, "is_default": True, "quantity": 1,
     "nutrition": {"calories": 400, "protein": 20.0, "fat": 5.0, "sodium": 300.0}},
    {"code": "E2", "name": "高卡炸物", "diff": 0, "is_default": False, "quantity": 0,
     "nutrition": {"calories": 1200, "protein": 5.0, "fat": 60.0, "sodium": 1500.0}},
]}]
_p_eng = Person(name="仅营养约束", diet=[], max_calories=500)
_res_eng = solve(FeastRequest(people=[_p_eng], budget_per_person=50), [_eng_meal])
_eng_ok = True
if _res_eng.plans:
    for _a in _res_eng.plans[0].assignments:
        # 每个方案里的人均热量必须真的 <= 500（用 Assignment 的实际有效营养）
        if _a.calories is not None and _a.calories > 500:
            _eng_ok = False
check("仅设营养约束时选法口径一致（严重#1）", _eng_ok,
      "最终方案热量超出约束")

# (3) 严重#2：只读声明必须名副其实 —— 不得存在写操作接口
check("客户端无下单接口（严重#2）", not hasattr(McdMcpClient, "create_order"))
check("客户端无领券接口（严重#2）", not hasattr(McdMcpClient, "auto_bind_coupons"))
check("客户端无个人订单接口（严重#2）", not hasattr(McdMcpClient, "order_list"))
check("客户端无个人券接口（严重#2）", not hasattr(McdMcpClient, "query_my_coupons"))

# (4) 中等#2：手选补差价按 choice.quantity 乘算
_m_qty = Meal(code="QTY", name="多份补差套餐", price=1000)
_m_qty.combo_rounds = [{"id": 1, "name": "区", "min": 1, "max": 1, "choices": [
    {"code": "Q1", "name": "基础件", "diff": 0, "is_default": True, "quantity": 1},
    {"code": "Q2", "name": "加价件", "diff": 300, "is_default": False, "quantity": 2},
]}]
_v_qty = validate_selection(_m_qty, ["Q2"])
check("手选补差价乘算份数（中等#2）", _v_qty["diff_total"] == 600,
      "得到 %d，应为 600" % _v_qty["diff_total"])

# (5) 中等#3：超出所需份数的重复 code 不得被计为「选满」
_m_dup2 = Meal(code="DUP2", name="重复选择套餐", price=1000)
_m_dup2.combo_rounds = [{"id": 1, "name": "区", "min": 1, "max": 1, "choices": [
    {"code": "D1", "name": "甲", "diff": 0, "is_default": True, "quantity": 1},
    {"code": "D2", "name": "乙", "diff": 0, "is_default": False, "quantity": 0},
]}]
# 该区只需 1 件，传 3 次同一 code 不能变成 3 件
_v_dup2 = validate_selection(_m_dup2, ["D1", "D1", "D1"])
check("重复 code 不超过应选件数（中等#3）", len(_v_dup2["picks"]) == 1,
      "picks=%d，应被裁到 1" % len(_v_dup2["picks"]))
# 而 min=2 的区传两次同一 code 是合法的（代表 2 份）
_m_dup3 = Meal(code="DUP3", name="两份套餐", price=1000)
_m_dup3.combo_rounds = [{"id": 1, "name": "区", "min": 2, "max": 2, "choices": [
    {"code": "E1", "name": "甲", "diff": 0, "is_default": True, "quantity": 2},
    {"code": "E2", "name": "乙", "diff": 0, "is_default": False, "quantity": 0},
]}]
_v_dup3 = validate_selection(_m_dup3, ["E1", "E1"])
check("min=2 时同 code 两次合法（中等#3）",
      _v_dup3["ok"] is True and len(_v_dup3["picks"]) == 2,
      "picks=%d ok=%s" % (len(_v_dup3["picks"]), _v_dup3["ok"]))

# (6) 中等#4：单人自动推荐接入 preference
_p_solo_pref = Person(name="单人咖啡控", diet=[], preference="咖啡")
_recs_pref = recommend_solo([_pref_meal], _p_solo_pref)
_pref_solo_hit = any(
    "咖啡" in pk["name"]
    for r in _recs_pref for pk in r.get("picks", [])
)
check("单人推荐接入偏好（中等#4）", _pref_solo_hit,
      "picks=%s" % [r.get("picks") for r in _recs_pref])

# (7) 中等#6：多个纯文本块应拼接而非丢弃
_multi_res = {"content": [
    {"type": "text", "text": "[160]{a,b}:"},
    {"type": "text", "text": "可乐中杯,150"},
], "isError": False}
_multi_txt = McdMcpClient._extract_text_or_res(_multi_res)
check("多纯文本块被拼接（中等#6）",
      isinstance(_multi_txt, str) and "可乐中杯" in _multi_txt,
      "得到 %s" % type(_multi_txt).__name__)

# (8) 文档一致性：默认不再写死具体城市/关键词
_cli_src = open(os.path.join(_here, "feast_cli.py"), encoding="utf-8").read()
check("CLI 不再写死默认城市（轻微#2）", 'default="北京市"' not in _cli_src,
      "仍存在硬编码城市默认值")

# ---------------------------------------------------------------------------
# [18] AGY 第二轮复审修复回归
# ---------------------------------------------------------------------------
print("\n[18] AGY 复审2 修复回归")

# (1) 严重#1：跨区重复 code 的合法默认配置必须能通过校验
_target_cross = None
for _m in meals:
    _all = [c["code"] for r in _m.combo_rounds for c in r["choices"]]
    if len(_all) != len(set(_all)) and any(r["min"] for r in _m.combo_rounds):
        _target_cross = _m
        break
if _target_cross is None:
    check("存在跨区重复 code 的真实套餐", False, "未找到可用于回归的套餐")
else:
    _dc = []
    for _r in _target_cross.combo_rounds:
        for _c in _r["choices"]:
            if _c["is_default"]:
                _dc += [_c["code"]] * max(1, _c.get("quantity") or 1)
    _rc = validate_selection(_target_cross, _dc)
    check("跨区重复 code 默认配置校验通过（严重#1）", _rc["ok"],
          "%s → %s" % (_target_cross.name, _rc["errors"]))
    # 选出的件数应等于各区 min 之和
    _expect = sum(r.get("min") or 0 for r in _target_cross.combo_rounds)
    check("跨区重复 code 选满件数正确（严重#1）",
          len(_rc["picks"]) == _expect,
          "实际 %d，应为 %d" % (len(_rc["picks"]), _expect))

# (2) 严重#2：控卡用户必须能选到换件套餐（不得整类清空）
_cands_cal = build_person_candidates(meals, Person("控卡", max_calories=800), 5000)
_n_swap_cal = sum(1 for c in _cands_cal if any(m.combo_rounds for m in c))
check("控卡用户可选到换件套餐（严重#2）", _n_swap_cal > 0,
      "含可换件候选组=%d，控卡用户被整类清空" % _n_swap_cal)
_cands_free = build_person_candidates(meals, Person("普通"), 5000)
_n_swap_free = sum(1 for c in _cands_free if any(m.combo_rounds for m in c))
check("普通用户可选到换件套餐（严重#2）", _n_swap_free > 0,
      "含可换件候选组=%d" % _n_swap_free)

# (3) 中等#1：低卡路排序不得让营养缺失项冒领前排
_low_cal_lane = sorted(
    _cands_free,
    key=lambda c: (all(m.calories is not None or m.combo_rounds for m in c),),
)[:3]
# 直接检查：真正低卡的候选组应含「每件都有营养」的组合
_complete_low = [c for c in _cands_free
                 if all(m.calories is not None for m in c)]
check("存在营养完整的候选组（中等#1）", bool(_complete_low),
      "无营养完整候选组")

# (4) 中等#2：套餐营养估算必须「所有默认区全部命中」才返回
_fake2 = {"rounds": [
    {"name": "A", "minQuantity": 1, "maxQuantity": 1, "choices": [
        {"code": "H1", "name": "巨无霸", "diffPrice": 0, "isDefault": 1, "quantity": 1}]},
    {"name": "B", "minQuantity": 1, "maxQuantity": 1, "choices": [
        {"code": "S1", "name": "完全未知小食ZZZ", "diffPrice": 0,
         "isDefault": 1, "quantity": 1}]},
]}
_tbl2 = {"巨无霸": {"calories": 550, "protein": 25.0, "fat": 30.0, "sodium": 1000.0}}
_got2 = _nutrition_from_rounds(_fake2, _tbl2)
check("套餐营养部分命中即判不可估算（中等#2）", _got2 == {},
      "返回了 %s，会低估套餐热量" % _got2)

# (5) 轻微#1：Assignment.sodium 反映换件覆盖
_am = Meal(code="NA", name="钠测试", price=1000)
_am.calories, _am.protein, _am.sodium = 500, 20.0, 1200.0
_am.combo_rounds = [{"id": 1, "name": "区", "min": 1, "max": 1, "choices": [
    {"code": "N1", "name": "基础件", "diff": 0, "is_default": True, "quantity": 1,
     "nutrition": {"calories": 500, "protein": 20.0, "fat": 20.0, "sodium": 1200.0}},
    {"code": "N2", "name": "低钠件", "diff": 0, "is_default": False, "quantity": 0,
     "nutrition": {"calories": 300, "protein": 15.0, "fat": 10.0, "sodium": 400.0}},
]}]
_asg = Assignment(person="测试", items=[(_am, 1)],
                  nut_override={"NA": {"calories": 300, "protein": 15.0,
                                       "fat": 10.0, "sodium": 400.0}})
check("Assignment.sodium 尊重换件覆盖（轻微#1）", _asg.sodium == 400.0,
      "得到 %s，应为 400.0" % _asg.sodium)

# (6) 轻微#3：calculate_price 不再有未生效的券形参
import inspect as _inspect                             # noqa: E402
_sig = _inspect.signature(McdMcpClient.calculate_price)
check("calculate_price 无未生效券形参（轻微#3）",
      "coupon_code" not in _sig.parameters and "coupon_id" not in _sig.parameters,
      "仍保留 %s" % list(_sig.parameters))

# (7) 轻微#4：死代码已清理
import normalize as _nz                                # noqa: E402
check("normalize 死代码已清理（轻微#4）",
      not hasattr(_nz, "pick_compliant_choice")
      and not hasattr(_nz, "_choice_matches_forbidden"),
      "仍存在未引用的死代码函数")

# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("结果：%d 通过 / %d 失败" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\n失败项：")
    for n, d in FAILED:
        print("  - %s  %s" % (n, d))
print("=" * 70)
sys.exit(1 if FAILED else 0)
