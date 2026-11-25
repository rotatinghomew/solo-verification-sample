"""总体版本与修订台账。

两个时间点必须分开：
- 冻结版本（freeze）：随机抽取下达当时的总框，n/N 在该时刻固定，之后永不改写。
- 现行版本（current）：纳入已知周的后补与重复申报判定后的适用总体。

重复申报把多份登记折到同一物理参与段（canonical）。后补单位从已知周进入，
不属于任何旧冻结总体，因此在旧抽取下抽样机会为 0，只能通过补抽覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .validation import RANDOM_MODE


@dataclass(frozen=True)
class FrameUnit:
    segment: str
    week: int
    stratum: str
    site: str
    shared_activity: str | None
    reported: bool
    origin: str  # "冻结" | "后补"
    # 该物理段在冻结总框中对应了几行登记（canonical 自身 + 被并入的重复行）
    frozen_rows: int = 1


@dataclass
class StratumFrame:
    week: int
    stratum: str
    site: str
    freeze_n: int | None = None  # 冻结版本规模 N
    draw_n: int | None = None    # 冻结版本随机抽取数 n
    frozen_units: dict[str, FrameUnit] = field(default_factory=dict)
    # 重复申报：重复行 -> canonical
    duplicates: dict[str, str] = field(default_factory=dict)
    late_units: dict[str, FrameUnit] = field(default_factory=dict)
    # 补抽 freeze 下后补单位的 (n补, N补)；未排上补抽的后补单位不在此表中
    supplement_draws: dict[str, tuple[int, int]] = field(default_factory=dict)

    @property
    def current_units(self) -> dict[str, FrameUnit]:
        """现行适用总体：去重后的冻结单位 + 后补单位。"""
        kept = {s: u for s, u in self.frozen_units.items() if s not in self.duplicates}
        kept.update(self.late_units)
        return kept

    @property
    def current_n(self) -> int:
        return len(self.current_units)

    def covered_units(self) -> set[str]:
        """在冻结随机抽取（含已执行补抽）下具有正抽样机会的现行单位。"""
        return {s for s in self.current_units if self.frozen_coverage(s) > 0.0}

    def uncovered_units(self) -> set[str]:
        return set(self.current_units) - self.covered_units()

    def frozen_coverage(self, segment: str) -> float:
        """现行单位被覆盖的概率。

        冻结框内单位按物理段合并解释（任一中签即覆盖）；
        后补单位只在补抽 freeze 下有机会，概率 n补/N补；未排上补抽则为 0。
        """
        from math import comb

        unit = self.current_units.get(segment)
        if unit is None:
            return 0.0
        if unit.origin == "后补":
            if segment not in self.supplement_draws:
                return 0.0
            n2, N2 = self.supplement_draws[segment]
            return n2 / N2
        if self.draw_n is None or self.freeze_n is None:
            return 0.0
        k = min(unit.frozen_rows, self.freeze_n)
        n, N = self.draw_n, self.freeze_n
        return 1.0 - comb(N - k, n) / comb(N, n)


@dataclass
class RevisionLedger:
    frames: dict[tuple[int, str], StratumFrame]
    revision_rows: list[dict]

    def strata(self) -> list[tuple[int, str]]:
        return sorted(self.frames)

    def revised_strata(self) -> list[tuple[int, str]]:
        weeks = {r["known_week"] for r in self.revision_rows}
        return sorted(k for k in self.frames if any(
            r["kind"] in {"后补", "重复申报"} and k[0] < r["known_week"]
            and k[1] == r["stratum"]
            for r in self.revision_rows
        ))

    def version_rows(self) -> list[dict]:
        """现行/冻结版本对照与覆盖清单，供报告与情景复用。"""
        rows = []
        keys = self.revised_strata() or self.strata()
        for key in keys:
            bucket = self.frames[key]
            rows.append({
                "week": bucket.week,
                "stratum": bucket.stratum,
                "site": bucket.site,
                "frozen_n": bucket.freeze_n,
                "draw_n": bucket.draw_n,
                "current_n": bucket.current_n,
                "duplicates_merged": list(bucket.duplicates.items()),
                "late_units": sorted(bucket.late_units),
                "covered_by_frozen_draw": sorted(bucket.covered_units()),
                "uncovered_units": sorted(bucket.uncovered_units()),
            })
        return rows


def build_ledger(payload: dict) -> RevisionLedger:
    sf: dict[tuple[int, str], StratumFrame] = {}
    for row in payload["frame"]:
        key = (row["week"], row["stratum"])
        bucket = sf.setdefault(key, StratumFrame(row["week"], row["stratum"], row["site"]))
        bucket.frozen_units[row["segment"]] = FrameUnit(
            segment=row["segment"],
            week=row["week"],
            stratum=row["stratum"],
            site=row["site"],
            shared_activity=row["shared_activity"],
            reported=bool(row["reported"]),
            origin="冻结",
        )

    # 冻结抽取参数（随机模式）；补抽 freeze 单独记录后补池单位的机会
    supplement_pools: dict[tuple[int, str], list[tuple[str, int, int]]] = {}
    for row in payload["draws"]:
        if row["mode"] != RANDOM_MODE:
            continue
        bucket = sf[(row["week"], row["stratum"])]
        if "补抽" in row["freeze"]:
            supplement_pools.setdefault((row["week"], row["stratum"]), []).append(
                (row["unit"], row["n"], row["N"])
            )
            continue
        if bucket.freeze_n is None:
            bucket.freeze_n, bucket.draw_n = row["N"], row["n"]

    # 修订
    for rev in payload["revisions"]:
        key = (int(rev["unit"].split("-")[0]), rev["stratum"])
        bucket = sf[key]
        if rev["kind"] == "重复申报":
            dup, canon = rev["unit"], rev["canonical"]
            bucket.duplicates[dup] = canon
        elif rev["kind"] == "后补":
            bucket.late_units[rev["unit"]] = FrameUnit(
                segment=rev["unit"],
                week=key[0],
                stratum=rev["stratum"],
                site=bucket.site,
                shared_activity=None,  # 后补记录未声明共享活动，整群归属保留未知
                reported=bool(rev["reported"]),
                origin="后补",
            )

    # 把重复行数并入 canonical 的 frozen_rows
    for bucket in sf.values():
        merged: dict[str, int] = {}
        for dup in bucket.duplicates:
            canon = bucket.duplicates[dup]
            merged[canon] = merged.get(canon, 1) + 1
        for canon, k in merged.items():
            if canon in bucket.frozen_units:
                old = bucket.frozen_units[canon]
                bucket.frozen_units[canon] = FrameUnit(
                    segment=old.segment, week=old.week, stratum=old.stratum, site=old.site,
                    shared_activity=old.shared_activity, reported=old.reported,
                    origin=old.origin, frozen_rows=k,
                )

    # 补抽池机会登记到后补单位
    for key, pool in supplement_pools.items():
        for unit, n2, N2 in pool:
            sf[key].supplement_draws[unit] = (n2, N2)

    return RevisionLedger(frames=sf, revision_rows=payload["revisions"])
