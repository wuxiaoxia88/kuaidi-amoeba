#!/usr/bin/env python3
"""快递阿米巴核算小工具（只用标准库）。

用一份网点的日/月经营数据（JSON），算三件事：
  1. 公共费用分摊：按「谁受益谁承担」把场地、设备、系统等费用分到各个巴。
  2. 内部结算价：在一条作业线（进港线 / 出港线）的「可分配单价」里，
     按稻盛「各工序单位时间附加值大体相同」的原则倒推每个巴的内部价，
     再和外包市场参考价对照，超出区间的给出提示。
  3. 单位时间核算表：每个巴的 内部收入 − 费用（不含本巴人工）= 附加值，
     附加值 ÷ 总工时 = 单位时间附加值，再和本巴时薪比，看有没有「挣出自己的工钱」。

用法：
  python3 tools/amoeba.py examples/demo-site.json            # 按配置里的内部价出核算表
  python3 tools/amoeba.py examples/demo-site.json --suggest  # 先按单位时间均衡倒推，再用外包参考价封顶保底
  python3 tools/amoeba.py examples/demo-site.json --markdown # 输出 Markdown 表格
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def allocate_shared(line: dict) -> dict[str, float]:
    """按每项公共费用的分摊依据（drivers）把金额分到各巴。"""
    out = {a["id"]: 0.0 for a in line["amoebas"]}
    for cost in line.get("shared_costs", []):
        drivers = cost["drivers"]
        total = sum(drivers.values())
        if total <= 0:
            raise ValueError(f"公共费用「{cost['name']}」的分摊依据合计为 0")
        for aid, w in drivers.items():
            if aid not in out:
                raise ValueError(f"公共费用「{cost['name']}」分给了不存在的巴 {aid}")
            out[aid] += cost["amount"] * w / total
    return out


def suggest_prices(line: dict, shared: dict[str, float]) -> tuple[dict[str, float], float]:
    """让线上每个巴的单位时间附加值相同：
    v = (N·P − ΣC) / ΣH，p_i = (C_i + v·H_i) / N。
    N = 本线票量，P = 每票可分配单价，C_i = 本巴费用（不含本巴人工，含分摊），H_i = 本巴工时。
    """
    n = line["tickets"]
    pool = line["unit_price"] - sum(d["amount"] for d in line.get("platform_deductions", []))
    costs = {a["id"]: sum(a["costs"].values()) + shared[a["id"]] for a in line["amoebas"]}
    hours = {a["id"]: a["hours"] for a in line["amoebas"]}
    v = (n * pool - sum(costs.values())) / sum(hours.values())
    return {aid: (costs[aid] + v * hours[aid]) / n for aid in costs}, v


def bound_by_market(line: dict, eq: dict[str, float]) -> dict[str, float]:
    """稻盛的提醒：均衡价不能脱离「社会常识」。把服务巴的价格夹在外包参考价区间里，
    差额全部归标了 residual 的利润巴（比如揽收销售巴、派送巴），让它承担经营结果。"""
    pool = line["unit_price"] - sum(d["amount"] for d in line.get("platform_deductions", []))
    residual = [a["id"] for a in line["amoebas"] if a.get("residual")]
    if not residual:
        return eq
    out = {}
    for a in line["amoebas"]:
        if a["id"] in residual:
            continue
        lo, hi = a.get("market_price") or (eq[a["id"]], eq[a["id"]])
        out[a["id"]] = min(max(eq[a["id"]], lo), hi)
    left = pool - sum(out.values())
    for aid in residual:
        out[aid] = left / len(residual)
    return out


def ledger(line: dict, prices: dict[str, float], shared: dict[str, float]) -> list[dict]:
    rows = []
    n = line["tickets"]
    for a in line["amoebas"]:
        revenue = prices[a["id"]] * n
        cost = sum(a["costs"].values()) + shared[a["id"]]
        value_added = revenue - cost
        per_hour = value_added / a["hours"]
        rows.append({
            "id": a["id"], "name": a["name"], "price": prices[a["id"]],
            "market": a.get("market_price"), "revenue": revenue, "cost": cost,
            "shared": shared[a["id"]], "value_added": value_added, "hours": a["hours"],
            "per_hour": per_hour, "wage": a.get("hourly_wage"),
            "surplus": value_added - a["hours"] * a["hourly_wage"] if a.get("hourly_wage") else None,
        })
    return rows


def check(line: dict, prices: dict[str, float], rows: list[dict]) -> list[str]:
    notes = []
    pool = line["unit_price"] - sum(d["amount"] for d in line.get("platform_deductions", []))
    gap = pool - sum(prices.values())
    if abs(gap) > 1e-6:
        notes.append(f"内部价合计比可分配单价{'少' if gap > 0 else '多'} {abs(gap):.4f} 元/票，"
                     f"{'这部分留在网点' if gap > 0 else '网点在倒贴'}。")
    for r in rows:
        m = r["market"]
        if m and not (m[0] <= r["price"] <= m[1]):
            side = "高于" if r["price"] > m[1] else "低于"
            notes.append(f"{r['name']} 内部价 {r['price']:.3f} {side}外包参考价 {m[0]:.2f}–{m[1]:.2f}："
                         f"{'先查效率，外包更便宜时要回答「为什么不外包」' if side == '高于' else '这个巴在补贴别人，要么提价，要么承认它是战略投入'}。")
        if r["wage"] and r["per_hour"] < r["wage"]:
            notes.append(f"{r['name']} 单位时间附加值 {r['per_hour']:.1f} 元/时，低于时薪 {r['wage']:.1f}：挣不出自己的工钱。")
    return notes


def fmt_table(rows: list[dict], markdown: bool) -> str:
    head = ["巴", "内部价/票", "外包参考", "内部收入", "费用(含分摊)", "附加值", "工时", "单位时间附加值", "时薪", "附加值−人工"]
    body = []
    for r in rows:
        m = f"{r['market'][0]:.2f}–{r['market'][1]:.2f}" if r["market"] else "—"
        body.append([r["name"], f"{r['price']:.3f}", m, f"{r['revenue']:,.0f}", f"{r['cost']:,.0f}",
                     f"{r['value_added']:,.0f}", f"{r['hours']:g}", f"{r['per_hour']:.1f}",
                     f"{r['wage']:.1f}" if r["wage"] else "—",
                     f"{r['surplus']:,.0f}" if r["surplus"] is not None else "—"])
    if markdown:
        lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        lines += ["| " + " | ".join(b) + " |" for b in body]
        return "\n".join(lines)
    widths = [max(len(str(x)) for x in col) for col in zip(head, *body)]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(row, widths)) for row in [head, *body])


def main() -> int:
    ap = argparse.ArgumentParser(description="快递阿米巴核算小工具")
    ap.add_argument("data", type=Path)
    ap.add_argument("--suggest", action="store_true", help="按单位时间附加值相等倒推内部价")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    site = json.loads(args.data.read_text(encoding="utf-8"))
    print(f"# {site['name']}（{site.get('period', '日')}，{site.get('note', '')}）\n")
    for line in site["lines"]:
        shared = allocate_shared(line)
        prices = {a["id"]: a.get("price", 0.0) for a in line["amoebas"]}
        v = None
        if args.suggest:
            eq, v = suggest_prices(line, shared)
            prices = bound_by_market(line, eq)
        rows = ledger(line, prices, shared)
        deductions = "，".join(f"{d['name']} {d['amount']:.2f}" for d in line.get("platform_deductions", [])) or "无"
        print(f"## {line['name']}：{line['tickets']:,} 票 × {line['unit_price']:.2f} 元/票（先扣：{deductions}）\n")
        if v is not None:
            print(f"第一步（纯均衡）：整条线的单位时间附加值统一为 {v:.1f} 元/时，对应内部价 "
                  + "，".join(f"{a['name']} {eq[a['id']]:.3f}" for a in line["amoebas"]) + "\n")
            if prices != eq:
                print("第二步（按外包参考价封顶保底，差额归利润巴）后的核算表：\n")
        print(fmt_table(rows, args.markdown))
        notes = check(line, prices, rows)
        if notes:
            print("\n提示：")
            for x in notes:
                print(f"- {x}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
