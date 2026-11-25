"""分析流水线与给编辑看的结构化报告。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .estimation import (
    AggregateEstimate,
    StratumEstimate,
    apply_release_gate,
    build_aggregates,
    estimate_stratum,
)
from .frames import RevisionLedger, build_ledger
from .sampling import Schedule, build_sample, plan_supplements
from .scenarios import (
    Scenario,
    coverage_gap_by_site,
    nonresponse_followups,
    required_effort,
    run_scenario,
    scheduled_supplement_units,
)
from .validation import ValidationReport, validate_payload


@dataclass
class AnalysisResult:
    validation: ValidationReport
    ledger: RevisionLedger
    registry: dict
    stratum_estimates: list[StratumEstimate]
    aggregates: dict[str, AggregateEstimate]
    schedule: Schedule
    release: dict
    planning_week: int
    frame_versions: list[dict[str, Any]] = field(default_factory=list)
    reuse: list[dict[str, Any]] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    nonresponse_followup_units: list[dict[str, Any]] = field(default_factory=list)
    source_payload: dict | None = None


def _frame_version_rows(ledger: RevisionLedger) -> list[dict[str, Any]]:
    return ledger.version_rows()


def _blocking_priorities(aggregates: dict[str, AggregateEstimate],
                         release: dict) -> dict[tuple[int, str], int]:
    """层优先级：该层是否卡住其所属发布分组（覆盖缺口/确证不足/宽度超限）。

    数字越小越优先。补抽只投到“确有机会解除当前发布障碍”的层——
    即未覆盖单位所在、且其分组当前被门禁拦截的层。
    """
    groups = release["groups"]

    def group_of(stratum: str) -> str:
        return next(g for g in groups if g in stratum)

    blocked_groups = {
        g for g in groups
        if aggregates[f"组:{g}"].release_status == "暂不能采用"
    }
    prio: dict[tuple[int, str], int] = {}
    for key_agg, agg in aggregates.items():
        if agg.level != "week_group":
            continue
        wk = agg.weeks[0]
        for st in agg.strata:
            if group_of(st) in blocked_groups and agg.blocking_reasons:
                # 覆盖缺口最优先，其次确证数/宽度问题
                reason_rank = 0 if any("未覆盖" in r for r in agg.blocking_reasons) else 1
                prio[(wk, st)] = reason_rank
    return prio


def _reuse_rows(ledger: RevisionLedger,
                registry: dict[tuple, object]) -> list[dict[str, Any]]:
    """说明已提请核查的记录代表哪一版总体、哪部分可复用、哪部分要补抽。"""
    rows = []
    for key in ledger.revised_strata():
        bucket = ledger.frames[key]
        if not bucket.duplicates and not bucket.late_units:
            continue
        sample = registry[key]
        random_eff = {u.effective_segment for u in sample.random_units()}
        reusable = sorted(random_eff & bucket.covered_units())
        needs_supplement = sorted(bucket.uncovered_units())
        dup_note = [f"{dup}→{canon}" for dup, canon in bucket.duplicates.items()]
        rows.append({
            "week": bucket.week,
            "stratum": bucket.stratum,
            "frozen_version": f"{bucket.week}/{bucket.stratum}",
            "represents": (
                f"冻结版本 N={bucket.freeze_n}，n={bucket.draw_n}；"
                f"现行版本 N={bucket.current_n}"
            ),
            "duplicate_mapping": dup_note,
            "reusable_effective_segments": reusable,
            "reusable_count": len(reusable),
            "supplement_required_units": needs_supplement,
            "frozen_probabilities_unchanged": True,
            "note": (
                "原抽取名单的 n/N 保持下达当时的值；重复行按物理段并入，"
                "后补单位在旧抽取下机会为 0，只能补抽"
            ),
        })
    return rows


def run_pipeline(payload: dict[str, Any], planning_week: int | None = None) -> AnalysisResult:
    validation = validate_payload(payload)
    if not validation.ok:
        raise ValueError(f"输入契约不通过: {validation.errors}")

    release = payload["release"]
    planning_week = planning_week or max(s["week"] for s in payload["staff"])

    ledger = build_ledger(payload)
    registry = build_sample(ledger, payload)

    estimates = [
        estimate_stratum(registry[key], confidence=release["confidence"])
        for key in ledger.strata()
    ]
    aggregates = build_aggregates(estimates, registry, release)
    for key, agg in aggregates.items():
        apply_release_gate(agg, release, release["published_through"])

    priorities = _blocking_priorities(aggregates, release)
    schedule = plan_supplements(ledger, registry, payload, planning_week, priorities)
    reuse = _reuse_rows(ledger, registry)

    # 情景：排程内能安排的补抽全部完成，分别按到场/未到场给两端
    scheduled = scheduled_supplement_units(schedule)
    scenarios: list[Scenario] = []
    if scheduled:
        for assume in ("到场", "未到场"):
            scenarios.append(run_scenario(
                payload,
                name=f"补抽完成（假设补抽单位均{assume}）",
                description=(
                    f"第 {planning_week} 周排程内 {len(scheduled)} 个补抽单位取得回执；"
                    "旧冻结名单概率不变，仅新增独立补抽 freeze"
                ),
                outcome_overrides={u: assume for u in scheduled},
                extra_draw_units=scheduled,
            ))

    # 理想情景：若时段充足，把全部未覆盖后补单位都补上（用于量化还差多少核查量）
    all_late = [
        seg for bucket in ledger.frames.values()
        for seg in bucket.uncovered_units()
        if bucket.current_units[seg].origin == "后补"
    ]
    missing = sorted(set(all_late) - set(scheduled))
    if missing:
        full = sorted(set(scheduled) | set(missing))
        scenarios.append(run_scenario(
            payload,
            name="理想情景：全部后补缺口均补齐（假设到场）",
            description=(
                f"不受当前时段约束，{len(full)} 个后补单位全部补抽到；"
                f"其中 {len(missing)} 个在现有时段下排不上，用于说明还需追加的访员时段"
            ),
            outcome_overrides={u: "到场" for u in full},
            extra_draw_units=full,
        ))

    followups = nonresponse_followups(ledger, registry)

    # 可达性演示：假设能为东站点追加时段，对周34东-活动无应答单位补约访 + 补抽后补。
    # 用于展示“宽度障碍可由补约访解除、覆盖障碍只能由补抽解除”，以及跨站点缺口的连带拦截。
    east_activity_fu = {
        r["unit"]: "到场" for r in followups
        if r["week"] == max(release["valid_frame_weeks"]) and r["stratum"] == "东-活动"
    }
    east_late = [seg for seg in scheduled if "东-活动" in seg]
    if east_activity_fu or east_late:
        overrides = dict(east_activity_fu)
        overrides.update({u: "到场" for u in east_late})
        scenarios.append(run_scenario(
            payload,
            name="追加时段演示：东-活动补约访+补抽（假设到场）",
            description=(
                "假设为东站点追加访员时段：周34东-活动的无应答随机单位补约访"
                "（π 不变，未知份额转证据）并补抽其后补单位；"
                "用于说明宽度障碍与覆盖障碍分别由哪类核查解除"
            ),
            outcome_overrides=overrides,
            extra_draw_units=east_late,
        ))

    return AnalysisResult(
        validation=validation,
        ledger=ledger,
        registry=registry,
        stratum_estimates=estimates,
        aggregates=aggregates,
        schedule=schedule,
        release=release,
        planning_week=planning_week,
        frame_versions=_frame_version_rows(ledger),
        reuse=reuse,
        scenarios=scenarios,
        nonresponse_followup_units=followups,
        source_payload=payload,
    )


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------

def _agg_to_dict(agg: AggregateEstimate) -> dict[str, Any]:
    d = asdict(agg)
    for k in ("point", "unknown_share", "ci_low", "ci_high",
              "interval_width", "covered_fraction"):
        if d[k] is not None:
            d[k] = round(d[k], 4)
    return d


def _est_to_dict(e: StratumEstimate) -> dict[str, Any]:
    d = asdict(e)
    for k, v in list(d.items()):
        if isinstance(v, float):
            d[k] = round(v, 4)
    return d


def result_to_dict(result: AnalysisResult) -> dict[str, Any]:
    return {
        "planning_week": result.planning_week,
        "release_contract": result.release,
        "frame_versions": result.frame_versions,
        "reuse_and_supplement": result.reuse,
        "stratum_estimates": [_est_to_dict(e) for e in result.stratum_estimates],
        "aggregates": {k: _agg_to_dict(v) for k, v in result.aggregates.items()},
        "supplement_schedule": [
            {
                "site": p.site,
                "total_slots": p.total_slots,
                "reserved_for_appointments": p.reserved_appointments,
                "residual_slots": p.residual_slots,
                "requested": p.requested,
                "scheduled": p.scheduled,
                "unscheduled": p.unscheduled,
                "deferred_appointments": p.deferred_appointments,
            }
            for p in result.schedule.plans.values()
        ],
        "scenarios": [
            {
                "name": s.name,
                "description": s.description,
                "newly_resolved": s.newly_resolved,
                "extra_draws": s.extra_draws,
                "aggregates": {k: _agg_to_dict(v) for k, v in s.aggregates.items()},
            }
            for s in result.scenarios
        ],
        "required_effort": required_effort(result),
        "coverage_gap_by_site": coverage_gap_by_site(result),
        "nonresponse_followup_units": result.nonresponse_followup_units,
    }
