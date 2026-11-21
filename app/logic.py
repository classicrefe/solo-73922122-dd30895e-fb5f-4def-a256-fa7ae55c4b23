"""新鲜度核查核心逻辑：纯函数实现，便于单元测试。

时间约定：所有时间统一归一化为 UTC ISO8601 字符串（秒级）存储与比较。
分区小时向下取整到整点。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

UTC = timezone.utc

# 阻断原因代码
MISSING = "MISSING"        # 缺失：目录中没有任何提交记录
PENDING = "PENDING"        # 截至时刻前尚未提交
DUPLICATE = "DUPLICATE"    # 同一分区同一修订被重复提交
LATE = "LATE"              # 提交时间晚于 计划截止 + 宽限分钟

REASON_LABELS = {
    MISSING: "缺失",
    PENDING: "截至前未提交",
    DUPLICATE: "重复提交",
    LATE: "迟到超宽限",
}


def now() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: Any) -> datetime:
    """解析时间为 UTC aware datetime。naive 输入按 UTC 处理。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def window_hours(business_date: date, offset_days: int) -> list[datetime]:
    """日报业务日期 - 分区偏移天数 = 目标分区日期，返回该日 24 个整点（UTC）。"""
    target = business_date - timedelta(days=offset_days)
    start = datetime(target.year, target.month, target.day, tzinfo=UTC)
    return [start + timedelta(hours=i) for i in range(24)]


def evaluate_report(
    window: list[datetime],
    rows_by_hour: Mapping[str, list[Mapping[str, Any]]],
    as_of: Any,
    grace_minutes: int,
) -> dict[str, Any]:
    """对单个日报在给定窗口上计算安全水位与最早阻断原因。

    rows_by_hour: 键为 UTC 整点 ISO 字符串，值为该分区的所有提交记录
    （含 planned_deadline / submitted_at / revision 字段）。
    规则：
      - 每个分区只有最高修订号生效，低修订为“已被新修订替代”，不参与判定；
      - 最高修订存在多条 -> 重复提交，阻断；
      - 生效提交晚于截至时刻 -> 截至前未提交，阻断；
      - 生效提交晚于 计划截止+宽限 -> 迟到超宽限，阻断；
      - 晚于计划截止但在宽限内 -> 可用但标记；
      - 水位 = 从窗口起点开始连续可用分区的最后一个小时。
    """
    as_of_dt = parse_dt(as_of)
    grace = timedelta(minutes=grace_minutes)
    details: list[dict[str, Any]] = []
    counts = {"ok": 0, "late_within_grace": 0, "late": 0,
              "missing": 0, "pending": 0, "duplicate": 0}
    watermark: str | None = None
    block_hour = block_reason = block_message = None

    for hour in window:
        hour_iso = iso(hour)
        rows = list(rows_by_hour.get(hour_iso, []))
        entry: dict[str, Any] = {
            "hour": hour_iso, "state": None, "usable": False, "reason": None,
            "message": None, "revision": None, "submitted_at": None,
            "planned_deadline": None, "superseded_count": 0,
            "submissions": len(rows),
        }
        if not rows:
            entry["state"] = "missing"
            entry["reason"] = MISSING
            entry["message"] = f"分区 {hour_iso} 缺失：目录中没有任何提交记录"
        else:
            max_rev = max(r["revision"] for r in rows)
            effective = [r for r in rows if r["revision"] == max_rev]
            entry["revision"] = max_rev
            entry["superseded_count"] = len(rows) - len(effective)
            if len(effective) > 1:
                e = effective[0]
                entry["submitted_at"] = e["submitted_at"]
                entry["planned_deadline"] = e["planned_deadline"]
                entry["state"] = "duplicate"
                entry["reason"] = DUPLICATE
                entry["message"] = (
                    f"分区 {hour_iso} 的修订 r{max_rev} 被重复提交 {len(effective)} 次，"
                    "无法确认有效版本"
                )
            else:
                e = effective[0]
                sub = parse_dt(e["submitted_at"])
                deadline = parse_dt(e["planned_deadline"])
                entry["submitted_at"] = iso(sub)
                entry["planned_deadline"] = iso(deadline)
                if sub > as_of_dt:
                    entry["state"] = "pending"
                    entry["reason"] = PENDING
                    entry["message"] = (
                        f"分区 {hour_iso} 在截至时刻 {iso(as_of_dt)} 前尚未提交"
                        f"（实际提交 {iso(sub)}）"
                    )
                elif sub > deadline + grace:
                    entry["state"] = "late"
                    entry["reason"] = LATE
                    entry["message"] = (
                        f"分区 {hour_iso} 提交于 {iso(sub)}，晚于计划截止 {iso(deadline)} "
                        f"+ 宽限 {grace_minutes} 分钟"
                    )
                elif sub > deadline:
                    entry["state"] = "late_within_grace"
                    entry["usable"] = True
                    entry["message"] = (
                        f"迟到但在宽限内（提交 {iso(sub)}，截止 {iso(deadline)}）"
                    )
                else:
                    entry["state"] = "ok"
                    entry["usable"] = True

        counts[entry["state"]] += 1
        if entry["usable"]:
            if block_hour is None:
                watermark = hour_iso
        elif block_hour is None:
            block_hour = hour_iso
            block_reason = entry["reason"]
            block_message = entry["message"]
        details.append(entry)

    status = "READY" if all(e["usable"] for e in details) else "BLOCKED"
    return {
        "window_start": iso(window[0]),
        "window_end": iso(window[-1]),
        "watermark_hour": watermark,
        "status": status,
        "block_hour": block_hour,
        "block_reason": block_reason,
        "block_message": block_message,
        "counts": counts,
        "details": details,
    }


# ---------- 历史分析 ----------

def daterange(start: date, end: date) -> list[date]:
    """闭区间 [start, end] 内的全部日期。"""
    if end < start:
        return []
    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]


def latest_checks_by_day(checks: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """每个业务日取截至时刻（as_of）最晚的核查；时刻相同取最后保存的一次（id 最大）。

    checks 元素需含 id / business_date / as_of（ISO8601 UTC 字符串，可字典序比较）。
    """
    chosen: dict[str, dict[str, Any]] = {}
    for c in checks:
        day = c["business_date"]
        cur = chosen.get(day)
        if cur is None or (c["as_of"], c["id"]) > (cur["as_of"], cur["id"]):
            chosen[day] = dict(c)
    return chosen


def analyze_blocking(
    days: Sequence[date],
    chosen_checks: Mapping[str, Mapping[str, Any]],
    results_by_check: Mapping[int, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """按"首个阻断原因"统计各业务日。

    chosen_checks: business_date -> 当日入选核查（见 latest_checks_by_day）
    results_by_check: check_id -> 该核查中目标身份（或未归属）的 check_results 行

    当日入选核查中目标身份若为 READY，则较早的阻断不计入（本就不在入选核查内）；
    BLOCKED 时按 block_reason 计 1 天；无核查或无该身份结果记为对应空状态。
    返回原因计数（含全部四类原因，便于前端固定顺序展示）与逐日明细。
    """
    counts = {reason: 0 for reason in REASON_LABELS}
    day_rows: list[dict[str, Any]] = []
    totals = {"blocked_days": 0, "ready_days": 0, "no_check_days": 0, "no_record_days": 0}

    for day in days:
        day_iso = day.isoformat()
        check = chosen_checks.get(day_iso)
        row: dict[str, Any] = {
            "business_date": day_iso,
            "status": None, "block_reason": None, "check_id": None,
            "as_of": None, "stale": None, "catalog_version": None,
            "report_name": None,
        }
        if check is None:
            row["status"] = "NO_CHECK"
            totals["no_check_days"] += 1
        else:
            results = list(results_by_check.get(check["id"], []))
            row["check_id"] = check["id"]
            row["as_of"] = check["as_of"]
            row["stale"] = check.get("stale")
            row["catalog_version"] = check.get("catalog_version")
            if not results:
                row["status"] = "NO_RECORD"
                totals["no_record_days"] += 1
                row["report_name"] = None
            else:
                # 未归属身份下同一核查可能保留多条无法确认身份的结果：取首个阻断；
                # 若全部就绪则取第一条作为当日代表。
                blocked = next((r for r in results if r["status"] == "BLOCKED"), None)
                if blocked is not None:
                    rep = blocked
                    row["status"] = "BLOCKED"
                    row["block_reason"] = rep["block_reason"]
                    counts[rep["block_reason"]] += 1
                    totals["blocked_days"] += 1
                else:
                    rep = results[0]
                    row["status"] = "READY"
                    totals["ready_days"] += 1
                row["report_name"] = rep["report_name"]
        day_rows.append(row)

    reason_summary = [
        {"reason": reason, "label": REASON_LABELS[reason], "days": counts[reason]}
        for reason in (MISSING, PENDING, DUPLICATE, LATE)
    ]
    return {
        "reason_counts": counts,
        "reason_summary": reason_summary,
        "days": day_rows,
        "totals": totals,
    }


def classify_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """为分区列表页计算每行状态标记。

    状态优先级：superseded（已被新修订替代） > duplicate（重复提交） > late（迟到） > ok。
    flags 保留全部命中标记。
    """
    by_hour: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        by_hour.setdefault(r["partition_hour"], []).append(r)

    out: list[dict[str, Any]] = []
    for hour, rs in by_hour.items():
        max_rev = max(r["revision"] for r in rs)
        eff_count = sum(1 for r in rs if r["revision"] == max_rev)
        for r in rs:
            flags: list[str] = []
            if r["revision"] < max_rev:
                flags.append("superseded")
            elif eff_count > 1:
                flags.append("duplicate")
            if parse_dt(r["submitted_at"]) > parse_dt(r["planned_deadline"]):
                flags.append("late")
            out.append({
                **dict(r),
                "flags": flags,
                "status": flags[0] if flags else "ok",
            })
    out.sort(key=lambda r: (r["partition_hour"], r["revision"], r["id"]))
    return out
