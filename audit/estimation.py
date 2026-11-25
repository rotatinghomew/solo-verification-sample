"""加权比例估计、未知影响区间、整群相关与发布门禁。

估计口径
--------
- 目标量：适用总体中确证到场（核查结局=到场）的参与段比例。
- 随机样本按冻结版本的包含概率 π=n/N 取基础权重 w=N/n；重复申报折到同一物理段，
  一个物理段只贡献一次，避免重复报送放大权重。
- 失联 / 拒绝出证属于无应答：其真实出勤状态没有可估计证据，不做插补，
  以“未知权重份额”给出悲观/乐观边界，区间里明确保留这部分未知影响。
- 后补单位在旧冻结抽取下 π=0，随机估计只覆盖“可代表的版本总体”；
  未覆盖份额单独报告，发布门禁会据此拦截。
- 多站点共享同一原始活动时，参与段在 shared_activity 整群内相关；除分层 SRS
  方差外同时给出整群稳健方差，并以更宽（保守）者驱动发布判定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from scipy.stats import t, norm

from .sampling import StratumSample


@dataclass
class StratumEstimate:
    week: int
    stratum: str
    site: str
    frozen_n: int
    current_n: int
    covered_n: int          # 现行总体中在冻结抽取下有正抽样机会的单位数
    uncovered_n: int        # 后补等无抽样机会单位数
    sampled_effective: int  # 按物理段去重后的随机单位数
    evidence_n: int         # 有可估计证据（到场/未到场）的随机单位
    verified_attended: int
    nonresponse_n: int      # 失联/拒绝出证
    pending_n: int          # 尚无回执
    point: float | None     # 应答者加权比例（对覆盖子总体的估计）
    unknown_share: float    # 无应答+待核在估计口径中的权重份额
    half_width_srs: float | None
    half_width_cluster: float | None
    half_width: float | None
    ci_low: float | None
    ci_high: float | None
    interval_width: float | None
    covered_fraction: float
    notes: list[str] = field(default_factory=list)

    @property
    def estimable(self) -> bool:
        return self.evidence_n > 0 and self.covered_fraction > 0


def _z(confidence: float) -> float:
    return float(norm.ppf(1 - (1 - confidence) / 2))


def estimate_stratum(sample: StratumSample, confidence: float = 0.9) -> StratumEstimate:
    bucket = sample.frame
    rnd = sample.random_units()  # 已按物理段去重、保留随机身份
    n = bucket.draw_n or 0
    N = bucket.freeze_n or 0

    def unit_weight(u) -> float:
        """单位权重 = 1/π；原冻结单位与补抽池单位各自按其 freeze 的 n/N。"""
        if u.is_supplement and u.n and u.N:
            return u.N / u.n
        return (N / n) if n else 0.0

    evidence = [u for u in rnd if u.has_evidence]
    attended = [u for u in rnd if u.outcome == "到场"]
    nonresp = [u for u in rnd if u.is_nonresponse]
    pending = [u for u in rnd if u.pending]

    covered = bucket.covered_units()
    current = bucket.current_units
    covered_n, current_n = len(covered), len(current)
    uncovered_n = current_n - covered_n
    covered_fraction = covered_n / current_n if current_n else 0.0

    notes: list[str] = []
    dup_drawn = sorted({u.unit for u in rnd if u.canonical})
    if dup_drawn:
        notes.append(f"重复申报中签行 {dup_drawn} 已并入 canonical 物理段，不重复计权")
    if uncovered_n:
        notes.append(f"{uncovered_n} 个现行单位（后补）在冻结抽取下抽样机会为 0，随机估计不覆盖")
    supp = [u for u in rnd if u.is_supplement]
    if supp:
        notes.append(
            f"补抽单位 {sorted(u.unit for u in supp)} 按独立补抽 freeze 的 π 计权，"
            "原冻结名单 n/N 不变"
        )
    if any(u.targeted_only for u in sample.units):
        notes.append("指定核查结果不进入随机分子分母")

    if not evidence or not n:
        return StratumEstimate(
            bucket.week, bucket.stratum, bucket.site, N, current_n, covered_n,
            uncovered_n, len(rnd), 0, 0, len(nonresp), len(pending),
            None, 1.0, None, None, None, None, None, None,
            covered_fraction, notes,
        )

    # 加权 HT 口径：两阶段（原冻结 + 补抽池）各自权重
    w_ev = sum(unit_weight(u) for u in evidence)
    w_att = sum(unit_weight(u) for u in attended)
    w_unknown = sum(unit_weight(u) for u in nonresp + pending)
    w_total = sum(unit_weight(u) for u in rnd)
    point = w_att / w_ev
    unknown_share = w_unknown / w_total

    # --- SRS 比例方差（含 FPC）：原冻结部分主导，补抽池为全数核查时方差贡献为 0 ---
    base_ev = [u for u in evidence if not u.is_supplement]
    m = len(base_ev) or len(evidence)
    p = point
    var_srs = (1 - n / N) * p * (1 - p) / max(m - 1, 1)
    tdf = t.ppf(1 - (1 - confidence) / 2, df=max(m - 1, 1))
    hw_srs = tdf * math.sqrt(var_srs)

    # --- 整群稳健方差：按 shared_activity 汇总加权残差 ---
    pop_clusters = {u.shared_activity for u in bucket.frozen_units.values()
                    if u.shared_activity is not None}
    C_pop = len(pop_clusters) or 1
    cluster_sum: dict[str, float] = {}
    for u in evidence:
        info = current.get(u.effective_segment)
        cluster = info.shared_activity if info is not None else None
        if cluster is None:
            cluster = f"__singleton__:{u.effective_segment}"
            if not any("共享活动归属不可证实" in x for x in notes):
                notes.append("后补/无归属单位按独立 singleton 处理，整群相关性保留未知")
        w = unit_weight(u)
        r = w * (1 - p) if u.outcome == "到场" else w * (0 - p)
        cluster_sum[cluster] = cluster_sum.get(cluster, 0.0) + r
    c = len(cluster_sum)
    if c >= 2:
        raw = sum(v * v for v in cluster_sum.values())
        fpc = max(1.0 - c / C_pop, 0.0)
        var_total = (c / (c - 1)) * raw * fpc
        var_cluster = var_total / w_ev ** 2
        hw_cluster = t.ppf(1 - (1 - confidence) / 2, df=c - 1) * math.sqrt(max(var_cluster, 0.0))
    else:
        hw_cluster = hw_srs

    hw = max(hw_srs, hw_cluster)
    low0 = point * (1 - unknown_share)
    high0 = point * (1 - unknown_share) + unknown_share
    ci_low = max(0.0, low0 - hw)
    ci_high = min(1.0, high0 + hw)

    return StratumEstimate(
        bucket.week, bucket.stratum, bucket.site, N, current_n, covered_n,
        uncovered_n, len(rnd), len(evidence), len(attended), len(nonresp), len(pending),
        point, unknown_share, hw_srs, hw_cluster, hw,
        ci_low, ci_high, ci_high - ci_low, covered_fraction, notes,
    )


@dataclass
class AggregateEstimate:
    name: str
    level: str                # overall | group | week | week_group
    weeks: list[int]
    strata: list[str]
    current_n: int
    covered_n: int
    uncovered_n: int
    evidence_n: int
    verified_attended: int
    point: float | None
    unknown_share: float
    ci_low: float | None
    ci_high: float | None
    interval_width: float | None
    covered_fraction: float
    blocking_reasons: list[str] = field(default_factory=list)
    release_status: str = "未判定"

    @property
    def estimable(self) -> bool:
        return self.point is not None and self.covered_fraction > 0


def _cluster_dataset(estimates: list[StratumEstimate],
                     registry: dict[tuple[int, str], StratumSample]):
    """构造聚合域内的整群数据集：返回 (cluster_id, 加权残差贡献, 层内整群FPC信息)。

    同一 shared_activity 跨两个层出现时，其残差和进入同一个 cluster_id，
    这样跨层正相关（共同活动）不会被独立相加假设掩盖。
    """
    rows: list[tuple[str, float]] = []
    pop_cluster_count = 0
    for e in estimates:
        sample = registry[(e.week, e.stratum)]
        bucket = sample.frame
        n, N = bucket.draw_n or 0, bucket.freeze_n or 0

        def _w(u, n=n, N=N):
            if u.is_supplement and u.n and u.N:
                return u.N / u.n
            return (N / n) if n else 0.0

        pop_cluster_count += len({u.shared_activity for u in bucket.frozen_units.values()
                                  if u.shared_activity})
        if not e.estimable:
            continue
        p = e.point
        for u in sample.random_units():
            if not u.has_evidence:
                continue
            info = bucket.current_units.get(u.effective_segment)
            cid = info.shared_activity if info is not None and info.shared_activity \
                else f"__singleton__:{e.week}:{e.stratum}:{u.effective_segment}"
            w = _w(u)
            r = w * (1 - p) if u.outcome == "到场" else w * (0 - p)
            rows.append((cid, r))
    return rows, pop_cluster_count


def _aggregate(name: str, level: str, weeks: list[int], strata: list[str],
               estimates: list[StratumEstimate],
               registry: dict[tuple[int, str], StratumSample],
               confidence: float) -> AggregateEstimate:
    current_n = sum(e.current_n for e in estimates)
    covered_n = sum(e.covered_n for e in estimates)
    uncovered_n = sum(e.uncovered_n for e in estimates)
    evidence_n = sum(e.evidence_n for e in estimates)
    attended = sum(e.verified_attended for e in estimates)
    covered_fraction = covered_n / current_n if current_n else 0.0
    usable = [e for e in estimates if e.estimable]

    if not usable:
        return AggregateEstimate(name, level, weeks, strata, current_n, covered_n,
                                 uncovered_n, evidence_n, attended, None, 1.0, None, None,
                                 None, covered_fraction)

    wsum = sum(e.covered_n for e in usable)
    point = sum(e.point * e.covered_n for e in usable) / wsum
    unknown_share = sum(e.unknown_share * e.covered_n for e in usable) / wsum
    z = _z(confidence)

    # SRS 合并方差（层间独立），按覆盖规模权重
    var_srs = sum(
        ((e.covered_n / wsum) ** 2) * (e.half_width_srs / z) ** 2
        for e in usable if e.half_width_srs is not None
    )
    hw_srs = z * math.sqrt(var_srs)

    # 整群稳健：在聚合域内按整群重新汇总，跨层共享活动的协方差自然落在同一群内
    rows, pop_c = _cluster_dataset(usable, registry)
    sums: dict[str, float] = {}
    for cid, r in rows:
        sums[cid] = sums.get(cid, 0.0) + r
    c = len(sums)
    if c >= 2:
        raw = sum(v * v for v in sums.values())
        fpc = max(1.0 - c / max(pop_c, 1), 0.0)
        # 比例口径：分母用覆盖子总体规模的权重和量纲，按 wsum 缩放
        scale = 1.0 / wsum
        var_cluster = scale ** 2 * (c / (c - 1)) * raw * fpc
        hw_cluster = t.ppf(1 - (1 - confidence) / 2, df=c - 1) * math.sqrt(max(var_cluster, 0.0))
    else:
        hw_cluster = hw_srs

    hw = max(hw_srs, hw_cluster)
    low0 = point * (1 - unknown_share)
    high0 = point * (1 - unknown_share) + unknown_share
    ci_low = max(0.0, low0 - hw)
    ci_high = min(1.0, high0 + hw)
    return AggregateEstimate(
        name, level, weeks, strata, current_n, covered_n, uncovered_n,
        evidence_n, attended, point, unknown_share, ci_low, ci_high,
        ci_high - ci_low, covered_fraction,
    )


def build_aggregates(estimates: list[StratumEstimate],
                     registry: dict[tuple[int, str], StratumSample],
                     release: dict) -> dict[str, AggregateEstimate]:
    weeks = sorted(release["valid_frame_weeks"])
    groups = release["groups"]
    confidence = release["confidence"]
    out: dict[str, AggregateEstimate] = {}

    def group_of(stratum: str) -> str:
        return next(g for g in groups if g in stratum)

    out["总体"] = _aggregate("总体", "overall", weeks,
                             sorted({e.stratum for e in estimates}),
                             estimates, registry, confidence)
    for g in groups:
        members = [e for e in estimates if group_of(e.stratum) == g]
        out[f"组:{g}"] = _aggregate(g, "group", weeks,
                                    sorted({e.stratum for e in members}),
                                    members, registry, confidence)
    for wk in weeks:
        members = [e for e in estimates if e.week == wk]
        out[f"周{wk}:总体"] = _aggregate(f"第{wk}周总体", "week", [wk],
                                         sorted({e.stratum for e in members}),
                                         members, registry, confidence)
        for g in groups:
            gm = [e for e in members if group_of(e.stratum) == g]
            out[f"周{wk}:{g}"] = _aggregate(f"第{wk}周-{g}", "week_group", [wk],
                                            sorted({e.stratum for e in gm}),
                                            gm, registry, confidence)
    return out


def apply_release_gate(agg: AggregateEstimate, release: dict,
                       published_through: int) -> AggregateEstimate:
    """按发布契约判定一个汇总能否发布。历史已发布周不再重新编辑。"""
    if all(wk <= published_through for wk in agg.weeks):
        agg.release_status = "历史已发布（不重新编辑）"
        return agg

    reasons: list[str] = []
    if not agg.estimable:
        reasons.append("没有可估计证据的层，比例保留未知")
    if agg.covered_fraction < 1.0:
        reasons.append(
            f"现行总体存在未覆盖单位 {agg.uncovered_n} 个"
            f"（覆盖率 {agg.covered_fraction:.1%}）"
        )
    if agg.evidence_n < release["minimum_verified"]:
        reasons.append(
            f"确证单位 {agg.evidence_n} 低于最低确证数 {release['minimum_verified']}"
        )
    if agg.interval_width is None or agg.interval_width > release["max_width"]:
        width = "不可计算" if agg.interval_width is None else f"{agg.interval_width:.3f}"
        reasons.append(f"区间宽度 {width} 超过上限 {release['max_width']:.2f}")

    agg.blocking_reasons = reasons
    agg.release_status = "可发布" if not reasons else "暂不能采用"
    return agg
