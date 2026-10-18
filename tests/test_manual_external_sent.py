import tempfile
from pathlib import Path

import pytest

from bosshunter.db import (
    JobDeletionConfirmationError,
    JobManualSentConflictError,
    get_db,
    insert_job,
    mark_external_jobs_sent,
)


def _job(job_id: str, platform: str) -> dict:
    urls = {
        "zhilian": "https://www.zhaopin.com/jobdetail/example.htm",
        "liepin": "https://www.liepin.com/job/1234567890.shtml",
    }
    return {
        "id": job_id,
        "title": "产品经理",
        "company": "示例公司",
        "source_platform": platform,
        "source_job_id": job_id,
        "url": urls.get(platform, "https://jobs.51job.com/example.html"),
    }


def test_boss_manual_sent_is_atomic_and_idempotent():
    with tempfile.TemporaryDirectory() as temporary:
        db = get_db(Path(temporary) / "jobs.db")
        try:
            insert_job(db, _job("boss-manual", "boss"))

            first = mark_external_jobs_sent(db, ["boss-manual"], confirmed=True)
            second = mark_external_jobs_sent(db, ["boss-manual"], confirmed=True)
            status = db.execute("SELECT status FROM jobs WHERE id = ?", ("boss-manual",)).fetchone()["status"]
            history = db.execute(
                "SELECT action, detail FROM history WHERE job_id = ? ORDER BY id",
                ("boss-manual",),
            ).fetchall()
        finally:
            db.close()

    assert first["affected_count"] == 1
    assert second == {"requested_count": 1, "affected_count": 0, "already_sent": ["boss-manual"]}
    assert status == "sent"
    assert [(row["action"], row["detail"]) for row in history] == [
        ("manual_sent", "用户在BOSS 直聘完成投递后手动标记"),
    ]


def test_manual_sent_rejects_other_platforms_and_does_not_partially_update_boss_jobs():
    with tempfile.TemporaryDirectory() as temporary:
        db = get_db(Path(temporary) / "jobs.db")
        try:
            insert_job(db, _job("external", "51job"))
            insert_job(db, _job("boss", "boss"))
            with pytest.raises(JobManualSentConflictError) as captured:
                mark_external_jobs_sent(db, ["external", "boss"], confirmed=True)
            statuses = {
                row["id"]: row["status"]
                for row in db.execute("SELECT id, status FROM jobs WHERE id IN ('external', 'boss')").fetchall()
            }
        finally:
            db.close()

    assert captured.value.blocked == [{
        "job_id": "external",
        "reasons": ["仅 BOSS 直聘岗位支持手动标记已投递"],
    }]
    assert statuses == {"boss": "pending", "external": "pending"}


def test_manual_sent_requires_explicit_confirmation():
    with tempfile.TemporaryDirectory() as temporary:
        db = get_db(Path(temporary) / "jobs.db")
        try:
            insert_job(db, _job("external", "51job"))
            with pytest.raises(JobDeletionConfirmationError):
                mark_external_jobs_sent(db, ["external"], confirmed=False)
        finally:
            db.close()


