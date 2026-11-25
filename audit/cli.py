"""命令行入口：读取资料 -> 跑流水线 -> 输出结构化 JSON 与文字版简报。

用法：
    python3 -m audit.cli data/records.json --json out/brief.json --markdown out/brief.md
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .report import result_to_dict, run_pipeline
from .validation import load_payload


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.1%}"


def render_markdown(result) -> str:
    rel = result.release
    lines: list[str] = []
    lines.append("# 校外体验参与复核抽样简报（滚动纠偏）")
    lines.append("")
    lines.append(
        f"- 信心水平 {rel['confidence']:.0%}｜区间宽度上限 {rel['max_width']:.2f}"
        f"｜每发布分组最低确证 {rel['minimum_verified']}"
    )
    lines.append(
        f"- 有效总体周 {rel['valid_frame_weeks']}｜历史已发布至第 {rel['published_through']} 周（不重新编辑）"
    )
    lines.append(f"- 排程周：第 {result.planning_week} 周")
    lines.append("")

    lines.append("## 1. 适用总体与版本覆盖")
    lines.append("")
    lines.append("| 周 | 分层 | 站点 | 冻结N | 现行N | 并入重复 | 后补 | 冻结抽取覆盖 | 未覆盖单位 |")
    lines.append("|---|---|---|---:|---:|---|---|---:|---|")
    for row in result.frame_versions:
        dup = ", ".join(f"{a}→{b}" for a, b in row["duplicates_merged"]) or "—"
        late = ", ".join(row["late_units"]) or "—"
        uncov = ", ".join(row["uncovered_units"]) or "—"
        lines.append(
            f"| {row['week']} | {row['stratum']} | {row['site']} | {row['frozen_n']} "
            f"| {row['current_n']} | {dup} | {late} | {len(row['covered_by_frozen_draw'])} | {uncov} |"
        )
    lines.append("")

    lines.append("## 2. 分层随机估计（指定核查不进随机口径）")
    lines.append("")
    lines.append("| 周 | 分层 | 随机单位 | 确证 | 到场 | 无应答 | 待核 | 点估计 | 未知份额 | 90%区间 | 宽度 | 覆盖率 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|")
    for e in result.stratum_estimates:
        ci = "—" if e.ci_low is None else f"[{e.ci_low:.3f}, {e.ci_high:.3f}]"
        width = "—" if e.interval_width is None else f"{e.interval_width:.3f}"
        lines.append(
            f"| {e.week} | {e.stratum} | {e.sampled_effective} | {e.evidence_n} "
            f"| {e.verified_attended} | {e.nonresponse_n} | {e.pending_n} "
            f"| {_pct(e.point)} | {_pct(e.unknown_share)} | {ci} | {width} "
            f"| {e.covered_fraction:.1%} |"
        )
    lines.append("")

    lines.append("## 3. 总体与可发布分组")
    lines.append("")
    lines.append("| 汇总 | 层级 | 现行N | 未覆盖 | 确证 | 点估计 | 未知份额 | 90%区间 | 宽度 | 判定 | 拦截原因 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---:|---|---|---|")
    order = ["总体"] + [f"组:{g}" for g in rel["groups"]]
    week_keys = [k for k in result.aggregates if k.startswith("周")]
    order += sorted(week_keys, key=lambda k: (int(k.split(":")[0][1:]), k))
    for key in order:
        a = result.aggregates[key]
        ci = "—" if a.ci_low is None else f"[{a.ci_low:.3f}, {a.ci_high:.3f}]"
        width = "—" if a.interval_width is None else f"{a.interval_width:.3f}"
        reasons = "；".join(a.blocking_reasons) or "—"
        lines.append(
            f"| {a.name} | {a.level} | {a.current_n} | {a.uncovered_n} | {a.evidence_n} "
            f"| {_pct(a.point)} | {_pct(a.unknown_share)} | {ci} | {width} "
            f"| {a.release_status} | {reasons} |"
        )
    lines.append("")

    lines.append("## 4. 补查时段排程（已约核查不可取消）")
    lines.append("")
    lines.append("| 站点 | 总时段 | 留给已约 | 可补抽 | 申请补抽 | 已安排 | 排不上 | 被迫顺延的已约 |")
    lines.append("|---|---:|---:|---:|---|---|---|---|")
    for p in result.schedule.plans.values():
        lines.append(
            f"| {p.site} | {p.total_slots} | {p.reserved_appointments} | {p.residual_slots} "
            f"| {', '.join(p.requested) or '—'} | {', '.join(p.scheduled) or '—'} "
            f"| {', '.join(p.unscheduled) or '—'} | {', '.join(p.deferred_appointments) or '—'} |"
        )
    lines.append("")

    lines.append("## 5. 已提请核查代表的总体版本：可复用与须补抽")
    lines.append("")
    if not result.reuse:
        lines.append("（本期无修订层）")
    for row in result.reuse:
        lines.append(f"### {row['week']}/{row['stratum']}")
        lines.append("")
        lines.append(f"- {row['represents']}")
        lines.append(f"- 重复映射：{', '.join(row['duplicate_mapping']) or '—'}")
        lines.append(f"- 可复用的已核查物理段（{row['reusable_count']}）：{', '.join(row['reusable_effective_segments']) or '—'}")
        lines.append(f"- 须补抽单位（旧抽取机会为 0）：{', '.join(row['supplement_required_units']) or '—'}")
        lines.append(f"- {row['note']}")
        lines.append("")

    lines.append("## 6. 情景评估：新增核查能否替换草稿（不改动历史值）")
    lines.append("")
    if not result.scenarios:
        lines.append("（无可安排的补抽）")
    for sc in result.scenarios:
        lines.append(f"### {sc.name}")
        lines.append("")
        lines.append(f"> {sc.description}")
        lines.append("")
        lines.append("| 汇总 | 点估计 | 90%区间 | 宽度 | 覆盖率 | 判定 | 拦截原因 |")
        lines.append("|---|---:|---|---:|---:|---|---|")
        for key, a in sc.aggregates.items():
            if "历史已发布" in a.release_status:
                continue
            ci = "—" if a.ci_low is None else f"[{a.ci_low:.3f}, {a.ci_high:.3f}]"
            width = "—" if a.interval_width is None else f"{a.interval_width:.3f}"
            reasons = "；".join(a.blocking_reasons) or "—"
            lines.append(
                f"| {a.name} | {_pct(a.point)} | {ci} | {width} "
                f"| {a.covered_fraction:.1%} | {a.release_status} | {reasons} |"
            )
        lines.append("")

    lines.append("## 7. 分层口径备注")
    lines.append("")
    seen_notes = set()
    for e in result.stratum_estimates:
        for note in e.notes:
            if note not in seen_notes:
                seen_notes.add(note)
                lines.append(f"- {e.week}/{e.stratum}：{note}")
    lines.append("")

    from .scenarios import coverage_gap_by_site, required_effort

    lines.append("## 8. 解除发布障碍所需核查量")
    lines.append("")
    lines.append("| 汇总 | 待覆盖单位 | 确证缺口 | 未知份额 | 宽度缺口 | 拦截原因 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for row in required_effort(result):
        lines.append(
            f"| {row['name']} | {row['uncovered_to_cover']} "
            f"| {row['verified_shortfall']} | {_pct(row['unknown_share'])} "
            f"| {row['width_gap'] if row['width_gap'] is not None else '—'} "
            f"| {'；'.join(row['blocking_reasons'])} |"
        )
    lines.append("")
    lines.append("### 站点时段缺口（补抽只能用剩余时段）")
    lines.append("")
    lines.append("| 站点 | 总时段 | 已约占用 | 补抽余量 | 排不上的补抽 | 顺延的已约 |")
    lines.append("|---|---:|---:|---:|---|---|")
    for site, row in sorted(coverage_gap_by_site(result).items()):
        lines.append(
            f"| {site} | {row['total_slots']} | {row['reserved_appointments']} "
            f"| {row['residual_slots']} | {', '.join(row.get('unscheduled_units', [])) or '—'} "
            f"| {', '.join(row['deferred_appointments']) or '—'} |"
        )
    lines.append("")
    lines.append("### 可补约访的无应答单位（π 不变，仅把未知转成证据）")
    lines.append("")
    if result.nonresponse_followup_units:
        lines.append("| 单位 | 周 | 分层 | 站点 | 现结局 |")
        lines.append("|---|---:|---|---|---|")
        for r in result.nonresponse_followup_units:
            lines.append(
                f"| {r['unit']} | {r['week']} | {r['stratum']} | {r['site']} | {r['outcome']} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    # 一句话结论
    publishable = [a.name for a in result.aggregates.values()
                   if a.release_status == "可发布"]
    lines.append("## 9. 本期发布结论")
    lines.append("")
    if publishable:
        lines.append(f"- 可发布层级：{', '.join(publishable)}")
    else:
        lines.append("- 当期（第34周及合并分组）**没有任何层级达到发布门禁**，简报暂不能采用新值。")
    lines.append("- 第32、33周为历史已发布值，本期不重新编辑、不回改。")
    lines.append(
        "- 首要障碍是无应答推高的未知份额（区间宽度超限），其次是后补单位覆盖缺口；"
        "仅当补抽与新增确证把对应汇总的覆盖率补到 100%、确证数达标且宽度≤"
        f"{result.release['max_width']} 时，才能用新草稿替换。"
    )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="复核抽样滚动纠偏流水线")
    ap.add_argument("records", type=Path, help="records.json 路径")
    ap.add_argument("--json", type=Path, help="结构化 JSON 输出路径")
    ap.add_argument("--markdown", type=Path, help="文字简报输出路径")
    ap.add_argument("--planning-week", type=int, default=None)
    args = ap.parse_args()

    payload = load_payload(args.records)
    result = run_pipeline(payload, planning_week=args.planning_week)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result_to_dict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result), encoding="utf-8")
    if not args.json and not args.markdown:
        print(render_markdown(result))


if __name__ == "__main__":
    main()
