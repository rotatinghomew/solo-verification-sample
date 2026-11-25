"""抽取登记、有效抽样机会与访员时段排程。

口径要点
--------
1. 回执是对“抽取单位”的核查结果；同一物理段的多份登记不产生多次独立抽样。
   重复申报行若已中签，其结果按可证实的映射归到 canonical 物理段。
2. 同一单位同时出现在随机名单与指定名单时，它保留随机样本身份（可进随机估计），
   指定核查不改变任何随机记录的概率。
3. 临时指定的异常核查只在该单位本身具备合规抽样机会时才允许并入随机结果；
   纯指定单位永远只服务于核查线索，不填进随机分子分母。
4. 已约核查（appointment）占用访员时段且不可取消；补抽只能使用剩余时段，
   且访员只能处理本服务点记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import comb

from .frames import RevisionLedger, StratumFrame
from .validation import EVIDENCE_OUTCOMES, RANDOM_MODE, TARGETED_MODE

# 无应答结局：证据保留未知
NONRESPONSE = frozenset({"失联", "拒绝出证"})


@dataclass
class SampledUnit:
    unit: str
    stratum_key: tuple[int, str]
    in_random: bool
    targeted_only: bool
    appointment: bool
    n: int | None
    N: int | None
    outcome: str | None          # 原始回执
    canonical: str | None        # 若该行为重复申报行，映射到的物理段
    effective_segment: str       # 解释时落到的物理参与段
    freeze: str = ""             # 来源 freeze，补抽 freeze 与原冻结名单区分
    is_supplement: bool = False  # 是否来自补抽池（后补单位）

    @property
    def pending(self) -> bool:
        return self.outcome is None

    @property
    def has_evidence(self) -> bool:
        return self.outcome in EVIDENCE_OUTCOMES

    @property
    def is_nonresponse(self) -> bool:
        return self.outcome in NONRESPONSE


@dataclass
class StratumSample:
    key: tuple[int, str]
    frame: StratumFrame
    units: list[SampledUnit] = field(default_factory=list)

    def random_units(self) -> list[SampledUnit]:
        """具备随机抽样机会的单位（含双身份单位），按物理段去重。"""
        seen: dict[str, SampledUnit] = {}
        for u in self.units:
            if u.in_random and u.effective_segment not in seen:
                seen[u.effective_segment] = u
        return list(seen.values())

    def targeted_only_units(self) -> list[SampledUnit]:
        return [u for u in self.units if u.targeted_only]

    def pending_appointments(self) -> list[SampledUnit]:
        return [u for u in self.units if u.appointment and u.pending]


def build_sample(ledger: RevisionLedger, payload: dict) -> dict[tuple[int, str], StratumSample]:
    outcomes = {r["unit"]: r["outcome"] for r in payload["results"]}
    registry: dict[tuple[int, str], StratumSample] = {
        key: StratumSample(key=key, frame=bucket) for key, bucket in ledger.frames.items()
    }
    random_units: dict[tuple[int, str], set[str]] = {}
    for row in payload["draws"]:
        key = (row["week"], row["stratum"])
        if row["mode"] == RANDOM_MODE:
            random_units.setdefault(key, set()).add(row["unit"])

    for row in payload["draws"]:
        key = (row["week"], row["stratum"])
        bucket = ledger.frames[key]
        unit = row["unit"]
        is_supp = row["mode"] == RANDOM_MODE and "补抽" in row["freeze"]
        in_random = row["mode"] == RANDOM_MODE or unit in random_units.get(key, set())
        targeted_only = row["mode"] == TARGETED_MODE and unit not in random_units.get(key, set())
        canonical = bucket.duplicates.get(unit)
        effective = canonical if canonical is not None else unit
        registry[key].units.append(SampledUnit(
            unit=unit,
            stratum_key=key,
            in_random=in_random,
            targeted_only=targeted_only,
            appointment=bool(row["appointment"]),
            n=row["n"],
            N=row["N"],
            outcome=outcomes.get(unit),
            canonical=canonical,
            effective_segment=effective,
            freeze=row["freeze"],
            is_supplement=is_supp,
        ))

    # 重复行的回执归并：canonical 若自身无证据而映射行有，采用映射行的首个可证实结果
    for sample in registry.values():
        by_eff: dict[str, list[SampledUnit]] = {}
        for u in sample.units:
            by_eff.setdefault(u.effective_segment, []).append(u)
        for eff, group in by_eff.items():
            if len(group) < 2:
                continue
            evidence = next((g.outcome for g in group if g.outcome in EVIDENCE_OUTCOMES), None)
            if evidence is None:
                evidence = next((g.outcome for g in group if g.outcome is not None), None)
            for g in group:
                if g.outcome is None and evidence is not None:
                    g.outcome = evidence
    return registry


# --------------------------------------------------------------------------
# 补抽排程
# --------------------------------------------------------------------------

@dataclass
class SupplementPlan:
    site: str
    week: int
    total_slots: int
    reserved_appointments: int
    residual_slots: int
    requested: list[str] = field(default_factory=list)       # 希望补抽的单位
    scheduled: list[str] = field(default_factory=list)       # 时段内可安排
    unscheduled: list[str] = field(default_factory=list)     # 时段不足被卡住
    deferred_appointments: list[str] = field(default_factory=list)  # 已约但本周期无时段


@dataclass
class Schedule:
    plans: dict[str, SupplementPlan]
    appointments: list[SampledUnit]

    @property
    def all_scheduled_supplements(self) -> list[str]:
        out: list[str] = []
        for p in self.plans.values():
            out.extend(p.scheduled)
        return out

    @property
    def all_unscheduled(self) -> list[str]:
        out: list[str] = []
        for p in self.plans.values():
            out.extend(p.unscheduled)
        return out


def plan_supplements(
    ledger: RevisionLedger,
    registry: dict[tuple[int, str], StratumSample],
    payload: dict,
    planning_week: int,
    priorities: dict[tuple[int, str], int] | None = None,
) -> Schedule:
    """按服务点分配补查时段。

    priorities: 各冻结层补抽需求的优先级（数字越小越优先）；用于把剩余时段
    优先投到“确有机会解除发布障碍”的层。
    """
    # 1) 汇总每点可用时段
    slots: dict[str, int] = {}
    for row in payload["staff"]:
        if row["week"] != planning_week:
            continue
        slots[row["site"]] = slots.get(row["site"], 0) + row["slots"]

    # 2) 已约且尚未回执的核查必须保留，且只能由本点访员处理
    pending_by_site: dict[str, list[SampledUnit]] = {}
    for sample in registry.values():
        for u in sample.pending_appointments():
            site = ledger.frames[u.stratum_key].site
            pending_by_site.setdefault(site, []).append(u)

    plans: dict[str, SupplementPlan] = {}
    all_sites = sorted(set(slots) | set(pending_by_site))
    for site in all_sites:
        total = slots.get(site, 0)
        pending = pending_by_site.get(site, [])
        reserved = min(total, len(pending))
        deferred = [u.unit for u in pending[total:]] if len(pending) > total else []
        plans[site] = SupplementPlan(
            site=site, week=planning_week, total_slots=total,
            reserved_appointments=reserved,
            residual_slots=max(0, total - len(pending)),
            deferred_appointments=deferred,
        )

    # 3) 补抽需求：现行总体中在冻结抽取下没有正抽样机会、且尚未在任何名单中的单位
    already: set[str] = {u.unit for sample in registry.values() for u in sample.units}
    requests: dict[str, list[tuple[int, tuple[int, str], str]]] = {}
    for key, bucket in ledger.frames.items():
        for seg in sorted(bucket.uncovered_units()):
            if seg in already:
                continue
            prio = (priorities or {}).get(key, 99)
            requests.setdefault(bucket.site, []).append((prio, key, seg))

    for site, reqs in requests.items():
        plan = plans.setdefault(site, SupplementPlan(site, planning_week, 0, 0, 0))
        reqs.sort(key=lambda x: (x[0], x[1], x[2]))
        plan.requested = [seg for _, _, seg in reqs]
        capacity = plan.residual_slots
        for _, _, seg in reqs:
            if capacity > 0:
                plan.scheduled.append(seg)
                capacity -= 1
            else:
                plan.unscheduled.append(seg)

    appointments = [u for lst in pending_by_site.values() for u in lst]
    return Schedule(plans=plans, appointments=appointments)


def supplement_draw_probability(frame: StratumFrame, extra_n: int) -> float:
    """补抽在未覆盖单位中等概率不放回抽取时，单个未覆盖单位的中签概率。"""
    pool = len(frame.uncovered_units())
    if pool == 0:
        return 0.0
    return min(extra_n, pool) / pool


def combined_coverage(frame: StratumFrame, segment: str, extra_n: int) -> float:
    """冻结抽取与本点补抽合并后，现行单位的覆盖概率（两阶段独立近似）。"""
    base = frame.frozen_coverage(segment)
    if segment not in frame.uncovered_units():
        return base
    p2 = supplement_draw_probability(frame, extra_n)
    return 1.0 - (1.0 - base) * (1.0 - p2)
