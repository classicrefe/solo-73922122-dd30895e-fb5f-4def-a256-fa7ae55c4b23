"""新鲜度核查核心逻辑：纯函数实现，便于单元测试。

时间约定：所有时间统一归一化为 UTC ISO8601 字符串（秒级）存储与比较。
分区小时向下取整到整点。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

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
