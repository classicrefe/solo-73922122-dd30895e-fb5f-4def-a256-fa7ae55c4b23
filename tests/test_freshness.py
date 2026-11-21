"""端到端自测：跨日窗口、迟到/宽限、重复修订、修订替代、缺失、未提交、结果失效。"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from app.main import create_app

DAY = "2026-11-20"      # 分区日期
BIZ = "2026-11-21"      # 日报业务日期（offset=1 -> 依赖 DAY）
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
    """生成某一日 24 个准时分区，可跳过/覆盖指定小时。"""
    parts = []
    for h in range(24):
        if skip and h in skip:
            continue
        if overrides and h in overrides:
            parts.extend(overrides[h])
        else:
            parts.append(mk_partition(h, day=day))
    return parts


def setup_catalog(client: "TestClient", partitions: list[dict],
                  reports: list[dict] | None = None) -> int:
    cid = client.post("/api/catalogs", json={"name": "t"}).json()["id"]
    if partitions:
        r = client.post(f"/api/catalogs/{cid}/partitions", json={"partitions": partitions})
        assert r.status_code == 201, r.text
    for rep in (reports if reports is not None else
                [{"name": "日报A", "offset_days": 1, "grace_minutes": 30}]):
        r = client.post(f"/api/catalogs/{cid}/reports", json=rep)
        assert r.status_code == 201, r.text
    return cid


def run_check(client: "TestClient", cid: int, business_date: str = BIZ,
              as_of: str = AS_OF) -> dict:
    r = client.post(f"/api/catalogs/{cid}/checks",
                    json={"business_date": business_date, "as_of": as_of})
    assert r.status_code == 201, r.text
    return r.json()


# ---------- 跨日 ----------

def test_cross_day_window_and_watermark(client):
    """offset=1 的日报在业务日 11-21 应使用 11-20 的 24 个分区，全部准时就绪。"""
    cid = setup_catalog(client, full_day())
    check = run_check(client, cid)
    res = check["results"][0]
    assert res["status"] == "READY"
    assert res["window_start"] == "2026-11-20T00:00:00+00:00"
    assert res["window_end"] == "2026-11-20T23:00:00+00:00"
    assert res["watermark_hour"] == "2026-11-20T23:00:00+00:00"
    assert res["block_reason"] is None


def test_offset_two_uses_day_before_yesterday(client):
    """offset=2 跨两日：11-21 的日报依赖 11-19 的分区；只传 11-20 会在首小时缺失阻断。"""
    cid = setup_catalog(client, full_day(),
                        reports=[{"name": "回顾报", "offset_days": 2, "grace_minutes": 30}])
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "BLOCKED"
    assert res["block_reason"] == "MISSING"
    assert res["block_hour"] == "2026-11-19T00:00:00+00:00"
    assert res["watermark_hour"] is None


# ---------- 迟到与宽限 ----------

def test_late_beyond_grace_blocks(client):
    """13 点分区迟到 50 分钟 > 宽限 30：水位停在 12 点，阻断原因为 LATE。"""
    parts = full_day(overrides={13: [mk_partition(13, submitted_min=110)]})  # 01:50 提交
    cid = setup_catalog(client, parts)
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "BLOCKED"
    assert res["block_reason"] == "LATE"
    assert res["block_hour"] == "2026-11-20T13:00:00+00:00"
    assert res["watermark_hour"] == "2026-11-20T12:00:00+00:00"
    assert "宽限 30 分钟" in res["block_message"]


def test_late_within_grace_usable(client):
    """同样的迟到 50 分钟，宽限 60 则可用，但逐小时明细中标记为宽限内迟到。"""
    parts = full_day(overrides={13: [mk_partition(13, submitted_min=110)]})
    cid = setup_catalog(client, parts,
                        reports=[{"name": "日报A", "offset_days": 1, "grace_minutes": 60}])
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "READY"
    assert res["watermark_hour"] == "2026-11-20T23:00:00+00:00"
    d13 = res["details"][13]
    assert d13["state"] == "late_within_grace" and d13["usable"] is True
    assert res["counts"]["late_within_grace"] == 1


# ---------- 重复提交 / 修订替代 ----------

def test_duplicate_revision_blocks(client):
    """09 点同一修订被提交两次：阻断，原因 DUPLICATE。"""
    parts = full_day(overrides={9: [mk_partition(9), mk_partition(9)]})
    cid = setup_catalog(client, parts)
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "BLOCKED"
    assert res["block_reason"] == "DUPLICATE"
    assert res["block_hour"] == "2026-11-20T09:00:00+00:00"
    assert res["watermark_hour"] == "2026-11-20T08:00:00+00:00"
    # 分区列表中两条都标记 duplicate
    plist = client.get(f"/api/catalogs/{cid}/partitions").json()["partitions"]
    dup = [p for p in plist if p["partition_hour"].startswith("2026-11-20T09")]
    assert len(dup) == 2 and all(p["status"] == "duplicate" for p in dup)


def test_superseded_revision_not_blocking(client):
    """05 点 r1 迟到 2 小时，r2 准时：只有 r2 生效，r1 标记为已被替代，不阻断。"""
    parts = full_day(overrides={5: [mk_partition(5, submitted_min=180, revision=1),
                                    mk_partition(5, submitted_min=55, revision=2)]})
    cid = setup_catalog(client, parts)
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "READY"
    d5 = res["details"][5]
    assert d5["revision"] == 2 and d5["superseded_count"] == 1 and d5["usable"] is True
    plist = client.get(f"/api/catalogs/{cid}/partitions").json()["partitions"]
    r1 = [p for p in plist if p["partition_hour"].startswith("2026-11-20T05")
          and p["revision"] == 1][0]
    assert r1["status"] == "superseded"
    assert "late" in r1["flags"]  # 迟到事实仍保留在标记中


# ---------- 缺失 / 截至前未提交 ----------

def test_missing_partition_blocks(client):
    """18 点无任何提交：水位停在 17 点，原因 MISSING。"""
    cid = setup_catalog(client, full_day(skip={18}))
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "BLOCKED"
    assert res["block_reason"] == "MISSING"
    assert res["block_hour"] == "2026-11-20T18:00:00+00:00"
    assert res["watermark_hour"] == "2026-11-20T17:00:00+00:00"


def test_pending_after_as_of_blocks(client):
    """22 点分区在截至时刻之后才提交：按“截至前未提交”阻断。"""
    parts = full_day(overrides={22: [{
        "partition_hour": "2026-11-20T22:00:00+00:00",
        "planned_deadline": "2026-11-20T23:00:00+00:00",
        "submitted_at": "2026-11-21T10:00:00+00:00",
        "revision": 1,
    }]})
    cid = setup_catalog(client, parts)
    res = run_check(client, cid)["results"][0]
    assert res["status"] == "BLOCKED"
    assert res["block_reason"] == "PENDING"
    assert res["block_hour"] == "2026-11-20T22:00:00+00:00"
    assert res["watermark_hour"] == "2026-11-20T21:00:00+00:00"


# ---------- 结果失效 ----------

def test_check_stale_after_catalog_update(client):
    """目录更新（上传分区/改依赖）后，旧核查结果标记为已失效。"""
    cid = setup_catalog(client, full_day())
    check = run_check(client, cid)
    checks = client.get(f"/api/catalogs/{cid}/checks").json()["checks"]
    assert checks[0]["stale"] is False

    # 上传新分区 -> 旧核查失效
    client.post(f"/api/catalogs/{cid}/partitions",
                json={"partitions": [mk_partition(0, day="2026-11-21")]})
    checks = client.get(f"/api/catalogs/{cid}/checks").json()["checks"]
    assert checks[0]["stale"] is True
    detail = client.get(f"/api/checks/{check['id']}").json()
    assert detail["stale"] is True

    # 新核查基于新版本，恢复有效
    check2 = run_check(client, cid)
    checks = client.get(f"/api/catalogs/{cid}/checks").json()["checks"]
    by_id = {c["id"]: c for c in checks}
    assert by_id[check2["id"]]["stale"] is False
    assert by_id[check["id"]]["stale"] is True

    # 修改日报依赖 -> 同样失效
    client.post(f"/api/catalogs/{cid}/reports",
                json={"name": "日报B", "offset_days": 1, "grace_minutes": 0})
    checks = client.get(f"/api/catalogs/{cid}/checks").json()["checks"]
    assert all(c["stale"] for c in checks)


# ---------- 演示数据 ----------

def test_demo_dataset_story(client):
    """演示目录：运营日报被重复提交阻断、设备日报被超宽限迟到阻断、前日回顾报就绪。"""
    demo = client.post("/api/demo").json()
    cid = demo["catalog_id"]
    check = run_check(client, cid, demo["business_date"], demo["as_of"])
    by_name = {r["report_name"]: r for r in check["results"]}

    ops = by_name["运营日报"]          # 宽限 60：07 点迟到 50 分放行，13 点重复提交阻断
    assert ops["status"] == "BLOCKED" and ops["block_reason"] == "DUPLICATE"
    assert ops["block_hour"].endswith("T13:00:00+00:00")
    assert ops["watermark_hour"].endswith("T12:00:00+00:00")
    states = {d["hour"][11:13]: d["state"] for d in ops["details"]}
    assert states["07"] == "late_within_grace"
    assert states["18"] == "missing"
    assert states["22"] == "pending"
    d5 = [d for d in ops["details"] if d["hour"][11:13] == "05"][0]
    assert d5["superseded_count"] == 1 and d5["revision"] == 2

    dev = by_name["设备日报"]          # 宽限 30：07 点迟到 50 分超宽限阻断
    assert dev["status"] == "BLOCKED" and dev["block_reason"] == "LATE"
    assert dev["block_hour"].endswith("T07:00:00+00:00")
    assert dev["watermark_hour"].endswith("T06:00:00+00:00")

    retro = by_name["前日回顾报"]       # offset=2：前天分区全部准时
    assert retro["status"] == "READY"
    assert retro["watermark_hour"].endswith("T23:00:00+00:00")


# ---------- 输入校验 ----------

def test_validation_errors(client):
    cid = setup_catalog(client, [])
    r = client.post(f"/api/catalogs/{cid}/partitions",
                    json={"partitions": [{"partition_hour": "not-a-time",
                                          "planned_deadline": "2026-11-20T01:00:00Z",
                                          "submitted_at": "2026-11-20T00:55:00Z"}]})
    assert r.status_code == 422
    r = client.post(f"/api/catalogs/{cid}/checks",
                    json={"business_date": "2026/11/21", "as_of": AS_OF})
    assert r.status_code == 422
    assert client.get("/api/catalogs/999").status_code == 404
