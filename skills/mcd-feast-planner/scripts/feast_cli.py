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
# 兼容 Windows 控制台 UTF-8 输出，防止 GBK 编码下 ¥ / 箭头等符号抛出 UnicodeEncodeError
if sys.platform == 'win32':
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


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


def _has_mcd_token():
    """是否具备可用 Token。

    判定顺序与 mcd_client 保持一致：环境变量优先；若本机存在可选的
    密文凭据模块（mcd_secret）则视为可用。**不打印、不落盘任何明文。**
    """
    if os.environ.get("MCD_MCP_TOKEN"):
        return True
    if os.environ.get("MCD_TOKEN_FILE"):
        return os.path.exists(os.environ["MCD_TOKEN_FILE"])
    try:
        import mcd_secret            # noqa: F401  （可选的本机密文凭据模块）
        return True
    except Exception:                # noqa: BLE001
        return False


def _live_query_stores(cli, args):
    """定位门店：返回 (store, be_code) 或 (None, None)。

    门店列表里只有 beType=5（得来速）/ 外送场景才带 beCode；到店自提
    （beType=1）不带，此时 be_code 为 None 属正常。
    """
    if not args.city or not args.keyword:
        print("真实调用需显式提供定位参数：--city <城市> --keyword <地标名>"
              "（例如 --city <城市> --keyword <校区/写字楼/商圈名>），"
              "程序会查询该位置附近的门店。")
        return None, None
    stores = cli.query_nearby_stores(city=args.city, keyword=args.keyword,
                                     be_type=args.be_type)
    if not stores:
        print("  未找到门店（可换 --keyword，或确认 --be-type 是否正确）。")
        return None, None
    store = stores[0]
    return store, store.get("beCode")


def _prepare_live_meals(cli, args):
    """真实模式共用数据准备：门店 → 菜单 → 详情 → 营养 → 补价 → 过滤。

    返回 (store_code, meals, store)；任一步失败返回 (None, None, None)。
    抽出来是为了让「多人配餐 / 单人自助 / 下单」三条链路共用同一份数据
    获取逻辑，避免三处各写一遍导致行为漂移。
    """
    from normalize import (parse_menu_codes, parse_nutrition, build_meals,
                           parse_price_result)

    store, be_code = _live_query_stores(cli, args)
    if store is None:
        return None, None, None
    store_code = store["storeCode"]
    print("  → %s (storeCode=%s)" % (store["storeName"], store_code))

    menu_raw = cli.query_meals(store_code, be_type=args.be_type,
                               order_type=args.order_type, be_code=be_code)
    entries = parse_menu_codes(menu_raw)
    print("  → 菜单共 %d 个餐品" % len(entries))

    details = {}
    for e in entries[: args.max_detail]:
        try:
            d = cli.query_meal_detail(store_code, e["code"], be_type=args.be_type,
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
    failed = 0
    if zero_price and not args.no_price_fill:
        print("  → 补齐 %d 个餐品价格（逐个 calculate-price）" % len(zero_price))
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
    return store_code, meals, store


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
    from normalize import parse_price_result

    cli = McdMcpClient(verbose=args.verbose)

    print("[1/5] 定位门店 / [2/5] 拉取菜单 / [3/5] 补全数据 …")
    store_code, meals, store = _prepare_live_meals(cli, args)
    if store_code is None:
        return 1
    be_code = store.get("beCode")

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
    """自助模式的数据来源：--demo 读离线样例，否则走真实 MCP。

    返回 (meals, store, cli)；失败时 meals 为 None，cli 仍返回以便打印调用统计。
    """
    if args.demo:
        meals, store = load_sample_meals()
        return meals, store, None

    from mcd_client import McdMcpClient

    cli = McdMcpClient(verbose=args.verbose)
    store_code, meals, store = _prepare_live_meals(cli, args)
    if store_code is None:
        return None, None, cli
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
    # 标签展示：把「饮食忌口」与「营养约束」合并成一个字符串，
    # 避免出现「我:减脂」已生效（设了热量上限）却显示「[无限制]」的误导。
    _labels = list(person.diet_labels())
    if person.max_calories:
        _labels.append("热量≤%d千卡" % person.max_calories)
    if person.min_protein:
        _labels.append("蛋白≥%g克" % person.min_protein)
    if person.preference:
        _labels.append("偏好=%s" % person.preference)
    labels_text = "、".join(_labels) if _labels else "无限制"

    print("  本人：%s[%s]" % (person.name, labels_text))
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


def _make_client(args):
    """构造真实 MCP 客户端（仅在这些命令里按需导入，保持离线演示零依赖）。"""
    from mcd_client import McdMcpClient
    return McdMcpClient(verbose=getattr(args, "verbose", False))


def _unwrap_payload(raw):
    """把 MCP 返回值统一拆到业务 payload。

    ⚠️ 返回类型**不固定**：信封印版是 {success,code,data}，但
       query-nearby-stores 直接返回**裸列表**（实测）。旧实现把非 dict
       一律压成 {}，导致真实门店数据被静默丢弃、误报「未找到门店」。
       现在保留 list 原样返回，交由各 parse_* 自行处理。
    """
    from mcd_client import McdMcpClient
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if isinstance(raw, dict) and "success" in raw:
        return McdMcpClient._unwrap(raw)
    if isinstance(raw, list):
        return raw
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# 演示模式下的门店 / 券样例（无 Token 也能看输出格式）
# ---------------------------------------------------------------------------
_DEMO_STORES = [
    {"store_code": "DEMO-0001", "store_name": "麦当劳示例门店（甲）",
     "address": "示例市示例区示例路 1 号", "be_code": None, "phone": "000-0000-0000"},
    {"store_code": "DEMO-0002", "store_name": "麦当劳示例门店（乙）",
     "address": "示例市示例区示例路 2 号", "be_code": None, "phone": "000-0000-0001"},
    {"store_code": "DEMO-0005", "store_name": "麦当劳示例得来速餐厅",
     "address": "示例市示例区示例大道 5 号", "be_code": "BE-DEMO-5",
     "phone": "000-0000-0002"},
]
_DEMO_COUPONS = [
    {"title": "【演示】满 30 减 5", "coupon_code": "CPN-DEMO-30-5",
     "coupon_id": "CID-DEMO-1", "trade_time": "2026-01-01 ~ 2026-12-31",
     "promotion_tag": "演示券", "status": "可领取", "products": []},
    {"title": "【演示】指定套餐立减 3 元", "coupon_code": "CPN-DEMO-COMBO-3",
     "coupon_id": "CID-DEMO-2", "trade_time": "2026-01-01 ~ 2026-12-31",
     "promotion_tag": "演示券", "status": "可领取",
     "products": [{"code": "DEMO-P1", "name": "精选超值随心配"}]},
]


# ---------------------------------------------------------------------------
# 门店搜索
# ---------------------------------------------------------------------------
def run_stores(args) -> int:
    """搜索门店：--favorites 查收藏；否则按 --city/--keyword 搜索。

    ⚠️ beType=1（到店自取）返回的门店**没有** beCode，后续调用不得传；
       beType=5（得来速）返回的门店有 beCode，后续调用必须传。
    """
    from normalize import parse_stores

    # ⚠️ 参数校验对 demo / 真实模式**一视同仁**：否则演示时能跑通、真跑时
    #    才报错，用户会以为是自己环境的问题。（自测 [20] 已覆盖此点）
    if args.favorites:
        search_type = 1
        if args.be_type not in (1, 5):
            print("--favorites 仅支持 --be-type 1（到店自取）或 5（得来速）。")
            return 2
    else:
        search_type = 2
        if not args.city or not args.keyword:
            print("按位置搜索门店需要同时提供 --city 与 --keyword。")
            print('  例：python feast_cli.py --stores --city <城市> --keyword <地标名>')
            print("  （查收藏门店请改用 --favorites）")
            return 2

    if args.demo:
        stores = [dict(s) for s in _DEMO_STORES]
        if args.favorites:
            stores = stores[:2]
        if not args.favorites and args.be_type == 5:
            stores = [s for s in stores if s["be_code"]]
        cli = None
    else:
        cli = _make_client(args)
        cli.connect()
        raw = cli.query_nearby_stores(args.city, args.keyword,
                                      be_type=args.be_type, search_type=search_type)
        stores = parse_stores(_unwrap_payload(raw))

    if args.json:
        print(json.dumps(stores, ensure_ascii=False, indent=2))
        return 0

    print("=" * 74)
    print("  附近门店  [%s%s]"
          % ("收藏门店" if args.favorites else "按位置搜索",
             " · 离线演示样例" if args.demo else ""))
    print("=" * 74)
    if not stores:
        print("  未找到门店。请确认 --city / --keyword 是否准确。")
        return 0
    for i, s in enumerate(stores, 1):
        line = "  %d. %s" % (i, s["store_name"] or "(未命名门店)")
        print(line)
        if s["store_code"]:
            print("     storeCode = %s" % s["store_code"])
        if s["be_code"]:
            print("     beCode    = %s   ← 后续调用必须传" % s["be_code"])
        elif args.be_type == 5:
            print("     beCode    = (该门店未返回 beCode)")
        if s["address"]:
            print("     地址：%s" % s["address"])
        if s["phone"]:
            print("     电话：%s" % s["phone"])
    print()
    print("  下一步：用 storeCode 查菜单/券，例如")
    print("    python feast_cli.py --coupons --store-code <storeCode>")
    print("  ⚠️ 到店自取(beType=1) 不传 beCode；得来速(beType=5) 必须传 beCode。")
    if cli is None:
        print("  （离线演示样例，未发起任何 MCP 调用）")
    else:
        print("  MCP 调用：%d 次" % cli.call_count)
    return 0


# ---------------------------------------------------------------------------
# 优惠券
# ---------------------------------------------------------------------------
def run_coupons(args) -> int:
    """券查询/领取。三种口径必须分清，避免误导：
       · query-store-coupons —— 当前**门店**下可用（下单用这个）
       · query-my-coupons    —— 卡包中**已拥有**，不做门店校验
       · available-coupons   —— 当前**可领取**（尚未领取）
    """
    from normalize import parse_coupons

    if args.demo:
        cli = None
        if args.bind_coupons and not args.confirm:
            print("一键领券是【写操作】，会真实改变账户券资产。")
            print("确认后请追加 --confirm 重跑：")
            print("  python feast_cli.py --bind-coupons --confirm")
            return 2
        coupons = [dict(c) for c in _DEMO_COUPONS]
        if args.my_coupons:
            title = "我的卡包券（不做门店/订单校验，不承诺可用于当前订单）· 离线演示样例"
        elif args.available:
            title = "当前可领取的券（尚未领取）· 离线演示样例"
        elif args.bind_coupons:
            title = "一键领券结果 · 离线演示样例（未真实执行）"
        else:
            if not args.store_code:
                print("查询门店可用券需要 --store-code。")
                print("  （查卡包用 --my-coupons；查可领取用 --available）")
                return 2
            title = ("门店 %s 可用券（已校验门店/渠道/配送规则）· 离线演示样例"
                     % args.store_code)
        if args.json:
            print(json.dumps(coupons, ensure_ascii=False, indent=2))
            return 0
        print("=" * 74)
        print("  %s" % title)
        print("=" * 74)
        for i, c in enumerate(coupons, 1):
            suffix = ("（%s）" % c["status_text"]) if c.get("status_text") else ""
            print("  %d. %s%s" % (i, c["title"] or "(无标题)", suffix))
            if c["coupon_code"] or c["coupon_id"]:
                print("     couponCode = %s   couponId = %s"
                      % (c["coupon_code"] or "-", c["coupon_id"] or "-"))
            if c["trade_time"]:
                print("     有效期：%s" % c["trade_time"])
            if c["products"]:
                names = "、".join((p["name"] or p["code"] or "?")
                                  for p in c["products"][:6])
                print("     适用商品：%s" % names)
        print()
        print("  ⚠️ 口径说明：把 couponId/couponCode 传入 calculate-price 可得到**券后价**；")
        print("     卡包券(query-my-coupons) 未经门店校验，实际能否使用以下单结果为准。")
        print("  （离线演示样例，未发起任何 MCP 调用）")
        return 0

    cli = _make_client(args)
    cli.connect()

    # 一键领券：写操作，需 --confirm
    if args.bind_coupons:
        if not args.confirm:
            print("一键领券是【写操作】，会真实改变账户券资产。")
            print("确认后请追加 --confirm 重跑：")
            print("  python feast_cli.py --bind-coupons --confirm")
            return 2
        raw = cli.auto_bind_coupons()
        print("=" * 74)
        print("  一键领券结果")
        print("=" * 74)
        print("  %s" % json.dumps(_unwrap_payload(raw), ensure_ascii=False)[:500])
        print("  MCP 调用：%d 次" % cli.call_count)
        return 0

    if args.my_coupons:
        raw = cli.query_my_coupons()
        coupons = parse_coupons(_unwrap_payload(raw))
        title = "我的卡包券（不做门店/订单校验，不承诺可用于当前订单）"
    elif args.available:
        raw = cli.available_coupons()
        coupons = parse_coupons(_unwrap_payload(raw))
        title = "当前可领取的券（尚未领取）"
    else:
        store_code = args.store_code
        if not store_code:
            print("查询门店可用券需要 --store-code。")
            print("  （查卡包用 --my-coupons；查可领取用 --available）")
            return 2
        raw = cli.query_store_coupons(store_code, be_type=args.be_type,
                                      order_type=args.order_type,
                                      be_code=args.be_code)
        coupons = parse_coupons(_unwrap_payload(raw))
        title = "门店 %s 可用券（已校验门店/渠道/配送规则）" % store_code

    if args.json:
        print(json.dumps(coupons, ensure_ascii=False, indent=2))
        return 0

    print("=" * 74)
    print("  %s" % title)
    print("=" * 74)
    if not coupons:
        print("  没有查到券。")
        return 0
    for i, c in enumerate(coupons, 1):
        suffix = ("（%s）" % c["status_text"]) if c.get("status_text") else ""
        print("  %d. %s%s" % (i, c["title"] or "(无标题)", suffix))
        if c["coupon_code"] or c["coupon_id"]:
            print("     couponCode = %s   couponId = %s"
                  % (c["coupon_code"] or "-", c["coupon_id"] or "-"))
        if c["trade_time"]:
            print("     有效期：%s" % c["trade_time"])
        if c["promotion_tag"]:
            print("     促销标签：%s" % c["promotion_tag"])
        if c["products"]:
            names = "、".join((p["name"] or p["code"] or "?") for p in c["products"][:6])
            print("     适用商品：%s%s"
                  % (names, " 等" if len(c["products"]) > 6 else ""))
    print()
    print("  ⚠️ 口径说明：把 couponId/couponCode 传入 calculate-price 可得到**券后价**；")
    print("     卡包券(query-my-coupons) 未经门店校验，实际能否使用以下单结果为准。")
    print("  MCP 调用：%d 次" % cli.call_count)
    return 0


# ---------------------------------------------------------------------------
# 创建订单（写操作，默认 dry-run）
# ---------------------------------------------------------------------------
def run_order(args) -> int:
    """创建订单。**默认只打印将要提交的报文（dry-run）**，加 --confirm 才真实下单。

    ⚠️ 到店(orderType=1) 必传 takeWayCode（来自 calculate-price 的 takeWayList）；
       外送(orderType=2) 必传 addressId。
    ⚠️ 麦当劳 MCP **没有独立支付工具**：下单后订单处于待支付态，
       支付需在麦当劳官方渠道完成，之后用 --pay-status 复核。
    """
    store_code = args.store_code
    cli = None
    if not store_code:
        if args.demo:
            # 演示模式允许省略 --store-code，用样例门店占位
            store_code = _DEMO_STORES[0]["store_code"]
        else:
            print("下单需要 --store-code。")
            print("  可先用 `--stores --city <城市> --keyword <地标名>` 查门店编码。")
            return 2
    if not args.take_way_code and not args.address_id:
        print("缺少下单所需参数：")
        print("  · 到店(orderType=1)：需要 --take-way-code（来自 calculate-price 的 takeWayList）")
        print("  · 外送(orderType=2)：需要 --address-id")
        return 2

    # 用本地方案生成 items（与展示口径一致）
    if args.demo:
        meals, store = load_sample_meals()
    else:
        cli = _make_client(args)
        out = _prepare_live_meals(cli, args)
        if out is None:
            return 1
        store_code, meals, store = out

    people = [parse_person(p) for p in args.people]
    req = FeastRequest(people=people, budget_per_person=args.budget,
                       store_code=store_code,
                       store_name=(store or {}).get("storeName"),
                       be_type=args.be_type, order_type=args.order_type,
                       be_code=args.be_code)
    result = solve(req, meals)
    if not result.plans:
        print("未求得可行方案，无法下单。")
        if result.infeasible_reason:
            print("  原因：%s" % result.infeasible_reason)
        return 1

    plan = result.plans[0]
    # ⚠️ 官方购物车 schema 要求同一 productCode 聚合数量，不能重复列出
    agg = {}
    for a in plan.assignments:
        for m, q in a.items:
            agg[m.code] = agg.get(m.code, 0) + q
    items = [{"productCode": c, "quantity": q} for c, q in agg.items()]

    payload = {"storeCode": store_code, "beType": args.be_type,
               "orderType": args.order_type, "items": items}
    if args.take_way_code:
        payload["takeWayCode"] = args.take_way_code
    if args.address_id:
        payload["addressId"] = args.address_id
    if args.be_code:
        payload["beCode"] = args.be_code

    if args.json:
        print(json.dumps({"dry_run": not args.confirm, "payload": payload,
                          "plan": plan.title,
                          "total_cost_yuan": round(plan.total_cost / 100.0, 2)},
                         ensure_ascii=False, indent=2))
        return 0

    print("=" * 74)
    print("  创建订单  %s" % ("[真实下单]" if args.confirm else "[DRY-RUN 预演]"))
    print("=" * 74)
    print("  门店：%s" % (store_code or "-"))
    print("  场景：beType=%s  orderType=%s  方案：%s"
          % (args.be_type, args.order_type, plan.title))
    print("  明细：")
    for c, q in agg.items():
        print("    · %s ×%d" % (c, q))
    print("  合计：¥%.2f" % (plan.total_cost / 100.0))
    print()
    print("  将提交的报文：")
    print("  %s" % json.dumps(payload, ensure_ascii=False))

    if not args.confirm:
        print()
        print("  ⚠️ 当前为 DRY-RUN，**未真实下单**。确认无误后追加 --confirm 执行。")
        print("  ⚠️ 下单后订单为待支付状态；麦当劳 MCP 无独立支付工具，")
        print("     请在麦当劳官方渠道完成支付，再用 --pay-status 复核。")
        return 0

    if args.demo:
        print()
        print("  ⚠️ 离线演示模式**不会真实下单**（--demo 下 --confirm 仅演示流程）。")
        print("     去掉 --demo 并配置 Token 后可真实创建订单。")
        return 0

    if cli is None:
        cli = _make_client(args)
    cli.connect()
    raw = cli.create_order(
        store_code, items, be_type=args.be_type, order_type=args.order_type,
        be_code=args.be_code, take_way_code=args.take_way_code,
        address_id=args.address_id)
    from normalize import parse_order_result
    res = parse_order_result(_unwrap_payload(raw))
    print()
    print("  下单结果：")
    print("    订单号：%s" % (res["order_id"] or "(未返回)"))
    print("    状态　：%s %s" % (res["status"], res["status_text"]))
    if res["price"]:
        print("    金额　：¥%.2f" % (res["price"] / 100.0))
    print()
    print("  ⚠️ 支付说明：MCP 无支付工具。请在**麦当劳官方渠道**完成支付；")
    print("     之后用 `--pay-status --order-id <订单号>` 复核支付状态。")
    print("  MCP 调用：%d 次" % cli.call_count)
    return 0


# ---------------------------------------------------------------------------
# 支付状态查询
# ---------------------------------------------------------------------------
def run_pay_status(args) -> int:
    """查询订单支付状态（这是 MCP 侧唯一与"支付"相关的真实能力）。"""
    if not args.order_id:
        print("查询支付状态需要 --order-id。")
        return 2
    if args.demo:
        print("=" * 74)
        print("  订单支付状态  [离线演示样例]")
        print("=" * 74)
        print("  订单号：%s" % args.order_id)
        print("  状态　：演示模式不查询真实订单（未发起任何 MCP 调用）")
        print()
        print("  ⚠️ 麦当劳 MCP 无独立支付工具，本项目**不代收款项、不代付款**。")
        return 0
    cli = _make_client(args)
    cli.connect()
    raw = cli.query_order(args.order_id)
    from normalize import parse_order_result
    res = parse_order_result(_unwrap_payload(raw))

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print("=" * 74)
    print("  订单支付状态")
    print("=" * 74)
    print("  订单号：%s" % args.order_id)
    print("  状态　：%s %s" % (res["status"] or "(未返回)", res["status_text"]))
    if res["price"]:
        print("  金额　：¥%.2f" % (res["price"] / 100.0))
    if res["pay_url"]:
        print("  支付链接：%s" % res["pay_url"])
    else:
        print("  支付链接：(接口未返回) —— 请在麦当劳官方渠道完成支付")
    print()
    print("  ⚠️ 麦当劳 MCP 无独立支付工具，本项目**不代收款项、不代付款**。")
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

    # ---- 门店 / 优惠券 / 下单 / 支付状态 ----
    shop = ap.add_argument_group("门店 · 优惠券 · 下单 · 支付状态（需真实 Token）")
    shop.add_argument("--stores", action="store_true",
                      help="搜索附近门店（配合 --city/--keyword，或 --favorites 查收藏）")
    shop.add_argument("--favorites", action="store_true",
                      help="查询收藏门店（searchType=1），而非按位置搜索")
    shop.add_argument("--coupons", action="store_true",
                      help="查询券：默认查「当前门店可用券」；加 --my-coupons 查卡包；"
                           "加 --available 查可领取券")
    shop.add_argument("--my-coupons", action="store_true", dest="my_coupons",
                      help="查本人卡包中的券（不做门店校验）")
    shop.add_argument("--available", action="store_true",
                      help="查当前可领取的券")
    shop.add_argument("--bind-coupons", action="store_true", dest="bind_coupons",
                      help="一键领取麦麦省全部可用券（写操作，需 --confirm）")
    shop.add_argument("--store-code", default=None, dest="store_code",
                      help="门店编码（--coupons / --order 时用）")
    shop.add_argument("--be-code", default=None, dest="be_code",
                      help="业务编码（beType=5/6 或外送时必传）")
    shop.add_argument("--order", action="store_true",
                      help="按当前方案创建订单（写操作；默认 dry-run，加 --confirm 才真实下单）")
    shop.add_argument("--take-way-code", default=None, dest="take_way_code",
                      help="到店自取所需的取餐码（来自 calculate-price 的 takeWayList）")
    shop.add_argument("--address-id", default=None, dest="address_id",
                      help="外送所需的配送地址 ID")
    shop.add_argument("--confirm", action="store_true",
                      help="确认执行写操作（下单 / 领券）")
    shop.add_argument("--order-id", default=None, dest="order_id",
                      help="订单号（配合 --pay-status 查询支付状态）")
    shop.add_argument("--pay-status", action="store_true", dest="pay_status",
                      help="查询指定订单的支付状态（--order-id 必填）")
    args = ap.parse_args()

    # ---- 无需门店数据的轻量命令优先处理 ----
    if args.pay_status:
        return run_pay_status(args)
    if args.available or args.my_coupons or args.bind_coupons:
        return run_coupons(args)

    # ⚠️ --stores / --coupons / --order 可在 --demo 下运行（无 Token 也可预演），
    #    因此必须先于 demo 分支判断，否则 --demo --order 会被 demo 分支吞掉。
    if args.demo:
        if args.stores:
            return run_stores(args)
        if args.coupons:
            return run_coupons(args)
        if args.order:
            return run_order(args)
        if args.solo:
            return run_solo(args, demo=True)
        run_demo(args.budget, args.people, args.json)
        return 0

    if not _has_mcd_token():
        print("未检测到麦当劳 MCP Token。")
        print("  1) 前往 https://open.mcd.cn/mcp 申请 Token")
        print("  2) 通过环境变量注入：export MCD_MCP_TOKEN=你的token"
              "  (Windows: set MCD_MCP_TOKEN=...)")
        print("     或使用本机 DPAPI 密文凭据 "
              "C:\\Users\\<用户名>\\.workbuddy\\secrets\\mcd_mcp_token.cred")
        print("  或先运行离线演示：python feast_cli.py --demo"
              "（自助模式：--demo --solo）")
        return 2

    # ---- 需要门店的写/查命令 ----
    if args.stores:
        return run_stores(args)
    if args.coupons:
        return run_coupons(args)
    if args.order:
        return run_order(args)

    if args.solo:
        return run_solo(args, demo=False)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
