"""滚动复核情景分析：新增核查能否解除当前发布障碍。

规则
----
- 已发出的随机名单及其 n/N 永不改写；新增核查只作用于“冻结抽取下未覆盖”的单位
  （补抽，给它们抽样机会），或对已抽中但仍待核的单位取得回执（不改变原包含概率，
  只把未知份额转成可估计证据）。
- 情景通过“结局覆盖表 + 补抽单位表”驱动完整估计流水线重算，不使用任何摊薄系数，
  每个数字都能按相同口径复算。
- 情景结果只用于判断“哪一级汇总可以改用新草稿”，不会回改任何已对外发布的历史值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .estimation import (
    AggregateEstimate,
    apply_release_gate,
    build_aggregates,
    estimate_stratum,
)
from .frames import RevisionLedger, build_ledger
from .sampling import Schedule, build_sample
from .validation import RANDOM_MODE


@dataclass
class Scenario:
    name: str
    description: str
    aggregates: dict[str, AggregateEstimate]
    frame_versions: list[dict] = field(default_factory=list)
    newly_resolved: list[str] = field(default_factory=list)
    extra_draws: list[str] = field(default_factory=list)


def _payload_with_scenario(payload: dict,
                           outcome_overrides: dict[str, str],
                           extra_draw_units: list[str]) -> dict:
    """构造情景资料：覆盖回执结局、把补抽单位作为新一轮随机抽取登记。

    补抽单位来自冻结抽取未覆盖的后补池，以独立 freeze 标签登记，n/N 按补抽池
    固定，与原冻结名单完全分开，旧概率不被改写；frame 保持原样不动。
    """
    import copy

    p = copy.deepcopy(payload)
    late_index = {r["unit"]: r for r in p["revisions"] if r["kind"] == "后补"}

    # 1) 覆盖/补全回执（待核单位取得结局；无应答单位不被强行改写）
    existing = {r["unit"]: r for r in p["results"]}
    planning_week = max(s["week"] for s in p["staff"])
    for unit, outcome in outcome_overrides.items():
        if unit in existing:
            existing[unit]["outcome"] = outcome
        else:
            existing[unit] = {
                "unit": unit, "received_week": planning_week, "outcome": outcome,
            }
    p["results"] = list(existing.values())

    # 2) 补抽：按层登记独立 freeze，池规模 = 该层后补单位总数
    pool_by_stratum: dict[tuple[int, str], list[str]] = {}
    for unit in extra_draw_units:
        rev = late_index.get(unit)
        if rev is None:
            continue
        week = int(unit.split("-")[0])
        pool_by_stratum.setdefault((week, rev["stratum"]), []).append(unit)

    for (week, stratum), units in pool_by_stratum.items():
        pool_size = sum(
            1 for r in p["revisions"]
            if r["kind"] == "后补" and r["stratum"] == stratum
            and int(r["unit"].split("-")[0]) == week
        )
        n2 = len(units)
        for unit in units:
            p["draws"].append({
                "week": week,
                "stratum": stratum,
                "unit": unit,
                "mode": RANDOM_MODE,
                "n": n2,
                "N": max(pool_size, n2),
                "freeze": f"{week}/{stratum}#补抽",
                "appointment": False,
            })
    return p


def run_scenario(base_payload: dict,
                 name: str,
                 description: str,
                 outcome_overrides: dict[str, str],
                 extra_draw_units: list[str]) -> Scenario:
    p = _payload_with_scenario(base_payload, outcome_overrides, extra_draw_units)
    ledger = build_ledger(p)
    registry = build_sample(ledger, p)
    release = p["release"]
    estimates = [estimate_stratum(registry[key], confidence=release["confidence"])
                 for key in ledger.strata()]
    aggregates = build_aggregates(estimates, registry, release)
    for agg in aggregates.values():
        apply_release_gate(agg, release, release["published_through"])
    return Scenario(
        name=name,
        description=description,
        aggregates=aggregates,
        frame_versions=ledger.version_rows(),
        newly_resolved=sorted(outcome_overrides),
        extra_draws=sorted(extra_draw_units),
    )


def scheduled_supplement_units(schedule: Schedule) -> list[str]:
    return schedule.all_scheduled_supplements


def pending_appointment_units(schedule: Schedule) -> list[str]:
    return [u.unit for u in schedule.appointments if u.pending]


def required_effort(result) -> list[dict]:
    """量化每个未通过门禁汇总解除障碍所需的核查量。

    返回三类缺口：
    - coverage：现行总体中仍无抽样机会的单位数（必须补抽才能覆盖）；
    - verified：距最低确证数还差多少已确证单位；
    - width：当前区间宽度超出上限多少，以及主要由无应答未知份额驱动的部分。
    """
    release = result.release
    rows = []
    for key, agg in result.aggregates.items():
        if "历史已发布" in agg.release_status or agg.release_status == "可发布":
            continue
        width_gap = (
            None if agg.interval_width is None
            else round(max(0.0, agg.interval_width - release["max_width"]), 4)
        )
        rows.append({
            "aggregate": key,
            "name": agg.name,
            "level": agg.level,
            "uncovered_to_cover": agg.uncovered_n,
            "verified_shortfall": max(0, release["minimum_verified"] - agg.evidence_n),
            "unknown_share": round(agg.unknown_share, 4),
            "width_gap": width_gap,
            "blocking_reasons": agg.blocking_reasons,
        })
    return rows


def coverage_gap_by_site(result) -> dict[str, dict]:
    """按站点汇总补抽缺口与可用余量，直接告诉编辑哪点时段不够。"""
    ledger: RevisionLedger = result.ledger
    out: dict[str, dict] = {}
    for plan in result.schedule.plans.values():
        out[plan.site] = {
            "total_slots": plan.total_slots,
            "reserved_appointments": plan.reserved_appointments,
            "residual_slots": plan.residual_slots,
            "deferred_appointments": plan.deferred_appointments,
            "unscheduled_units": plan.unscheduled,
        }
    # 每层未覆盖单位归到站点
    for key, bucket in ledger.frames.items():
        for seg in bucket.uncovered_units():
            row = out.setdefault(bucket.site, {
                "total_slots": 0, "reserved_appointments": 0,
                "residual_slots": 0, "deferred_appointments": [],
                "unscheduled_units": [],
            })
            row.setdefault("uncovered", []).append(seg)
    return out


def nonresponse_followups(ledger: RevisionLedger, registry: dict) -> list[dict]:
    """列出已抽中但无应答（失联/拒绝出证）的随机单位——补约访可把未知份额转成证据。

    这些单位的包含概率不变，补约访只减少未知影响；按站点给出，受剩余时段约束。
    """
    rows = []
    for key, sample in registry.items():
        for u in sample.random_units():
            if u.is_nonresponse:
                rows.append({
                    "unit": u.unit,
                    "week": key[0],
                    "stratum": key[1],
                    "site": ledger.frames[key].site,
                    "outcome": u.outcome,
                    "effective_segment": u.effective_segment,
                })
    return sorted(rows, key=lambda r: (r["site"], r["week"], r["stratum"]))


def followup_scenario(payload: dict, name: str, description: str,
                      unit_outcomes: dict[str, str]):
    """对已抽中无应答单位补约访的情景（只改回执，不动任何 freeze/概率）。"""
    return run_scenario(payload, name, description, unit_outcomes, [])
