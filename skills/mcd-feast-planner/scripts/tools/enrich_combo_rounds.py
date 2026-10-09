#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_combo_rounds.py —— 为离线样例补充真实的「可换件」结构

背景：
    样例数据 meals.json 里的套餐只有 combo_items（默认组成），
    缺少 combo_rounds（全部可换备选）。而「换件」是本项目的关键能力，
    离线演示必须能体现它，否则评委会以为这只是个「静态菜单筛选器」。

做法：
    对样例里的每个套餐（含 combo_items 的餐品），调用一次
    query-meal-detail 拉取其 rounds[].choices[]，解析为 combo_rounds 后写回。

    为什么值得多花这些调用：
      1. 离线演示（--demo）无需 Token 也能展示换件能力
      2. combo_rounds 是静态数据，一次取回可永久复用，不浪费
      3. 让「样例即真实快照」，可审计

用法：
    set MCD_MCP_TOKEN=xxx        (Windows)
    py -3.14 -B tools/enrich_combo_rounds.py
可选：
    --store <storeCode>          指定门店（默认读 meals.json 里的 store）
    --only 9900013304,9900016075 只补指定 code
    --max 80                     最多补多少个（防限流）
"""

import argparse
import json
import os
import sys
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
_scripts = os.path.dirname(_here)
sys.path.insert(0, _scripts)

from mcd_client import McdMcpClient            # noqa: E402
from normalize import parse_combo_rounds, parse_nutrition   # noqa: E402

SAMPLE = os.path.join(_scripts, "sample_data", "meals.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None)
    ap.add_argument("--only", default=None, help="逗号分隔的餐品 code")
    ap.add_argument("--max", type=int, default=100)
    ap.add_argument("--be-type", type=int, default=1, dest="be_type")
    ap.add_argument("--order-type", type=int, default=1, dest="order_type")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    data = json.load(open(SAMPLE, encoding="utf-8"))
    store_code = args.store or data.get("store", {}).get("storeCode")
    if not store_code:
        print("缺少门店 code")
        return 2

    only = set(args.only.split(",")) if args.only else None

    # 只补「带组成」的套餐（单品的 rounds 恒为空，不值得调用）
    targets = [m for m in data["meals"]
               if m.get("combo_items") and (only is None or m["code"] in only)]
    targets = targets[: args.max]
    print("待补套餐：%d 个（门店 %s）" % (len(targets), store_code))

    cli = McdMcpClient(verbose=args.verbose)
    updated = 0
    swapable = 0
    failed = []

    # ⚠️ AGY 复审2 严重#2：本脚本此前未拉取营养表、也未把 nutrition 传给
    #    parse_combo_rounds(d, nutrition)，导致补出来的备选营养全为 None。
    #    上层对「有热量/蛋白约束」的用户会拒绝任何营养不可验证的换件套餐，
    #    于是控卡用户 100% 选不进包括随心配在内的所有套餐。此处补齐营养表。
    print("拉取营养表 …")
    try:
        _nut_raw = cli.list_nutrition_foods()
        nutrition = parse_nutrition(_nut_raw if isinstance(_nut_raw, str) else "")
    except Exception as ex:                        # noqa: BLE001
        nutrition = {}
        print("  营养表获取失败（%s），备选营养将为空。" % str(ex)[:60])
    print("  营养表：%d 条" % len(nutrition))

    for i, m in enumerate(targets, 1):
        try:
            d = cli.query_meal_detail(store_code, m["code"],
                                      be_type=args.be_type,
                                      order_type=args.order_type)
            if not isinstance(d, dict):
                failed.append((m["code"], "无详情"))
                continue
            rounds = parse_combo_rounds(d, nutrition)
            if not rounds:
                failed.append((m["code"], "无选择区"))
                continue
            m["combo_rounds"] = rounds
            updated += 1
            if any(len(r["choices"]) >= 2 for r in rounds):
                swapable += 1
            print("  [%d/%d] %s  ← %d 个选择区，最多 %d 个备选"
                  % (i, len(targets), m["name"], len(rounds),
                     max(len(r["choices"]) for r in rounds)))
        except Exception as ex:                    # noqa: BLE001
            failed.append((m["code"], str(ex)[:80]))

    # 兜底：未补到的套餐至少给一个空的 combo_rounds，保持字段一致
    for m in data["meals"]:
        m.setdefault("combo_rounds", [])

    # ⚠️ Codex 复审3 中等#7：本文件会被提交，真实门店号/店名/地址属个人信息，
    #    写回前强制脱敏（保留一个可辨识的占位门店，供离线演示使用）。
    data["store"] = {
        "storeCode": "0000000",
        "storeName": "麦当劳示例门店餐厅",
        "address": "示例市示例区示例路 0 号",
    }

    json.dump(data, open(SAMPLE, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\n完成：更新 %d 个套餐，其中 %d 个含可换件" % (updated, swapable))
    print("MCP 调用次数：%d" % cli.call_count)
    if failed:
        print("未补到 %d 个：" % len(failed))
        for c, why in failed[:10]:
            print("  - %s %s" % (c, why))
    return 0


if __name__ == "__main__":
    sys.exit(main())
