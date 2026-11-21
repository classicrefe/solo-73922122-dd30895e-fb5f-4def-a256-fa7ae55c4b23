"""历史阻断分析自测。

覆盖：
- 每个业务日取截至时刻最晚的核查；时刻相同取最后保存的一次；
- 当天入选核查就绪时，较早的阻断不计入；
- 按首个阻断原因统计天数，可回看原核查；
- 目录更新后旧核查（stale）仍参与统计；
- 日报按身份归档：软删除后同名重建分开统计；
- 无法确认身份的旧记录单列为未归属；
- 日期范围与参数校验。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.logic import analyze_blocking, daterange, latest_checks_by_day

DAY = "2026-11-20"
BIZ = "2026-11-21"
AS_OF = "2026-11-21T06:00:00+00:00"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def mk_partition(hour: int, submitted_min: int = 55, revision: int = 1,
                 deadline_min: int = 60, day: str = DAY) -> dict:
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    h = start + timedelta(hours=hour)
    return {
        "partition_hour": h.isoformat(),
        "planned_deadline": (h + timedelta(minutes=deadline_min)).isoformat(),
        "submitted_at": (h + timedelta(minutes=submitted_min)).isoformat(),
        "revision": revision,
    }


def full_day(day: str = DAY, skip: set[int] | None = None,
             overrides: dict[int, list[dict]] | None = None) -> list[dict]:
    parts = []
    for h in range(24):
        if skip and h in skip:
            continue
        if overrides and h in overrides:
            parts.extend(overrides[h])
        else:
            parts.append(mk_partition(h, day=day))
    return parts


def setup_catalog(client, partitions, reports=None) -> int:
    cid = client.post("/api/catalogs", json={"name": "t"}).json()["id"]
    if partitions:
        r = client.post(f"/api/catalogs/{cid}/partitions",
                        json={"partitions": partitions})
        assert r.status_code == 201, r.text
    for rep in (reports if reports is not None else
                [{"name": "日报A", "offset_days": 1, "grace_minutes": 30}]):
        r = client.post(f"/api/catalogs/{cid}/reports", json=rep)
        assert r.status_code == 201, r.text
    return cid


def run_check(client, cid, business_date=BIZ, as_of=AS_OF) -> dict:
    r = client.post(f"/api/catalogs/{cid}/checks",
                    json={"business_date": business_date, "as_of": as_of})
    assert r.status_code == 201, r.text
    return r.json()


def analysis(client, cid, rid, start="2026-11-15", end="2026-11-21",
             unassigned=False):
    params = f"start_date={start}&end_date={end}"
    params += "&unassigned=true" if unassigned else f"&report_id={rid}"
    r = client.get(f"/api/catalogs/{cid}/analysis?{params}")
    assert r.status_code == 200, r.text
    return r.json()


def block_partitions(kind: str, day: str = DAY) -> list[dict]:
    """构造在 10 点分区按指定原因阻断的一日分区（分区日 = 业务日 - 偏移 1 天）。"""
    if kind == "MISSING":
        return full_day(day=day, skip={10})
    if kind == "LATE":
        return full_day(day=day, overrides={10: [mk_partition(10, submitted_min=110, day=day)]})
    if kind == "DUPLICATE":
        return full_day(day=day, overrides={10: [mk_partition(10, day=day),
                                                 mk_partition(10, day=day)]})
    if kind == "PENDING":
        submitted = (datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
                     + timedelta(days=1, hours=12)).isoformat()
        return full_day(day=day, overrides={10: [{
            "partition_hour": f"{day}T10:00:00+00:00",
            "planned_deadline": f"{day}T11:00:00+00:00",
            "submitted_at": submitted,
            "revision": 1,
        }]})
    raise ValueError(kind)


# ---------- 核心统计语义 ----------

def test_analysis_counts_first_block_reason_by_day(client):
    """两个业务日分别因 MISSING / LATE 阻断：各计 1 天；无核查日不计阻断。"""
    # 业务日 11-19 -> 窗口分区日 11-18，MISSING 阻断
    cid = setup_catalog(client, block_partitions("MISSING", day="2026-11-18"))
    rid = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]["id"]
    run_check(client, cid, business_date="2026-11-19",
              as_of="2026-11-20T06:00:00+00:00")

    # 换目录内容（版本+1，旧核查变 stale），业务日 11-20 -> 分区日 11-19，LATE 阻断
    client.post(f"/api/catalogs/{cid}/reset")
    client.post(f"/api/catalogs/{cid}/partitions",
                json={"partitions": block_partitions("LATE", day="2026-11-19")})
    run_check(client, cid, business_date="2026-11-20",
              as_of="2026-11-21T06:00:00+00:00")

    data = analysis(client, cid, rid, start="2026-11-18", end="2026-11-21")
    assert data["reason_counts"]["MISSING"] == 1
    assert data["reason_counts"]["LATE"] == 1
    assert data["totals"]["blocked_days"] == 2
    assert data["totals"]["no_check_days"] == 2  # 11-18、11-21 无核查
    by_day = {d["business_date"]: d for d in data["days"]}
    assert by_day["2026-11-19"]["status"] == "BLOCKED"
    assert by_day["2026-11-19"]["block_reason"] == "MISSING"
    assert by_day["2026-11-19"]["stale"] is True   # 旧核查仍作为当时记录
    assert by_day["2026-11-20"]["block_reason"] == "LATE"
    assert by_day["2026-11-18"]["status"] == "NO_CHECK"


def test_latest_as_of_wins_and_ready_supersedes_early_block(client):
    """同一天两次核查：早一次阻断、晚一次就绪 -> 当天不阻断，原因计数为 0。"""
    cid = setup_catalog(client, block_partitions("DUPLICATE"))
    rid = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]["id"]
    first = run_check(client, cid, as_of="2026-11-21T06:00:00+00:00")
    assert first["results"][0]["block_reason"] == "DUPLICATE"

    # 修复重复提交后再核查（更晚的截至时刻）
    client.post(f"/api/catalogs/{cid}/reset")
    client.post(f"/api/catalogs/{cid}/partitions", json={"partitions": full_day()})
    second = run_check(client, cid, as_of="2026-11-21T09:00:00+00:00")
    assert second["results"][0]["status"] == "READY"

    data = analysis(client, cid, rid)
    assert data["totals"]["blocked_days"] == 0
    assert data["totals"]["ready_days"] == 1
    day = next(d for d in data["days"] if d["business_date"] == BIZ)
    assert day["status"] == "READY" and day["check_id"] == second["id"]
    # 较早的阻断核查仍可通过原核查详情回看
    detail = client.get(f"/api/checks/{first['id']}").json()
    assert detail["results"][0]["block_reason"] == "DUPLICATE"


def test_same_as_of_takes_last_saved(client):
    """同一业务日、同一截至时刻保存两次：取最后保存（id 最大）的一次。"""
    cid = setup_catalog(client, block_partitions("MISSING"))
    rid = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]["id"]
    run_check(client, cid, as_of=AS_OF)
    client.post(f"/api/catalogs/{cid}/reset")
    client.post(f"/api/catalogs/{cid}/partitions",
                json={"partitions": block_partitions("LATE")})
    later = run_check(client, cid, as_of=AS_OF)  # 同 as_of，后保存

    data = analysis(client, cid, rid)
    day = next(d for d in data["days"] if d["business_date"] == BIZ)
    assert day["check_id"] == later["id"]
    assert day["block_reason"] == "LATE"


def test_pending_reason_counted(client):
    cid = setup_catalog(client, block_partitions("PENDING"))
    rid = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]["id"]
    run_check(client, cid)
    data = analysis(client, cid, rid)
    assert data["reason_counts"]["PENDING"] == 1
    day = next(d for d in data["days"] if d["business_date"] == BIZ)
    assert day["block_reason"] == "PENDING" and day["check_id"]


# ---------- 日报身份：软删除 / 同名重建 / 未归属 ----------

def test_soft_delete_and_same_name_rebuild_are_separate_identities(client):
    """删除日报后同名重建：两个身份分开统计，旧核查仍挂在旧身份下。"""
    cid = setup_catalog(client, block_partitions("LATE"))
    old = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]
    run_check(client, cid)  # 旧身份在业务日 11-21 的阻断核查

    r = client.delete(f"/api/catalogs/{cid}/reports/{old['id']}")
    assert r.status_code == 200
    # 管理列表不再返回，但身份接口仍可查且标记 deleted
    assert client.get(f"/api/catalogs/{cid}/reports").json()["reports"] == []
    ids = client.get(f"/api/catalogs/{cid}/report-identities").json()
    assert ids["identities"][0]["deleted_at"] is not None

    # 同名重建 -> 新身份；在另一业务日（11-20 -> 窗口 11-19）产生 MISSING 核查
    r = client.post(f"/api/catalogs/{cid}/reports",
                    json={"name": "日报A", "offset_days": 1, "grace_minutes": 30})
    new_rid = r.json()["id"]
    assert new_rid != old["id"]
    client.post(f"/api/catalogs/{cid}/reset")
    client.post(f"/api/catalogs/{cid}/partitions",
                json={"partitions": block_partitions("MISSING", day="2026-11-19")})
    run_check(client, cid, business_date="2026-11-20",
              as_of="2026-11-21T06:00:00+00:00")
    # 新核查只包含存活的新身份，不含已删除的旧身份
    latest = client.get(f"/api/catalogs/{cid}/checks").json()["checks"][0]
    detail = client.get(f"/api/checks/{latest['id']}").json()
    assert {r["report_id"] for r in detail["results"]} == {new_rid}

    old_data = analysis(client, cid, old["id"])
    new_data = analysis(client, cid, new_rid)
    assert old_data["reason_counts"]["LATE"] == 1
    assert old_data["report_deleted"] is True
    assert new_data["reason_counts"]["MISSING"] == 1
    # 新身份在旧核查（11-21）中没有记录，旧身份在新核查（11-20）中没有记录
    old_rows = {d["business_date"]: d for d in old_data["days"]}
    new_rows = {d["business_date"]: d for d in new_data["days"]}
    assert old_rows[BIZ]["block_reason"] == "LATE"
    assert old_rows["2026-11-20"]["status"] == "NO_RECORD"
    assert new_rows["2026-11-20"]["block_reason"] == "MISSING"
    assert new_rows[BIZ]["status"] == "NO_RECORD"

    # 再删一次旧 id 应 404
    assert client.delete(f"/api/catalogs/{cid}/reports/{old['id']}").status_code == 404


def test_legacy_records_without_identity_are_unassigned(client, tmp_path):
    """历史库迁移：check_results.report_id 为 NULL 的记录单列为未归属。"""
    cid = setup_catalog(client, block_partitions("DUPLICATE"))
    rid = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]["id"]
    check = run_check(client, cid)

    # 模拟迁移前的旧记录：直接把 report_id 置空
    import sqlite3
    conn = sqlite3.connect(client.app.state.db_path)
    conn.execute("UPDATE check_results SET report_id = NULL")
    conn.commit()
    conn.close()

    ident = client.get(f"/api/catalogs/{cid}/report-identities").json()
    assert ident["unassigned_available"] is True

    # 指定身份查不到该结果 -> NO_RECORD；未归属口径能统计到 DUPLICATE
    by_id = analysis(client, cid, rid)
    day = next(d for d in by_id["days"] if d["business_date"] == BIZ)
    assert day["status"] == "NO_RECORD"

    un = analysis(client, cid, rid, unassigned=True)
    assert un["unassigned"] is True and un["report_name"] == "未归属"
    assert un["reason_counts"]["DUPLICATE"] == 1
    uday = next(d for d in un["days"] if d["business_date"] == BIZ)
    assert uday["status"] == "BLOCKED" and uday["block_reason"] == "DUPLICATE"
    assert uday["report_name"] == "日报A"
    # 原核查详情仍可打开
    detail = client.get(f"/api/checks/{check['id']}").json()
    assert detail["results"][0]["report_id"] is None


def test_migration_adds_columns_to_legacy_db(client, tmp_path):
    """旧版本数据库（无 deleted_at / report_id 列）启动时自动补列且数据保留。"""
    import sqlite3
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE catalogs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
            version INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id INTEGER,
            name TEXT, offset_days INTEGER, grace_minutes INTEGER, created_at TEXT);
        CREATE TABLE checks (id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id INTEGER,
            catalog_version INTEGER, business_date TEXT, as_of TEXT, created_at TEXT);
        CREATE TABLE check_results (id INTEGER PRIMARY KEY AUTOINCREMENT, check_id INTEGER,
            report_name TEXT, offset_days INTEGER, grace_minutes INTEGER,
            window_start TEXT, window_end TEXT, watermark_hour TEXT, status TEXT,
            block_hour TEXT, block_reason TEXT, block_message TEXT,
            counts_json TEXT, detail_json TEXT);
        CREATE TABLE partitions (id INTEGER PRIMARY KEY AUTOINCREMENT, catalog_id INTEGER,
            partition_hour TEXT, planned_deadline TEXT, submitted_at TEXT,
            revision INTEGER, created_at TEXT);
        INSERT INTO catalogs VALUES (1, '旧目录', 0, '2026-11-01T00:00:00+00:00');
        INSERT INTO reports VALUES (1, 1, '旧日报', 1, 30, '2026-11-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    app = create_app(str(legacy))
    with TestClient(app) as c:
        r = c.get("/api/catalogs/1/report-identities")
        assert r.status_code == 200
        assert r.json()["identities"][0]["name"] == "旧日报"
        assert r.json()["identities"][0]["deleted_at"] is None


# ---------- 范围与校验 ----------

def test_date_range_filter_and_validation(client):
    cid = setup_catalog(client, block_partitions("LATE"))
    rid = client.get(f"/api/catalogs/{cid}/reports").json()["reports"][0]["id"]
    run_check(client, cid)

    # 区间不含业务日 -> 全无核查
    data = analysis(client, cid, rid, start="2026-11-01", end="2026-11-05")
    assert data["totals"]["blocked_days"] == 0
    assert len(data["days"]) == 5 and all(d["status"] == "NO_CHECK" for d in data["days"])

    base = f"/api/catalogs/{cid}/analysis?start_date=2026-11-01"
    assert client.get(base + "&end_date=bad").status_code == 422
    assert client.get(base + "&end_date=2026-10-01").status_code == 422  # 起止倒置
    assert client.get(base + "&end_date=2026-11-05").status_code == 422  # 缺身份
    assert client.get(base + "&end_date=2026-11-05&unassigned=true"
                      f"&report_id={rid}").status_code == 422              # 互斥
    assert client.get(f"/api/catalogs/{cid}/analysis?start_date=2026-11-01"
                      f"&end_date=2026-11-05&report_id=99999").status_code == 404


def test_pure_helpers():
    checks = [
        {"id": 1, "business_date": "2026-11-20", "as_of": "2026-11-21T06:00:00+00:00"},
        {"id": 2, "business_date": "2026-11-20", "as_of": "2026-11-21T09:00:00+00:00"},
        {"id": 3, "business_date": "2026-11-20", "as_of": "2026-11-21T09:00:00+00:00"},
        {"id": 4, "business_date": "2026-11-19", "as_of": "2026-11-20T06:00:00+00:00"},
    ]
    chosen = latest_checks_by_day(checks)
    assert chosen["2026-11-20"]["id"] == 3   # 同 as_of 取最后保存
    assert chosen["2026-11-19"]["id"] == 4

    days = daterange(date(2026, 11, 19), date(2026, 11, 20))
    result = analyze_blocking(
        days, chosen,
        {3: [{"status": "READY", "report_name": "A"}],
         4: [{"status": "BLOCKED", "block_reason": "LATE", "report_name": "A"}]})
    assert result["reason_counts"]["LATE"] == 1
    assert result["totals"] == {"blocked_days": 1, "ready_days": 1,
                                "no_check_days": 0, "no_record_days": 0}
