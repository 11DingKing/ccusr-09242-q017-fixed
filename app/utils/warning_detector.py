import json
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session
from datetime import datetime

from app.models import (
    Graduate,
    College,
    MicroMajor,
    ProvinceReferenceLine,
    Warning,
    DestinationStatus,
    DestinationType,
    WarningType,
    WarningLevel,
    WarningStatus,
)
from app.utils.stats_calculator import calculate_group_stats


DECLINE_THRESHOLD_YELLOW = 2
DECLINE_THRESHOLD_ORANGE = 3
DECLINE_THRESHOLD_RED = 4


GAP_THRESHOLD_YELLOW = 5.0
GAP_THRESHOLD_ORANGE = 10.0
GAP_THRESHOLD_RED = 15.0


# 预警级别严重度。已人工关闭的预警只有在新级别更严重时才会被重新打开
LEVEL_SEVERITY = {
    WarningLevel.YELLOW: 1,
    WarningLevel.ORANGE: 2,
    WarningLevel.RED: 3,
}

OUTCOME_CREATED = "created"
OUTCOME_UPDATED = "updated"
OUTCOME_SKIPPED = "skipped"


class DetectionResult:
    """一次预警检测的结果，按新增/更新/跳过归类。"""

    def __init__(self) -> None:
        self.created: List[Warning] = []
        self.updated: List[Warning] = []
        self.skipped: List[Warning] = []

    def add(self, warning: Warning, outcome: str) -> None:
        getattr(self, outcome).append(warning)

    def extend(self, other: "DetectionResult") -> None:
        self.created.extend(other.created)
        self.updated.extend(other.updated)
        self.skipped.extend(other.skipped)

    @property
    def warnings(self) -> List[Warning]:
        """本次检测实际新增或更新的预警。"""
        return self.created + self.updated

    def counts(self) -> Dict[str, int]:
        return {
            "created_count": len(self.created),
            "updated_count": len(self.updated),
            "skipped_count": len(self.skipped),
        }


def calculate_yearly_indicators(
    db: Session,
    target_type: str,
    target_id: int,
) -> List[Dict]:
    years = db.query(Graduate.graduation_year).distinct().order_by(
        Graduate.graduation_year
    ).all()
    years = [y[0] for y in years]

    yearly_data = []
    for year in years:
        query = db.query(Graduate).filter(Graduate.graduation_year == year)

        if target_type == "micro_major":
            query = query.filter(
                Graduate.has_micro_major == True,
                Graduate.micro_major_id == target_id,
            )
        elif target_type == "college":
            query = query.filter(Graduate.college_id == target_id)

        graduates = query.all()
        if not graduates:
            continue

        stats = calculate_group_stats(graduates)

        yearly_data.append({
            "year": year,
            "confirmed_rate": stats.confirmed_rate,
            "aligned_rate": stats.aligned_rate,
            "total_count": stats.total_count,
            "confirmed_count": stats.confirmed_count,
            "aligned_count": stats.aligned_count,
        })

    return yearly_data


def get_province_reference_line(db: Session, year: int, indicator: str) -> Optional[ProvinceReferenceLine]:
    return db.query(ProvinceReferenceLine).filter(
        ProvinceReferenceLine.graduation_year == year,
        ProvinceReferenceLine.indicator == indicator,
    ).first()


def detect_continuous_decline(
    yearly_data: List[Dict],
    indicator: str,
) -> List[Tuple[int, int, int, List[Dict]]]:
    if len(yearly_data) < 2:
        return []

    declines = []
    current_start = 0
    current_decline_count = 0
    current_sequence = []

    for i in range(1, len(yearly_data)):
        prev_val = yearly_data[i - 1][indicator]
        curr_val = yearly_data[i][indicator]

        if curr_val < prev_val:
            if current_decline_count == 0:
                current_start = i - 1
            current_decline_count += 1
            current_sequence.append(yearly_data[i])
        else:
            if current_decline_count >= DECLINE_THRESHOLD_YELLOW:
                declines.append((
                    yearly_data[current_start]["year"],
                    yearly_data[i - 1]["year"],
                    current_decline_count,
                    yearly_data[current_start:i],
                ))
            current_decline_count = 0
            current_sequence = []

    if current_decline_count >= DECLINE_THRESHOLD_YELLOW:
        declines.append((
            yearly_data[current_start]["year"],
            yearly_data[-1]["year"],
            current_decline_count,
            yearly_data[current_start:],
        ))

    return declines


def determine_decline_level(decline_count: int) -> WarningLevel:
    if decline_count >= DECLINE_THRESHOLD_RED:
        return WarningLevel.RED
    elif decline_count >= DECLINE_THRESHOLD_ORANGE:
        return WarningLevel.ORANGE
    else:
        return WarningLevel.YELLOW


def determine_gap_level(gap: float) -> WarningLevel:
    if gap >= GAP_THRESHOLD_RED:
        return WarningLevel.RED
    elif gap >= GAP_THRESHOLD_ORANGE:
        return WarningLevel.ORANGE
    else:
        return WarningLevel.YELLOW


def detect_below_province_line(
    db: Session,
    yearly_data: List[Dict],
    indicator: str,
) -> List[Tuple[int, float, float, float]]:
    below_entries = []

    for data in yearly_data:
        reference_line = get_province_reference_line(db, data["year"], indicator)
        if not reference_line:
            continue

        current_value = data[indicator]
        if current_value < reference_line.threshold:
            gap = round(reference_line.threshold - current_value, 2)
            below_entries.append((
                data["year"],
                current_value,
                reference_line.threshold,
                gap,
            ))

    return below_entries


def get_target_name(db: Session, target_type: str, target_id: int) -> str:
    if target_type == "micro_major":
        obj = db.query(MicroMajor).filter(MicroMajor.id == target_id).first()
        return obj.name if obj else "未知微专业"
    elif target_type == "college":
        obj = db.query(College).filter(College.id == target_id).first()
        return obj.name if obj else "未知学院"
    return "未知"


def _parse_details(details_json: Optional[str]) -> List[Dict]:
    if not details_json:
        return []
    try:
        data = json.loads(details_json)
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _merge_details(existing_json: Optional[str], new_details: List[Dict]) -> List[Dict]:
    """按届次合并历史明细，同一届次以新数据为准，保留历史变化。"""
    merged = {}
    for item in _parse_details(existing_json) + list(new_details or []):
        if isinstance(item, dict) and item.get("year") is not None:
            merged[item["year"]] = item
    return [merged[year] for year in sorted(merged)]


def _query_warnings(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
    statuses: List[WarningStatus],
) -> List[Warning]:
    return db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
        Warning.status.in_(statuses),
    ).order_by(Warning.id).all()


def _apply_detection(
    warning: Warning,
    *,
    warning_level: WarningLevel,
    current_value: float,
    province_value: Optional[float],
    gap: Optional[float],
    start_year: int,
    end_year: int,
    decline_count: int,
    decline_details: List[Dict],
    description: Optional[str],
    keep_earliest_start: bool,
    accumulate_details: bool,
) -> bool:
    """把最新检测结果写入既有预警，返回是否有字段发生变化。"""
    if keep_earliest_start:
        start_year = min(warning.start_year, start_year)
    if accumulate_details:
        decline_details = _merge_details(warning.decline_details, decline_details)

    new_values = {
        "warning_level": warning_level,
        "current_value": current_value,
        "province_value": province_value,
        "gap": gap,
        "start_year": start_year,
        "end_year": end_year,
        "decline_count": decline_count,
        "description": description,
    }

    changed = False
    for field, value in new_values.items():
        if getattr(warning, field) != value:
            setattr(warning, field, value)
            changed = True

    if _parse_details(warning.decline_details) != decline_details:
        warning.decline_details = json.dumps(decline_details, ensure_ascii=False)
        changed = True

    return changed


def upsert_warning(
    db: Session,
    *,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    warning_level: WarningLevel,
    indicator: str,
    current_value: float,
    start_year: int,
    end_year: int,
    decline_count: int,
    decline_details: List[Dict],
    province_value: Optional[float] = None,
    gap: Optional[float] = None,
    description: Optional[str] = None,
    match_window: bool = False,
    keep_earliest_start: bool = False,
    accumulate_details: bool = False,
) -> Tuple[Warning, str]:
    """同一对象同一指标只保留一条活动预警。

    - 存在活动预警：用最新检测结果更新其依据、级别和差距，并合并重复的活动预警；
    - 仅有已关闭预警：只有新级别更严重（规则明确要求）时才重新打开，否则跳过；
    - 否则新建预警。

    返回 (预警, created/updated/skipped)。
    """

    def overlaps(w: Warning) -> bool:
        return w.start_year <= end_year and w.end_year >= start_year

    apply_kwargs = dict(
        warning_level=warning_level,
        current_value=current_value,
        province_value=province_value,
        gap=gap,
        start_year=start_year,
        end_year=end_year,
        decline_count=decline_count,
        decline_details=decline_details,
        description=description,
        keep_earliest_start=keep_earliest_start,
        accumulate_details=accumulate_details,
    )

    active = _query_warnings(
        db, target_type, target_id, warning_type, indicator, [WarningStatus.ACTIVE]
    )
    if match_window:
        active = [w for w in active if overlaps(w)]

    if active:
        # 保留最早创建的预警（归因记录等关联指向它），其余重复活动预警合并关闭
        primary = min(active, key=lambda w: w.id)
        changed = False
        for extra in active:
            if extra.id == primary.id:
                continue
            extra.status = WarningStatus.RESOLVED
            extra.description = f"{extra.description or ''}（系统合并重复预警）".strip()
            changed = True
        changed = _apply_detection(primary, **apply_kwargs) or changed
        db.flush()
        return primary, OUTCOME_UPDATED if changed else OUTCOME_SKIPPED

    closed = _query_warnings(
        db,
        target_type,
        target_id,
        warning_type,
        indicator,
        [WarningStatus.RESOLVED, WarningStatus.DISMISSED],
    )
    if match_window:
        closed = [w for w in closed if overlaps(w)]

    if closed:
        latest_closed = max(closed, key=lambda w: (w.end_year, w.id))
        if LEVEL_SEVERITY[warning_level] > LEVEL_SEVERITY[latest_closed.warning_level]:
            _apply_detection(latest_closed, **apply_kwargs)
            latest_closed.status = WarningStatus.ACTIVE
            db.flush()
            return latest_closed, OUTCOME_UPDATED
        return latest_closed, OUTCOME_SKIPPED

    target_name = get_target_name(db, target_type, target_id)
    warning = Warning(
        warning_type=warning_type,
        warning_level=warning_level,
        status=WarningStatus.ACTIVE,
        target_type=target_type,
        target_id=target_id,
        target_name=target_name,
        indicator=indicator,
        current_value=current_value,
        province_value=province_value,
        gap=gap,
        start_year=start_year,
        end_year=end_year,
        decline_count=decline_count,
        decline_details=json.dumps(decline_details, ensure_ascii=False),
        description=description,
    )
    db.add(warning)
    db.flush()
    return warning, OUTCOME_CREATED


def run_warning_detection_for_target(
    db: Session,
    target_type: str,
    target_id: int,
) -> DetectionResult:
    result = DetectionResult()

    yearly_data = calculate_yearly_indicators(db, target_type, target_id)
    if not yearly_data:
        return result

    for indicator, warning_type, label in [
        ("confirmed_rate", WarningType.CONFIRMED_RATE_DECLINE, "去向落实率"),
        ("aligned_rate", WarningType.ALIGNED_RATE_DECLINE, "对口就业率"),
    ]:
        declines = detect_continuous_decline(yearly_data, indicator)
        for start_year, end_year, decline_count, sequence in declines:
            level = determine_decline_level(decline_count)
            current_value = sequence[-1][indicator]
            description = (
                f"{label}自{start_year}届至{end_year}届连续{decline_count}届下降，"
                f"从{sequence[0][indicator]:.2f}%降至{current_value:.2f}%，"
                f"累计下降{sequence[0][indicator] - current_value:.2f}个百分点。"
            )

            warning, outcome = upsert_warning(
                db,
                target_type=target_type,
                target_id=target_id,
                warning_type=warning_type,
                warning_level=level,
                indicator=indicator,
                current_value=current_value,
                start_year=start_year,
                end_year=end_year,
                decline_count=decline_count,
                decline_details=sequence,
                description=description,
                match_window=True,
            )
            result.add(warning, outcome)

    for indicator, label in [
        ("confirmed_rate", "去向落实率"),
        ("aligned_rate", "对口就业率"),
    ]:
        below_entries = detect_below_province_line(db, yearly_data, indicator)
        if not below_entries:
            continue

        # 同一指标只维护一条活动预警：以最新低于省线的届次为当前状态，
        # 历史低于省线的届次并入明细保留
        year, current_value, threshold, gap = below_entries[-1]
        level = determine_gap_level(gap)
        below_years = {entry[0] for entry in below_entries}
        details = [d for d in yearly_data if d["year"] in below_years]
        description = (
            f"{year}届{label}为{current_value:.2f}%，"
            f"低于全省预警阈值{threshold:.2f}%，"
            f"差距为{gap:.2f}个百分点。"
        )

        warning, outcome = upsert_warning(
            db,
            target_type=target_type,
            target_id=target_id,
            warning_type=WarningType.BELOW_PROVINCE_LINE,
            warning_level=level,
            indicator=indicator,
            current_value=current_value,
            start_year=below_entries[0][0],
            end_year=year,
            decline_count=1,
            decline_details=details,
            province_value=threshold,
            gap=gap,
            description=description,
            keep_earliest_start=True,
            accumulate_details=True,
        )
        result.add(warning, outcome)

    return result


def run_full_warning_detection(db: Session) -> Dict:
    result = DetectionResult()

    micro_majors = db.query(MicroMajor).all()
    for mm in micro_majors:
        result.extend(run_warning_detection_for_target(db, "micro_major", mm.id))

    colleges = db.query(College).all()
    for college in colleges:
        result.extend(run_warning_detection_for_target(db, "college", college.id))

    db.commit()

    touched = result.warnings
    return {
        **result.counts(),
        "total_warnings": len(touched),
        "micro_major_warnings": sum(1 for w in touched if w.target_type == "micro_major"),
        "college_warnings": sum(1 for w in touched if w.target_type == "college"),
    }


def get_target_warnings(
    db: Session,
    target_type: str,
    target_id: int,
    status: Optional[str] = None,
) -> List[Warning]:
    query = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
    )

    if status:
        query = query.filter(Warning.status == status)

    return query.order_by(Warning.created_at.desc()).all()
