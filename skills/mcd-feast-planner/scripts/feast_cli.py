#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
feast_cli.py —— 麦麦攒局 命令行入口

两种模式：
  1. 离线演示（无需 Token）：  python feast_cli.py --demo
  2. 真实调用（需 MCP Token）：python feast_cli.py --city <城市> --keyword <地标名> \
                                   --people "我:no_beef,减脂" "小王:不要辣" --budget 50

设计目标：让评委在**没有 Token 的情况下也能完整跑通**，降低验证成本。
"""

import argparse
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from feast_solver import Person, FeastRequest, DIET_RULES   # noqa: E402
from feast_engine import solve                              # noqa: E402


SAMPLE_DIR = os.path.join(_here, "sample_data")


# ---------------------------------------------------------------------------
# 离线样例数据加载
# ---------------------------------------------------------------------------
def load_sample_meals():
    """从 sample_data/meals.json 读取离线菜单（真实结构快照）。"""
    from feast_solver import Meal
    path = os.path.join(SAMPLE_DIR, "meals.json")
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    meals = []
    for item in raw["meals"]:
        meals.append(Meal(
            code=item["code"], name=item["name"], price=item["price"],
            category=item["category"], calories=item.get("calories"),
            protein=item.get("protein"), fat=item.get("fat"),
            sodium=item.get("sodium"), tags=item.get("tags", []),
            combo_items=item.get("combo_items", []),
            combo_rounds=item.get("combo_rounds", []),
            nutrition_estimated=item.get("nutrition_estimated", False),
        ))
    return meals, raw.get("store", {})


# ---------------------------------------------------------------------------
# 人员参数解析：  "小王:no_beef,no_spicy" 或 "小王"
# ---------------------------------------------------------------------------
def parse_person(spec: str) -> Person:
    """解析参与者描述。

    支持格式：
        "小王"                                  → 无限制
        "小王:no_beef,no_spicy"                 → 忌口 key
        "小王:maxcal=600,protein=30"            → 个人约束
        "小王:不吃牛,减脂"                        → 中文口语（自动映射）
        "小王:prefer=咖啡"                       → 口味偏好（同价时优先选它）
    """
    if ":" in spec or "：" in spec:
        spec = spec.replace("：", ":")
        name, rest = spec.split(":", 1)
        rest = rest.strip()
        diet = []
        max_cal = None
        min_protein = None
        preference = None
        for token in rest.split(","):
            token = token.strip()
            if not token:
                continue
            if token.startswith("maxcal="):
                max_cal = int(token.split("=", 1)[1])
            elif token.startswith("protein="):
                min_protein = float(token.split("=", 1)[1])
            elif token.startswith("prefer="):
                # 口味偏好：同价换件时优先选中含该关键词的项
                preference = token.split("=", 1)[1].strip()
            elif token in DIET_RULES:
                diet.append(token)
            else:
                mapped = _map_chinese_diet(token)
                if not mapped:
                    continue
                kind, val = mapped
                if kind == "diet":
                    diet.append(val)
                elif kind == "maxcal":
                    max_cal = val
                elif kind == "protein":
                    min_protein = val
        return Person(name=name.strip(), diet=diet,
                      max_calories=max_cal, min_protein=min_protein,
                      preference=preference)
    return Person(name=spec.strip())


def _map_chinese_diet(text: str):
    """把中文口语映射到内置忌口 key 或个人约束。

    注意：「减脂」「控卡」「想瘦」属于**热量目标**，不是忌口，
    应当映射为热量上限而非禁止油炸（否则会误杀烤鸡类低脂餐）。
    返回 ("diet", key) 或 ("maxcal", N) 或 None。
    """
    t = text.strip()

    # 先处理热量目标（语义上不是忌口）
    if any(k in t for k in ("减脂", "控卡", "减重", "想瘦", "低卡", "低热量")):
        return ("maxcal", 650)
    if any(k in t for k in ("增肌", "高蛋白", "长肌肉")):
        return ("protein", 30)

    aliases = {
        "不吃牛": "no_beef", "牛": "no_beef",
        "不吃猪": "no_pork", "猪": "no_pork",
        "不吃鸡": "no_chicken", "鸡": "no_chicken",
        "不吃鱼": "no_fish", "鱼": "no_fish",
        # ⚠️ AGY 审查：原先"海鲜"被映射到范围更窄的 no_fish，
        #    导致内置的 no_seafood（含贝/蛤/鱿）无法通过自然语言触发。
        "不吃海鲜": "no_seafood", "海鲜": "no_seafood", "贝类": "no_seafood",
        "不吃辣": "no_spicy", "不要辣": "no_spicy", "辣": "no_spicy",
        "不吃油炸": "no_fried", "油炸": "no_fried",
        "不吃奶": "no_dairy", "乳糖不耐": "no_dairy", "奶": "no_dairy",
        "素食": "vegetarian", "吃素": "vegetarian", "蛋奶素": "vegetarian",
        # 严格纯素单列，避免与蛋奶素混为一谈（Codex 复审2 轻微#1）
        "纯素": "vegan", "严格素食": "vegan", "全素": "vegan", "vegan": "vegan",
    }
    for k, v in aliases.items():
        if k in t:
            return ("diet", v)
    return None


# ---------------------------------------------------------------------------
# 展示
# ---------------------------------------------------------------------------
def render(result, req: FeastRequest, mode_label: str):
    print("=" * 74)
    print("  麦麦攒局 · 聚餐点餐方案  [%s]" % mode_label)
    print("=" * 74)
    print("  人数：%d 人   人均预算：%s 元   门店：%s"
          % (req.headcount,
             req.budget_per_person if req.budget_per_person else "不限",
             req.store_name or "-"))
    print("  参与者：%s"
          % "；".join(
              "%s%s" % (p.name, ("[%s]" % "、".join(p.diet_labels())) if p.diet else "")
              for p in req.people))
    print()

    for w in result.warnings:
        print("  · %s" % w)
    print()

    if not result.ok:
        print("  ✗ 无可行方案")
        if result.infeasible_reason:
            print("    %s" % result.infeasible_reason)
        return

    for idx, plan in enumerate(result.plans, 1):
        print("-" * 74)
        print("  方案 %d ｜ %s" % (idx, plan.title))
        print("-" * 74)
        print("  总价 ¥%.2f   人均 ¥%.2f   总热量 %d 千卡   人均 %d 千卡"
              % (plan.total_cost / 100, plan.per_person_cost / 100,
                 plan.total_calories, plan.per_person_calories))
        print()
        for row in plan.share():
            items = " + ".join("%s×%d" % (i["name"], i["qty"]) for i in row["items"])
            print("    · %-6s  %s" % (row["person"], items))
            print("      %s%d 千卡 ｜ 蛋白 %.1fg ｜ 应付 ¥%.2f%s"
                  % (" " * 4, row["calories"], row["protein"],
                     row["should_pay"] / 100,
                     "" if row.get("nutrition_verified", True)
                     else "  （⚠ 含营养数据缺失项，热量不可保证）"))
            # 套餐展开：让用户看清「这个套餐到底含什么」
            for it in row["items"]:
                combo = it.get("combo") or ""
                final = it.get("combo_final") or combo
                if not combo:
                    continue
                if final and final != combo:
                    print("      %s└ %s 含：%s" % (" " * 4, it["name"], final))
                    print("      %s  （菜单默认：%s）" % (" " * 4, combo))
                else:
                    print("      %s└ %s 含：%s" % (" " * 4, it["name"], combo))
            # 换件明细：把「为忌口换掉了什么」讲清楚（1+1 / 三件套 / 随心选等）
            for s in row.get("swaps", []):
                if s.get("note"):
                    print("      %sℹ %s %s" % (" " * 4, s.get("combo", ""),
                                               s["note"]))
                    continue
                diff = s.get("diff", 0)
                if diff < 0:
                    tag = "省¥%.2f" % (-diff / 100)
                elif diff > 0:
                    tag = "加¥%.2f" % (diff / 100)
                else:
                    tag = "同价"
                # 区分换件动因：忌口驱动 vs 性价比驱动（对用户解释很重要）
                why = "为忌口" if s.get("reason") == "diet" else "同价优选"
                print("      %s↻ 换件（%s）%s：%s → %s（%s）"
                      % (" " * 4, why, s.get("round", ""), s.get("frm", ""),
                         s.get("to", ""), tag))
        print()
        for note in plan.notes:
            print("    %s" % note)
        # 分摊校验
        total_share = sum(r["should_pay"] for r in plan.share())
        flag = "✓" if total_share == plan.total_cost else "✗"
        print("    分摊校验：%s 各人应付合计 ¥%.2f = 订单总额 ¥%.2f"
              % (flag, total_share / 100, plan.total_cost / 100))
        print()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_demo(budget, people_specs, as_json):
    meals, store = load_sample_meals()
    people = [parse_person(s) for s in people_specs]
    req = FeastRequest(people=people, budget_per_person=budget,
                       store_code=store.get("storeCode"),
                       store_name=store.get("storeName"))
    result = solve(req, meals)
    if as_json:
        print(json.dumps({
            "mode": "demo",
            "store": store,
            "request": {
                "headcount": req.headcount,
                "budget_per_person": req.budget_per_person,
                "people": [{"name": p.name, "diet": p.diet} for p in people],
            },
            "plans": [p.to_dict() for p in result.plans],
            "warnings": result.warnings,
            "infeasible_reason": result.infeasible_reason,
        }, ensure_ascii=False, indent=2))
    else:
        render(result, req, "离线演示 · 内置样例数据")


def run_live(args):
    """真实调用：定位门店 → 取菜单 → 补详情/营养 → 求解 → 核价。"""
    from mcd_client import McdMcpClient
    from normalize import (parse_menu_codes, parse_nutrition, build_meals,
                           parse_price_result, merge_nutrition)

    cli = McdMcpClient(verbose=args.verbose)

    if not args.city or not args.keyword:
        print("真实调用需显式提供定位参数：--city <城市> --keyword <地标名>"
              "（例如 --city <城市> --keyword <校区/写字楼/商圈名>），"
              "程序会查询该位置附近的门店。")
        return 1

    print("[1/5] 定位门店 …")
    stores = cli.query_nearby_stores(city=args.city, keyword=args.keyword,
                                     be_type=args.be_type)
    if not stores:
        print("  未找到门店。")
        return 1
    store = stores[0]
    store_code = store["storeCode"]
    be_code = store.get("beCode")
    print("  → %s (storeCode=%s)" % (store["storeName"], store_code))

    print("[2/5] 拉取菜单 …")
    menu_raw = cli.query_meals(store_code, be_type=args.be_type,
                               order_type=args.order_type, be_code=be_code)
    entries = parse_menu_codes(menu_raw)
    print("  → 菜单共 %d 个餐品" % len(entries))

    print("[3/5] 补全名称/价格与营养数据 …")
    # 为控制调用量，只取前 N 个餐品详情（默认 60，可用 --max-detail 调整）
    details = {}
    for e in entries[: args.max_detail]:
        try:
            d = cli.query_meal_detail(store_code, e["code"],
                                      be_type=args.be_type,
                                      order_type=args.order_type, be_code=be_code)
            if isinstance(d, dict):
                details[e["code"]] = d
        except Exception as ex:      # noqa: BLE001
            if args.verbose:
                print("    跳过 %s: %s" % (e["code"], ex))
    print("  → 获取到 %d 个餐品详情" % len(details))

    nut_raw = cli.list_nutrition_foods()
    nutrition = parse_nutrition(nut_raw if isinstance(nut_raw, str) else "")
    print("  → 营养表 %d 条" % len(nutrition))

    meals = build_meals(entries, details, nutrition)
    # 价格补齐：detail 中常无 price，用 calculate-price 逐项补
    zero_price = [m for m in meals if m.price == 0]
    if zero_price and not args.no_price_fill:
        print("  → 补齐 %d 个餐品价格（逐个 calculate-price）" % len(zero_price))
        failed = 0
        for m in zero_price[: args.max_price_fill]:
            try:
                pr = parse_price_result(cli.calculate_price(
                    store_code, [{"productCode": m.code, "quantity": 1}],
                    be_type=args.be_type, order_type=args.order_type, be_code=be_code))
                if pr["price"] > 0:
                    m.price = pr["price"]
                else:
                    # ⚠️ Codex 审查：核价返回 0 是**静默失败**（字段名错/接口异常），
                    #    旧实现直接忽略，会让"未核验价"混进正常方案。
                    failed += 1
            except Exception:        # noqa: BLE001
                failed += 1
        if failed:
            print("  ⚠ 有 %d 个餐品核价未通过（price=0），已剔除或价格未核验"
                  % failed)

    # ⚠️ Codex 审查：菜单被 --max-detail 静默截断时，可能把"菜单范围外无解"
    #    误报成"预算内无解"。此处显式报告覆盖率与被跳过数量。
    tri_details = len(details)
    tri_menu = len(entries)
    if tri_details < tri_menu:
        print("  ⚠ 菜单覆盖：%d/%d 个餐品已取详情，另有 %d 个未参与求解"
              "（--max-detail=%d 可控）"
              % (tri_details, tri_menu, tri_menu - tri_details, args.max_detail))
    price_missing = len([m for m in meals if m.price == 0])
    if price_missing:
        print("  ⚠ 仍有 %d 个餐品价格未取得，已从候选中剔除" % price_missing)

    meals = [m for m in meals if m.price > 0]
    print("  → 可用餐品 %d 个（含价格）" % len(meals))

    print("[4/5] 求解多人方案 …")
    people = [parse_person(s) for s in args.people]
    req = FeastRequest(people=people, budget_per_person=args.budget,
                       store_code=store_code, store_name=store["storeName"],
                       be_type=args.be_type, order_type=args.order_type,
                       be_code=be_code)
    result = solve(req, meals)
    print("  → 得到 %d 个方案" % len(result.plans))

    print("[5/5] calculate-price 权威核价 …")
    unverified = 0
    for plan in result.plans:
        # ⚠️ AGY 复审2 轻微#2：多人可能点同一餐品，直接逐条 append 会产生重复的
        #    productCode 条目。按 productCode 聚合数量后再传，符合购物车 schema。
        merged_items = {}
        for a in plan.assignments:
            for m, q in a.items:
                merged_items[m.code] = merged_items.get(m.code, 0) + q
        items = [{"productCode": code, "quantity": qty}
                 for code, qty in merged_items.items()]
        try:
            pr = parse_price_result(cli.calculate_price(
                store_code, items, be_type=args.be_type,
                order_type=args.order_type, be_code=be_code))
            plan.verified_price = pr["price"]
            if pr["price"] > 0:
                # 核价基于「套餐基准价」；换件补差价由本地确定性计算叠加
                plan.total_cost = pr["price"] + sum(a.swap_diff for a in plan.assignments)
            else:
                # ⚠️ Codex 审查：price=0 是静默失败，绝不能当作"核价成功"。
                #    保留本地预测值，但显式标记为「价格未核验」。
                unverified += 1
                plan.notes.append(
                    "⚠ 核价未返回有效价格（price=0），当前总价为本地预测值，未获门店确认")
                result.warnings.append(
                    "方案「%s」核价未通过（price=0），价格未获门店确认。" % plan.title)
        except Exception as ex:      # noqa: BLE001
            unverified += 1
            plan.notes.append("⚠ 核价失败：%s（当前为本地预测价）" % ex)
    if unverified:
        print("  ⚠ %d 个方案核价未通过，已标记为「价格未核验」" % unverified)
    print("  → MCP 调用总次数：%d 次" % cli.call_count)
    print()

    if args.json:
        print(json.dumps({
            "mode": "live",
            "store": {"storeCode": store_code, "storeName": store["storeName"]},
            "mcp_calls": cli.call_count,
            "plans": [p.to_dict() for p in result.plans],
            "warnings": result.warnings,
            "infeasible_reason": result.infeasible_reason,
        }, ensure_ascii=False, indent=2))
    else:
        render(result, req, "真实调用 · %s" % store["storeName"])
    return 0


# ---------------------------------------------------------------------------
# 麦麦 1+1 自助（单人模式）
# ---------------------------------------------------------------------------
def _load_meals_for_solo(args):
    """自助模式的数据来源：--demo 读离线样例，否则走真实 MCP。"""
    if args.demo:
        meals, store = load_sample_meals()
        return meals, store, None

    from mcd_client import McdMcpClient
    from normalize import (parse_menu_codes, parse_nutrition, build_meals,
                           parse_price_result)

    cli = McdMcpClient(verbose=args.verbose)
    if not args.city or not args.keyword:
        print("真实调用需显式提供定位参数：--city <城市> --keyword <地标名>。")
        return None, None, cli
    stores = cli.query_nearby_stores(city=args.city, keyword=args.keyword,
                                     be_type=args.be_type)
    if not stores:
        print("  未找到门店。")
        return None, None, cli
    store = stores[0]
    store_code = store["storeCode"]
    be_code = store.get("beCode")
    menu_raw = cli.query_meals(store_code, be_type=args.be_type,
                               order_type=args.order_type, be_code=be_code)
    entries = parse_menu_codes(menu_raw)

    details = {}
    for e in entries[: args.max_detail]:
        try:
            d = cli.query_meal_detail(store_code, e["code"], be_type=args.be_type,
                                      order_type=args.order_type, be_code=be_code)
            if isinstance(d, dict):
                details[e["code"]] = d
        except Exception:                        # noqa: BLE001
            pass

    nut_raw = cli.list_nutrition_foods()
    nutrition = parse_nutrition(nut_raw if isinstance(nut_raw, str) else "")
    meals = build_meals(entries, details, nutrition)

    # 补价（自助模式下只补 0 价的，控制调用量）
    for m in [x for x in meals if x.price == 0][: args.max_price_fill]:
        try:
            pr = parse_price_result(cli.calculate_price(
                store_code, [{"productCode": m.code, "quantity": 1}],
                be_type=args.be_type, order_type=args.order_type, be_code=be_code))
            if pr["price"] > 0:
                m.price = pr["price"]
        except Exception:                        # noqa: BLE001
            pass
    meals = [m for m in meals if m.price > 0]
    return meals, {"storeCode": store_code, "storeName": store["storeName"]}, cli


def _print_choice_board(meal, person):
    """打印 1+1 的备选面板（标注忌口冲突与补差价）。"""
    from solo_engine import list_choices
    print("  ┌─ 备选清单（✗ = 与你的忌口冲突，+/- = 补差价）" + "─" * 20)
    for z in list_choices(meal, person):
        print("  │【%s】" % z["zone"])
        for idx, c in enumerate(z["choices"], 1):
            mark = "✗" if c["conflict"] else " "
            if c["conflict"]:
                mark += "(%s)" % "、".join(c["conflict_with"])
            diff = ""
            if c["diff"] < 0:
                diff = "省¥%.2f" % (-c["diff"] / 100)
            elif c["diff"] > 0:
                diff = "加¥%.2f" % (c["diff"] / 100)
            star = "★默认" if c["is_default"] else ""
            print("  │  %d) %-24s %s  %s %s"
                  % (idx, c["name"], c["code"], diff, (star + " " + mark).strip()))
    print("  └" + "─" * 52)


def _render_solo_pick(meal, person, res):
    """渲染手选校验结果。"""
    print()
    print("  ── 你的选择 ──")
    for p in res["picks"]:
        print("    · [%s] %s（%s）" % (p["zone"], p["name"], p["code"]))
    base = res["base_price"] / 100
    diff = res["diff_total"] / 100
    print("    基准价 ¥%.2f%s = 实付 ¥%.2f"
          % (base,
             ("  补差价 %+.2f" % diff) if diff else "（无补差价）",
             res["price"] / 100))

    if res["errors"]:
        print()
        for e in res["errors"]:
            print("    ✗ %s" % e)

    if res["conflicts"]:
        print()
        print("    ⚠ 忌口冲突提醒：")
        for c in res["conflicts"]:
            print("      · [%s] %s —— 含 %s"
                  % (c["zone"], c["name"], "、".join(c["with"])))
        print("    → 合规替换建议：")
        for s in res["suggestions"]:
            d = s["diff"]
            dtxt = ("%+.2f元" % (d / 100)) if d else "同价"
            print("      · [%s] 换成 %s（%s，%s）"
                  % (s["zone"], s["name"], dtxt, s["reason"]))
    elif res["ok"]:
        print()
        print("    ✓ 未发现忌口冲突，可直接下单。")


def run_solo(args, demo: bool):
    """单人 1+1 自助主流程。

    两种玩法：
      --solo-recommend         自动推荐多套（含换件，按忌口过滤）
      --pick "蓝区=xxx,粉区=yyy" 手选 + 实时校验（冲突提醒 + 替代建议）
      都不给                    打印备选面板（帮你看清有哪些选择）
    """
    from solo_engine import (find_one_plus_one, recommend_solo,
                             validate_selection, parse_pick_spec, list_choices)

    meals, store, cli = _load_meals_for_solo(args)
    if meals is None:
        return 1

    person = parse_person(args.me)
    store_name = (store or {}).get("storeName", "-")

    print("=" * 74)
    print("  麦麦 1+1 自助  [%s]" % ("离线演示" if demo else "真实调用 · %s" % store_name))
    print("=" * 74)
    print("  本人：%s%s"
          % (person.name,
             ("[%s]" % "、".join(person.diet_labels())) if person.diet else "[无限制]"))
    if person.max_calories:
        print("  热量上限：%d 千卡" % person.max_calories)
    print()

    ones = find_one_plus_one(meals)
    if not ones:
        print("  当前菜单未找到 1+1 形态套餐（需两个必选区，各选 1 件）。")
        if cli:
            print("  MCP 调用：%d 次" % cli.call_count)
        return 1
    print("  找到 %d 款 1+1：%s"
          % (len(ones), "、".join("%s(¥%.2f)" % (m.name, m.price / 100)
                                 for m in ones)))
    print()

    # ---- 选套餐 ----
    meal = None
    if args.meal:
        meal = next((m for m in ones if m.code == args.meal), None)
        if meal is None:
            print("  未找到 code=%s 的 1+1，改用第一款。" % args.meal)
    if meal is None:
        meal = ones[0]
    print("  使用套餐：%s（%s，¥%.2f）" % (meal.name, meal.code, meal.price / 100))
    print()

    # ---- 玩法 A：自动推荐 ----
    if args.solo_recommend:
        recs = recommend_solo(meals, person,
                              budget_per_person=args.budget,
                              limit=6)
        print("  ── 自动推荐（已按忌口筛除不可吃项，1+1 优先）──")
        if not recs:
            print("    在预算/忌口内无可用方案。")
        for i, r in enumerate(recs, 1):
            tag = "1+1" if r["is_one_plus_one"] else "组合"
            print()
            print("    %d. [%s] %s  ¥%.2f（基准 ¥%.2f）"
                  % (i, tag, r["name"], r["price"] / 100, r["base_price"] / 100))
            for pk in r["picks"]:
                print("         %s：%s" % (pk["zone"] or "选", pk["name"]))
            for s in r.get("swaps", []):
                print("         ↻ 为忌口换件 %s：%s → %s"
                      % (s.get("round", ""), s.get("frm", ""), s.get("to", "")))
            cal = r.get("calories")
            if cal is not None:
                print("         %d 千卡 ｜ 蛋白 %.1fg" % (cal, r.get("protein") or 0))
        if cli:
            print()
            print("  MCP 调用：%d 次" % cli.call_count)
        return 0

    # ---- 玩法 B：手选 ----
    if args.pick:
        codes = parse_pick_spec(args.pick)
        res = validate_selection(meal, codes, person)
        _render_solo_pick(meal, person, res)
        if args.json:
            print()
            print(json.dumps({"meal": meal.name, "code": meal.code,
                              "validation": res}, ensure_ascii=False, indent=2))
        if cli:
            print()
            print("  MCP 调用：%d 次" % cli.call_count)
        return 0 if res["ok"] else 1

    # ---- 玩法 C：只打印备选面板 ----
    _print_choice_board(meal, person)
    print()
    print("  用法：")
    print('    · 手选：  --pick "蓝区=1120,粉区=3050"   （或 "1120,3050"）')
    print("    · 推荐：  --solo-recommend")
    if cli:
        print()
        print("  MCP 调用：%d 次" % cli.call_count)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="麦麦攒局 · 麦当劳聚餐众筹点餐助手（基于麦当劳 MCP）")
    ap.add_argument("--demo", action="store_true",
                    help="离线演示模式（无需 MCP Token）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--budget", type=int, default=50, help="人均预算（元）")
    ap.add_argument("--people", nargs="+", default=["我:减脂", "小王:不要辣", "小李"],
                    help='参与者，如 "小王:no_beef,no_spicy" 或 "小王:不吃牛"')
    # ⚠️ Codex 复审3 轻微#2：不再把默认城市写死为某个真实城市，避免被误读为
    #    作者所在地；真实调用时请显式提供 --city 与 --keyword。
    ap.add_argument("--city", default=None,
                    help="城市名，真实调用时必填（如 <城市>）")
    ap.add_argument("--keyword", default=None,
                    help="定位关键词（地标/商圈/写字楼名），真实调用时必填")
    ap.add_argument("--be-type", type=int, default=1, dest="be_type",
                    help="1-到店自取 2-麦乐送 5-得来速 6-团餐")
    ap.add_argument("--order-type", type=int, default=1, dest="order_type",
                    help="1-到店 2-外送")
    ap.add_argument("--max-detail", type=int, default=60, dest="max_detail",
                    help="最多拉取多少个餐品详情")
    ap.add_argument("--max-price-fill", type=int, default=40, dest="max_price_fill",
                    help="最多补齐多少个餐品价格")
    ap.add_argument("--no-price-fill", action="store_true", dest="no_price_fill")
    ap.add_argument("--verbose", action="store_true")

    # ---- 麦麦 1+1 自助（单人）----
    solo = ap.add_argument_group("麦麦 1+1 自助（单人模式）")
    solo.add_argument("--solo", action="store_true",
                      help="进入单人 1+1 自助模式")
    solo.add_argument("--me", default="我:减脂",
                      help='本人的忌口/目标，如 "我:不吃牛,不要辣" 或 "我:减脂"')
    solo.add_argument("--meal", default=None,
                      help="指定套餐 code（不指定则用第一个 1+1）")
    solo.add_argument("--pick", default=None,
                      help='手选（两种写法）："蓝区=1120,粉区=3050" 或 "1120,3050"')
    solo.add_argument("--solo-recommend", action="store_true", dest="solo_recommend",
                      help="自动推荐多套 1+1（含换件），而非手选")
    args = ap.parse_args()

    if args.demo:
        if args.solo:
            return run_solo(args, demo=True)
        run_demo(args.budget, args.people, args.json)
        return 0

    if not os.environ.get("MCD_MCP_TOKEN"):
        print("未检测到环境变量 MCD_MCP_TOKEN。")
        print("  1) 前往 https://open.mcd.cn/mcp 申请 Token")
        print("  2) export MCD_MCP_TOKEN=你的token   (Windows: set MCD_MCP_TOKEN=...)")
        print("  或先运行离线演示：python feast_cli.py --demo"
              "（自助模式：--demo --solo）")
        return 2

    if args.solo:
        return run_solo(args, demo=False)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
