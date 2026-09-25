"""预警检测任务。

去重与更新规则（全量检测与单对象检测共用）：

1. 唯一键：同一对象（target_type + target_id）、同一预警类型、同一指标，
   最多存在一条"预警中"的预警。基准线或毕业生数据变化时，更新既有预警的
   依据（province_value）、级别（warning_level）、差距（gap）等字段，
   不再新增重复预警；历史关闭的预警记录保留，但不重复占用活动状态。
2. 每次检测结果分三类：created（新增）、updated（更新，含重新打开）、
   skipped（跳过——活动预警内容无变化，或已关闭预警未达到重开条件）。
3. 已人工关闭（已解决/已忽略）的预警，仅当新一轮检测的预警级别高于
   关闭时的级别（黄 -> 橙 -> 红）时才重新打开，否则保持关闭状态。
4. 历史遗留的同一唯一键多条活动预警，检测时保留最近一条并更新，
   其余转为"已忽略"，不再占用活动状态。
"""

import json
from typing import List, Dict, Tuple, Optional
from sqlalchemy.orm import Session

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


OUTCOME_CREATED = "created"
OUTCOME_UPDATED = "updated"
OUTCOME_SKIPPED = "skipped"

# 预警级别严重度，用于判定已关闭预警是否满足重新打开的条件
LEVEL_SEVERITY = {
    WarningLevel.YELLOW: 1,
    WarningLevel.ORANGE: 2,
    WarningLevel.RED: 3,
}

# 已人工关闭、检测不得随意重开的状态
CLOSED_STATUSES = (WarningStatus.RESOLVED, WarningStatus.DISMISSED)


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
            gap = reference_line.threshold - current_value
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


def find_warnings_by_key(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
    statuses: Optional[Tuple[WarningStatus, ...]] = None,
) -> List[Warning]:
    """按唯一键（对象 + 预警类型 + 指标）查询预警，最近更新的排前面。"""
    query = db.query(Warning).filter(
        Warning.target_type == target_type,
        Warning.target_id == target_id,
        Warning.warning_type == warning_type,
        Warning.indicator == indicator,
    )
    if statuses is not None:
        query = query.filter(Warning.status.in_(statuses))
    return query.order_by(Warning.updated_at.desc(), Warning.id.desc()).all()


def _values_differ(current, new) -> bool:
    if current is None and new is None:
        return False
    if current is None or new is None:
        return True
    if isinstance(current, float) or isinstance(new, float):
        return abs(float(current) - float(new)) > 1e-9
    return current != new


def _warning_fields_changed(warning: Warning, fields: Dict) -> bool:
    return any(
        _values_differ(getattr(warning, key), value)
        for key, value in fields.items()
    )


def _apply_warning_fields(warning: Warning, fields: Dict) -> None:
    for key, value in fields.items():
        setattr(warning, key, value)


def upsert_warning(
    db: Session,
    target_type: str,
    target_id: int,
    warning_type: WarningType,
    indicator: str,
    fields: Dict,
) -> Tuple[Warning, str]:
    """按唯一键落地一条检测结果，返回 (预警, created/updated/skipped)。

    - 无同键预警：新建活动预警（created）。
    - 已有活动预警：依据/级别/差距等有变化则更新（updated），无变化则
      跳过（skipped）；同键多余的活动遗留预警一并转为已忽略。
    - 仅有已关闭预警：新检测级别高于关闭时级别才重新打开（updated），
      否则保持关闭（skipped），不新建预警。
    """
    actives = find_warnings_by_key(
        db, target_type, target_id, warning_type, indicator,
        statuses=(WarningStatus.ACTIVE,),
    )
    if actives:
        canonical = actives[0]
        changed = False
        for duplicate in actives[1:]:
            duplicate.status = WarningStatus.DISMISSED
            duplicate.description = (
                f"{duplicate.description or ''}"
                f"（系统检测去重：该重复预警已由预警#{canonical.id}接管）"
            )
            changed = True
        if _warning_fields_changed(canonical, fields):
            _apply_warning_fields(canonical, fields)
            changed = True
        db.flush()
        return canonical, OUTCOME_UPDATED if changed else OUTCOME_SKIPPED

    closed = find_warnings_by_key(
        db, target_type, target_id, warning_type, indicator,
        statuses=CLOSED_STATUSES,
    )
    if closed:
        latest_closed = closed[0]
        new_level = fields["warning_level"]
        if LEVEL_SEVERITY[new_level] > LEVEL_SEVERITY[latest_closed.warning_level]:
            _apply_warning_fields(latest_closed, fields)
            latest_closed.status = WarningStatus.ACTIVE
            db.flush()
            return latest_closed, OUTCOME_UPDATED
        return latest_closed, OUTCOME_SKIPPED

    warning = Warning(
        warning_type=warning_type,
        warning_level=fields["warning_level"],
        status=WarningStatus.ACTIVE,
        target_type=target_type,
        target_id=target_id,
        target_name=get_target_name(db, target_type, target_id),
        indicator=indicator,
        current_value=fields["current_value"],
        province_value=fields["province_value"],
        gap=fields["gap"],
        start_year=fields["start_year"],
        end_year=fields["end_year"],
        decline_count=fields["decline_count"],
        decline_details=fields["decline_details"],
        description=fields["description"],
    )
    db.add(warning)
    db.flush()
    return warning, OUTCOME_CREATED


def _latest_below_line_run(
    below_entries: List[Tuple[int, float, float, float]],
) -> List[Tuple[int, float, float, float]]:
    """取最新一个连续低于省线的届次区间（按届次升序）。"""
    run = [below_entries[-1]]
    for entry in reversed(below_entries[:-1]):
        if entry[0] == run[0][0] - 1:
            run.insert(0, entry)
        else:
            break
    return run


def _new_detection_result() -> Dict:
    return {
        OUTCOME_CREATED: 0,
        OUTCOME_UPDATED: 0,
        OUTCOME_SKIPPED: 0,
        "warnings": [],
    }


def run_warning_detection_for_target(
    db: Session,
    target_type: str,
    target_id: int,
) -> Dict:
    """对单个对象运行预警检测。

    返回 {"created": n, "updated": n, "skipped": n, "warnings": [...]}，
    其中 warnings 为本次新增或更新的预警。
    """
    result = _new_detection_result()

    yearly_data = calculate_yearly_indicators(db, target_type, target_id)
    if not yearly_data:
        return result

    for indicator, warning_type, label in [
        ("confirmed_rate", WarningType.CONFIRMED_RATE_DECLINE, "去向落实率"),
        ("aligned_rate", WarningType.ALIGNED_RATE_DECLINE, "对口就业率"),
    ]:
        declines = detect_continuous_decline(yearly_data, indicator)
        if not declines:
            continue

        # 同一对象同一指标只保留一条活动预警，取最近一次连续下降区间
        start_year, end_year, decline_count, sequence = max(declines, key=lambda d: d[1])
        level = determine_decline_level(decline_count)
        current_value = sequence[-1][indicator]
        description = (
            f"{label}自{start_year}届至{end_year}届连续{decline_count}届下降，"
            f"从{sequence[0][indicator]:.2f}%降至{current_value:.2f}%，"
            f"累计下降{sequence[0][indicator] - current_value:.2f}个百分点。"
        )

        fields = {
            "warning_level": level,
            "current_value": current_value,
            "province_value": None,
            "gap": None,
            "start_year": start_year,
            "end_year": end_year,
            "decline_count": decline_count,
            "decline_details": json.dumps(sequence, ensure_ascii=False),
            "description": description,
        }
        warning, outcome = upsert_warning(
            db, target_type, target_id, warning_type, indicator, fields
        )
        result[outcome] += 1
        if outcome != OUTCOME_SKIPPED:
            result["warnings"].append(warning)

    for indicator, label in [
        ("confirmed_rate", "去向落实率"),
        ("aligned_rate", "对口就业率"),
    ]:
        below_entries = detect_below_province_line(db, yearly_data, indicator)
        if not below_entries:
            continue

        # 同一对象同一指标只保留一条活动预警，预警反映最新连续低于省线区间
        run = _latest_below_line_run(below_entries)
        start_year = run[0][0]
        end_year, current_value, threshold, gap = run[-1]
        gap = round(gap, 2)
        level = determine_gap_level(gap)
        details = [d for d in yearly_data if start_year <= d["year"] <= end_year]

        if start_year == end_year:
            description = (
                f"{end_year}届{label}为{current_value:.2f}%，"
                f"低于全省预警阈值{threshold:.2f}%，"
                f"差距为{gap:.2f}个百分点。"
            )
        else:
            description = (
                f"{label}自{start_year}届至{end_year}届持续低于全省预警阈值，"
                f"{end_year}届为{current_value:.2f}%，"
                f"低于全省预警阈值{threshold:.2f}%，"
                f"差距为{gap:.2f}个百分点。"
            )

        fields = {
            "warning_level": level,
            "current_value": current_value,
            "province_value": threshold,
            "gap": gap,
            "start_year": start_year,
            "end_year": end_year,
            "decline_count": len(run),
            "decline_details": json.dumps(details, ensure_ascii=False),
            "description": description,
        }
        warning, outcome = upsert_warning(
            db, target_type, target_id,
            WarningType.BELOW_PROVINCE_LINE, indicator, fields,
        )
        result[outcome] += 1
        if outcome != OUTCOME_SKIPPED:
            result["warnings"].append(warning)

    return result


def run_full_warning_detection(db: Session) -> Dict:
    summary = _new_detection_result()

    micro_majors = db.query(MicroMajor).all()
    for mm in micro_majors:
        result = run_warning_detection_for_target(db, "micro_major", mm.id)
        for key in (OUTCOME_CREATED, OUTCOME_UPDATED, OUTCOME_SKIPPED):
            summary[key] += result[key]
        summary["warnings"].extend(result["warnings"])

    colleges = db.query(College).all()
    for college in colleges:
        result = run_warning_detection_for_target(db, "college", college.id)
        for key in (OUTCOME_CREATED, OUTCOME_UPDATED, OUTCOME_SKIPPED):
            summary[key] += result[key]
        summary["warnings"].extend(result["warnings"])

    db.commit()

    affected = summary["warnings"]
    return {
        "created": summary[OUTCOME_CREATED],
        "updated": summary[OUTCOME_UPDATED],
        "skipped": summary[OUTCOME_SKIPPED],
        "total_warnings": len(affected),
        "micro_major_warnings": sum(1 for w in affected if w.target_type == "micro_major"),
        "college_warnings": sum(1 for w in affected if w.target_type == "college"),
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
