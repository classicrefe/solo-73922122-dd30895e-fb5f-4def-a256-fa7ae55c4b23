"""FastAPI 入口：数据产品新鲜度核查服务。

同一目录快照语义：每次核查记录当时的目录版本号；目录分区或日报依赖
发生变化都会递增版本，此前生成的核查结果即标记为失效（stale）。
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, logic

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DB = str(Path.cwd() / "data" / "freshness.db")


# ---------- 请求模型 ----------

class CatalogIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PartitionIn(BaseModel):
    partition_hour: str
    planned_deadline: str
    submitted_at: str
    revision: int = Field(default=1, ge=0)


class PartitionBatch(BaseModel):
    partitions: list[PartitionIn] = Field(min_length=1)


class ReportIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    offset_days: int = Field(default=1, ge=0, le=30)
    grace_minutes: int = Field(default=0, ge=0, le=24 * 60)


class CheckIn(BaseModel):
    business_date: str  # YYYY-MM-DD
    as_of: str          # ISO8601 时间


# ---------- 依赖与工具 ----------

def get_db(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_catalog_or_404(conn, catalog_id: int):
    row = conn.execute("SELECT * FROM catalogs WHERE id = ?", (catalog_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"目录 {catalog_id} 不存在")
    return row


def bump_version(conn, catalog_id: int) -> None:
    conn.execute("UPDATE catalogs SET version = version + 1 WHERE id = ?", (catalog_id,))


def current_version(conn, catalog_id: int) -> int:
    return conn.execute(
        "SELECT version FROM catalogs WHERE id = ?", (catalog_id,)
    ).fetchone()["version"]


def parse_dt_or_422(value: str, field: str) -> datetime:
    try:
        return logic.parse_dt(value)
    except Exception:
        raise HTTPException(status_code=422, detail=f"字段 {field} 不是合法时间: {value!r}")


def check_to_dict(row, current_ver: int) -> dict:
    d = dict(row)
    d["stale"] = row["catalog_version"] != current_ver
    return d


# ---------- 应用工厂 ----------

def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(title="数据产品新鲜度核查", version="1.0.0")
    path = db_path or os.environ.get("DB_PATH", DEFAULT_DB)
    db.init_db(path)
    app.state.db_path = path

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/analysis", include_in_schema=False)
    def analysis_page():
        return FileResponse(STATIC_DIR / "analysis.html")

    # ----- 目录 -----

    @app.post("/api/catalogs", status_code=201)
    def create_catalog(body: CatalogIn, conn=Depends(get_db)):
        cur = conn.execute(
            "INSERT INTO catalogs(name, version, created_at) VALUES (?, 0, ?)",
            (body.name, logic.iso(logic.now())),
        )
        conn.commit()
        return dict(get_catalog_or_404(conn, cur.lastrowid))

    @app.get("/api/catalogs")
    def list_catalogs(conn=Depends(get_db)):
        rows = conn.execute("SELECT * FROM catalogs ORDER BY id").fetchall()
        return {"catalogs": [dict(r) for r in rows]}

    @app.get("/api/catalogs/{catalog_id}")
    def get_catalog(catalog_id: int, conn=Depends(get_db)):
        return dict(get_catalog_or_404(conn, catalog_id))

    # ----- 分区 -----

    @app.post("/api/catalogs/{catalog_id}/partitions", status_code=201)
    def upload_partitions(catalog_id: int, body: PartitionBatch, conn=Depends(get_db)):
        get_catalog_or_404(conn, catalog_id)
        now_iso = logic.iso(logic.now())
        rows = []
        for p in body.partitions:
            hour = logic.floor_hour(parse_dt_or_422(p.partition_hour, "partition_hour"))
            deadline = parse_dt_or_422(p.planned_deadline, "planned_deadline")
            submitted = parse_dt_or_422(p.submitted_at, "submitted_at")
            rows.append((catalog_id, logic.iso(hour), logic.iso(deadline),
                         logic.iso(submitted), p.revision, now_iso))
        conn.executemany(
            "INSERT INTO partitions(catalog_id, partition_hour, planned_deadline,"
            " submitted_at, revision, created_at) VALUES (?,?,?,?,?,?)",
            rows,
        )
        bump_version(conn, catalog_id)  # 目录更新 -> 旧核查结果失效
        conn.commit()
        return {"inserted": len(rows), "catalog_version": current_version(conn, catalog_id)}

    @app.get("/api/catalogs/{catalog_id}/partitions")
    def list_partitions(catalog_id: int, conn=Depends(get_db)):
        get_catalog_or_404(conn, catalog_id)
        rows = conn.execute(
            "SELECT * FROM partitions WHERE catalog_id = ?", (catalog_id,)
        ).fetchall()
        return {"partitions": logic.classify_rows(rows)}

    @app.post("/api/catalogs/{catalog_id}/reset")
    def reset_partitions(catalog_id: int, conn=Depends(get_db)):
        get_catalog_or_404(conn, catalog_id)
        deleted = conn.execute(
            "DELETE FROM partitions WHERE catalog_id = ?", (catalog_id,)).rowcount
        bump_version(conn, catalog_id)
        conn.commit()
        return {"deleted": deleted, "catalog_version": current_version(conn, catalog_id)}

    # ----- 日报依赖 -----

    @app.post("/api/catalogs/{catalog_id}/reports", status_code=201)
    def create_report(catalog_id: int, body: ReportIn, conn=Depends(get_db)):
        get_catalog_or_404(conn, catalog_id)
        cur = conn.execute(
            "INSERT INTO reports(catalog_id, name, offset_days, grace_minutes, created_at)"
            " VALUES (?,?,?,?,?)",
            (catalog_id, body.name, body.offset_days, body.grace_minutes,
             logic.iso(logic.now())),
        )
        bump_version(conn, catalog_id)  # 依赖定义变化同样使旧核查失效
        conn.commit()
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)

    @app.get("/api/catalogs/{catalog_id}/reports")
    def list_reports(catalog_id: int, conn=Depends(get_db)):
        get_catalog_or_404(conn, catalog_id)
        rows = conn.execute(
            "SELECT * FROM reports WHERE catalog_id = ? AND deleted_at IS NULL ORDER BY id",
            (catalog_id,),
        ).fetchall()
        return {"reports": [dict(r) for r in rows]}

    @app.get("/api/catalogs/{catalog_id}/report-identities")
    def list_report_identities(catalog_id: int, conn=Depends(get_db)):
        """历史分析用：返回目录下全部日报身份，含已删除者（删后同名重建是不同身份）。"""
        get_catalog_or_404(conn, catalog_id)
        rows = conn.execute(
            "SELECT id, name, offset_days, grace_minutes, created_at, deleted_at"
            " FROM reports WHERE catalog_id = ? ORDER BY id",
            (catalog_id,),
        ).fetchall()
        identities = [dict(r) for r in rows]
        has_orphan = conn.execute(
            "SELECT 1 FROM checks c JOIN check_results r ON r.check_id = c.id"
            " WHERE c.catalog_id = ? AND r.report_id IS NULL LIMIT 1",
            (catalog_id,),
        ).fetchone()
        return {
            "identities": identities,
            "unassigned_available": has_orphan is not None,
        }

    @app.delete("/api/catalogs/{catalog_id}/reports/{report_id}")
    def delete_report(catalog_id: int, report_id: int, conn=Depends(get_db)):
        get_catalog_or_404(conn, catalog_id)
        cur = conn.execute(
            "UPDATE reports SET deleted_at = ? WHERE id = ? AND catalog_id = ?"
            " AND deleted_at IS NULL",
            (logic.iso(logic.now()), report_id, catalog_id))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"日报 {report_id} 不存在或已删除")
        # 软删除保留身份供历史归档；依赖集合变化仍使旧核查失效
        bump_version(conn, catalog_id)
        conn.commit()
        return {"deleted": report_id}

    # ----- 核查 -----

    @app.post("/api/catalogs/{catalog_id}/checks", status_code=201)
    def run_check(catalog_id: int, body: CheckIn, conn=Depends(get_db)):
        catalog = get_catalog_or_404(conn, catalog_id)
        try:
            business_date = date.fromisoformat(body.business_date)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail=f"business_date 应为 YYYY-MM-DD: {body.business_date!r}")
        as_of = parse_dt_or_422(body.as_of, "as_of")

        reports = conn.execute(
            "SELECT * FROM reports WHERE catalog_id = ? AND deleted_at IS NULL ORDER BY id",
            (catalog_id,),
        ).fetchall()
        part_rows = conn.execute(
            "SELECT * FROM partitions WHERE catalog_id = ?", (catalog_id,)
        ).fetchall()
        by_hour: dict[str, list] = {}
        for r in part_rows:
            by_hour.setdefault(r["partition_hour"], []).append(r)

        now_iso = logic.iso(logic.now())
        cur = conn.execute(
            "INSERT INTO checks(catalog_id, catalog_version, business_date, as_of, created_at)"
            " VALUES (?,?,?,?,?)",
            (catalog_id, catalog["version"], business_date.isoformat(),
             logic.iso(as_of), now_iso),
        )
        check_id = cur.lastrowid

        results = []
        for rep in reports:
            window = logic.window_hours(business_date, rep["offset_days"])
            res = logic.evaluate_report(window, by_hour, as_of, rep["grace_minutes"])
            conn.execute(
                "INSERT INTO check_results(check_id, report_id, report_name, offset_days,"
                " grace_minutes, window_start, window_end, watermark_hour, status,"
                " block_hour, block_reason, block_message, counts_json, detail_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (check_id, rep["id"], rep["name"], rep["offset_days"], rep["grace_minutes"],
                 res["window_start"], res["window_end"], res["watermark_hour"],
                 res["status"], res["block_hour"], res["block_reason"],
                 res["block_message"], json.dumps(res["counts"], ensure_ascii=False),
                 json.dumps(res["details"], ensure_ascii=False)),
            )
            results.append({"report_name": rep["name"],
                            "offset_days": rep["offset_days"],
                            "grace_minutes": rep["grace_minutes"], **res})

        conn.commit()
        return {
            "id": check_id, "catalog_id": catalog_id,
            "catalog_version": catalog["version"],
            "business_date": business_date.isoformat(),
            "as_of": logic.iso(as_of), "created_at": now_iso,
            "stale": False, "results": results,
        }

    @app.get("/api/catalogs/{catalog_id}/checks")
    def list_checks(catalog_id: int, conn=Depends(get_db)):
        catalog = get_catalog_or_404(conn, catalog_id)
        rows = conn.execute(
            "SELECT * FROM checks WHERE catalog_id = ? ORDER BY id DESC", (catalog_id,)
        ).fetchall()
        checks = []
        for r in rows:
            d = check_to_dict(r, catalog["version"])
            summary = conn.execute(
                "SELECT status, COUNT(*) AS c FROM check_results WHERE check_id = ?"
                " GROUP BY status", (r["id"],)).fetchall()
            d["summary"] = {s["status"]: s["c"] for s in summary}
            checks.append(d)
        return {"checks": checks, "current_version": catalog["version"]}

    @app.get("/api/checks/{check_id}")
    def get_check(check_id: int, conn=Depends(get_db)):
        row = conn.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"核查 {check_id} 不存在")
        ver = current_version(conn, row["catalog_id"])
        results = []
        for rr in conn.execute(
                "SELECT * FROM check_results WHERE check_id = ? ORDER BY id",
                (check_id,)).fetchall():
            d = dict(rr)
            d["counts"] = json.loads(d.pop("counts_json"))
            d["details"] = json.loads(d.pop("detail_json"))
            results.append(d)
        return {**check_to_dict(row, ver), "results": results}

    # ----- 历史分析 -----

    @app.get("/api/catalogs/{catalog_id}/analysis")
    def blocking_analysis(
        catalog_id: int,
        start_date: str,
        end_date: str,
        report_id: int | None = None,
        unassigned: bool = False,
        conn=Depends(get_db),
    ):
        """一周内（任意业务日期闭区间）按首个阻断原因统计阻断天数。

        每个业务日只取截至时刻最晚的已保存核查；时刻相同取最后保存的一次。
        当天入选核查中该日报身份若已就绪，则较早的阻断不计入。
        report_id 与 unassigned 二选一；unassigned 统计无法确认日报身份的旧记录。
        旧核查即使已失效（stale）仍作为当时的记录参与统计。
        """
        catalog = get_catalog_or_404(conn, catalog_id)
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="start_date/end_date 应为 YYYY-MM-DD")
        if end < start:
            raise HTTPException(status_code=422, detail="end_date 不能早于 start_date")
        if report_id is None and not unassigned:
            raise HTTPException(status_code=422,
                                detail="必须指定 report_id 或 unassigned=true")
        if report_id is not None and unassigned:
            raise HTTPException(status_code=422,
                                detail="report_id 与 unassigned 不能同时指定")
        if report_id is not None:
            rep = conn.execute(
                "SELECT * FROM reports WHERE id = ? AND catalog_id = ?",
                (report_id, catalog_id)).fetchone()
            if rep is None:
                raise HTTPException(status_code=404,
                                    detail=f"日报身份 {report_id} 不存在于目录 {catalog_id}")
            report_name, report_deleted_at = rep["name"], rep["deleted_at"]
        else:
            report_name, report_deleted_at = "未归属", None

        check_rows = conn.execute(
            "SELECT * FROM checks WHERE catalog_id = ? AND business_date BETWEEN ? AND ?"
            " ORDER BY id",
            (catalog_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        current_ver = catalog["version"]
        checks_payload = [check_to_dict(r, current_ver) for r in check_rows]
        chosen = logic.latest_checks_by_day(checks_payload)
        chosen_ids = [c["id"] for c in chosen.values()]

        results_by_check: dict[int, list] = {cid: [] for cid in chosen_ids}
        if chosen_ids:
            placeholders = ",".join("?" * len(chosen_ids))
            if unassigned:
                sql = (f"SELECT * FROM check_results WHERE check_id IN ({placeholders})"
                       " AND report_id IS NULL ORDER BY id")
                params = tuple(chosen_ids)
            else:
                sql = (f"SELECT * FROM check_results WHERE check_id IN ({placeholders})"
                       " AND report_id = ? ORDER BY id")
                params = tuple(chosen_ids) + (report_id,)
            for rr in conn.execute(sql, params).fetchall():
                results_by_check.setdefault(rr["check_id"], []).append(dict(rr))

        analysis = logic.analyze_blocking(
            logic.daterange(start, end), chosen, results_by_check)
        return {
            "catalog_id": catalog_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "report_id": report_id,
            "unassigned": unassigned,
            "report_name": report_name,
            "report_deleted": report_deleted_at is not None,
            "current_version": current_ver,
            **analysis,
        }

    # ----- 演示数据 -----

    @app.post("/api/demo", status_code=201)
    def load_demo(conn=Depends(get_db)):
        """创建/重置演示目录，覆盖：跨日依赖、宽限内/超宽限迟到、重复提交、
        修订替代、分区缺失、截至前未提交 等场景。"""
        name = "演示目录"
        row = conn.execute("SELECT * FROM catalogs WHERE name = ?", (name,)).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO catalogs(name, version, created_at) VALUES (?, 0, ?)",
                (name, logic.iso(logic.now())))
            catalog_id = cur.lastrowid
        else:
            catalog_id = row["id"]
        conn.execute(
            "DELETE FROM check_results WHERE check_id IN"
            " (SELECT id FROM checks WHERE catalog_id = ?)", (catalog_id,))
        conn.execute("DELETE FROM checks WHERE catalog_id = ?", (catalog_id,))
        conn.execute("DELETE FROM partitions WHERE catalog_id = ?", (catalog_id,))
        conn.execute("DELETE FROM reports WHERE catalog_id = ?", (catalog_id,))

        now_iso = logic.iso(logic.now())
        today = logic.now().date()
        d1 = today - timedelta(days=1)
        d2 = today - timedelta(days=2)

        def mk(day: date, hour: int, deadline_min: int, submitted_min: int, rev: int):
            start = datetime(day.year, day.month, day.day, tzinfo=logic.UTC)
            h = start + timedelta(hours=hour)
            return (catalog_id, logic.iso(h),
                    logic.iso(h + timedelta(minutes=deadline_min)),
                    logic.iso(h + timedelta(minutes=submitted_min)),
                    rev, now_iso)

        rows = []
        # 前天：24 个分区全部准时（供 offset=2 的日报 READY）
        for h in range(24):
            rows.append(mk(d2, h, 60, 55, 1))
        # 昨天：埋入各类异常
        for h in range(24):
            if h == 18:
                continue  # 缺失：无任何提交
            if h == 5:
                rows.append(mk(d1, 5, 60, 150, 1))   # r1 迟到 90 分钟
                rows.append(mk(d1, 5, 60, 55, 2))    # r2 准时 -> r1 被新修订替代
            elif h == 7:
                rows.append(mk(d1, 7, 60, 110, 1))   # 迟到 50 分钟（宽限 30 阻断 / 60 放行）
            elif h == 13:
                rows.append(mk(d1, 13, 60, 55, 1))
                rows.append(mk(d1, 13, 60, 55, 1))   # 同一修订重复提交
            elif h == 22:
                # 截至时刻（今天 06:00）之后才提交
                sub = datetime(today.year, today.month, today.day, 10, tzinfo=logic.UTC)
                start = datetime(d1.year, d1.month, d1.day, tzinfo=logic.UTC)
                h22 = start + timedelta(hours=22)
                rows.append((catalog_id, logic.iso(h22),
                             logic.iso(h22 + timedelta(minutes=60)),
                             logic.iso(sub), 1, now_iso))
            else:
                rows.append(mk(d1, h, 60, 55, 1))
        conn.executemany(
            "INSERT INTO partitions(catalog_id, partition_hour, planned_deadline,"
            " submitted_at, revision, created_at) VALUES (?,?,?,?,?,?)", rows)

        reports = [("运营日报", 1, 60), ("设备日报", 1, 30), ("前日回顾报", 2, 30)]
        conn.executemany(
            "INSERT INTO reports(catalog_id, name, offset_days, grace_minutes, created_at)"
            " VALUES (?,?,?,?,?)",
            [(catalog_id, n, o, g, now_iso) for n, o, g in reports])

        bump_version(conn, catalog_id)
        conn.commit()
        as_of = datetime(today.year, today.month, today.day, 6, tzinfo=logic.UTC)
        return {"catalog_id": catalog_id, "business_date": today.isoformat(),
                "as_of": logic.iso(as_of),
                "catalog_version": current_version(conn, catalog_id)}

    return app


app = create_app()
