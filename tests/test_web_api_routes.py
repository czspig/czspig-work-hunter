import io
import json
import tempfile
import unittest
from socketserver import ThreadingMixIn
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from unittest.mock import MagicMock, patch
from wsgiref.simple_server import WSGIServer
from zipfile import ZipFile

import yaml
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from bosshunter.db import (
    add_history,
    edit_job_greeting,
    get_db,
    insert_job,
    mark_existing_greeting_ready,
    reject_jobs,
    save_generated_greeting,
    update_job_greeting,
    update_job_score,
    update_job_status,
)
from bosshunter.throttle import SendWindowChecker
from bosshunter.web import server

from bosshunter.scoring_run_store import create_scoring_run, get_scoring_run, update_scoring_run
from bosshunter.collection_run_store import create_collection_run, update_collection_run
from bosshunter.web.tasks import WorkbenchTask

# datetime.UTC 自 Python 3.11 才提供，而 pyproject.toml 声明支持 >=3.10
try:
    from datetime import UTC
except ImportError:
    from datetime import timezone as _timezone

    UTC = _timezone.utc


def _job(job_id: str) -> dict:
    return {
        "id": job_id,
        "title": "Product Manager",
        "company": "Example",
        "salary": "20-30K",
        "city": "Shanghai",
        "experience": "1-3 years",
        "jd": "Build AI product features",
        "hr_name": "HR",
        "hr_title": "Recruiter",
        "hr_active": "",
        "company_size": "",
        "company_industry": "",
        "url": "https://example.com/job",
    }


class WebApiRouteTests(unittest.TestCase):
    def test_send_window_save_reload_and_invalid_update_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))
            windows = ["14:00-16:00", "09:00-12:00"]
            status, _, body = self._request("/api/config", "POST", {"throttle": {"send_windows": windows}})
            self.assertTrue(status.startswith("200"), body)
            status, _, body = self._request("/api/config")
            self.assertEqual(json.loads(body)["throttle"]["send_windows"], windows)
            original = server.CONFIG_PATH.read_bytes()
            for invalid in (None, "09:00-16:00", ["bad"], ["09:00-12:00", "11:00-16:00"]):
                with self.subTest(invalid=invalid):
                    status, _, body = self._request("/api/config", "POST", {"throttle": {"send_windows": invalid}})
                    self.assertTrue(status.startswith("400"), body)
                    self.assertIn("error", json.loads(body))
                    self.assertEqual(server.CONFIG_PATH.read_bytes(), original)

    def setUp(self):
        # Arrange
        self.original_base_dir = server.BASE_DIR

    def tearDown(self):
        # Cleanup
        server.set_base_dir(self.original_base_dir)

    def test_outsourcing_rules_refresh_old_jobs_in_every_review_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, {**_job("outsourcing-review"), "company": "测试供应商"})
                update_job_score(db, "outsourcing-review", 85, "synthetic score")
                update_job_status(db, "outsourcing-review", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)
            paths = {
                "/api/jobs": lambda payload: payload[0],
                "/api/jobs/search": lambda payload: payload["items"][0],
                "/api/jobs/outsourcing-review": lambda payload: payload,
                "/api/workbench": lambda payload: payload["pending_confirmation"][0],
            }
            versions = []
            for enabled, expected in ((True, "confirmed"), (False, "clean")):
                (base_dir / "config.yaml").write_text(yaml.safe_dump({
                    "outsourcing_rules": {"companies_user": ["测试供应商"], "enabled": enabled},
                }), encoding="utf-8")
                for path, get_record in paths.items():
                    with self.subTest(enabled=enabled, path=path):
                        status, _, body = self._request(path)
                        self.assertTrue(status.startswith("200"), body)
                        record = get_record(json.loads(body))
                        self.assertEqual(record["outsourcing_level"], expected)
                        self.assertEqual(record["outsourcing_confirmed"], enabled)
                        self.assertIsInstance(record["outsourcing_matches"], list)
                        self.assertEqual(record["status"], "ready")
                versions.append(record["outsourcing_rules_version"])
            self.assertNotEqual(*versions)

    def test_bad_outsourcing_json_cannot_break_job_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("malformed-outsourcing"))
                server.set_base_dir(base_dir)
                for raw in ("1", "null", "{}", '[{"keyword":{}}]'):
                    db.execute("UPDATE jobs SET outsourcing_matches=?,outsourcing_layers=?", (raw, raw))
                    db.commit()
                    status, _, body = self._request("/api/jobs/malformed-outsourcing")
                    self.assertTrue(status.startswith("200"), body)
                    record = json.loads(body)
                    self.assertEqual(record["outsourcing_matches"], [])
                    self.assertEqual(record["outsourcing_layers"], [])
            finally:
                db.close()


    def _request(self, path: str, method: str = "GET", json_body: dict | None = None, environ_overrides=None):
        if "?" in path:
            path_info, query_string = path.split("?", 1)
        else:
            path_info, query_string = path, ""

        status_headers = {}

        def start_response(status, headers, exc_info=None):
            status_headers["status"] = status
            status_headers["headers"] = dict(headers)

        request_body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        environ = {
            "REMOTE_ADDR": "127.0.0.1",
            "REQUEST_METHOD": method,
            "PATH_INFO": path_info,
            "QUERY_STRING": query_string,
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8686",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(request_body),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        if json_body is not None:
            environ["CONTENT_LENGTH"] = str(len(request_body))
            environ["CONTENT_TYPE"] = "application/json"

        environ.update(environ_overrides or {})
        response_iter = server.app(environ, start_response)
        try:
            body = b"".join(
                chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                for chunk in response_iter
            ).decode("utf-8")
        finally:
            close = getattr(response_iter, "close", None)
            if close:
                close()
        return status_headers["status"], status_headers["headers"], body


    def test_model_list_uses_draft_settings_and_preserves_saved_config(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            server.set_base_dir(Path(tmp))
            server._write_config({"ai": {"service": "custom", "provider": "openai_compatible", "base_url": "https://saved.example/v1", "api_key": "saved-secret", "model": "saved-model"}})
            before = server.CONFIG_PATH.read_bytes()
            for draft_key, expected_key in [("", "saved-secret"), ("draft-secret", "draft-secret")]:
                with patch.object(server, "list_ai_models", return_value=["model-a"]) as discover:
                    status, _, body = self._request("/api/config/models", "POST", {"ai": {"service": "custom", "base_url": "https://draft.example/v1", "api_key": draft_key}})
                self.assertTrue(status.startswith("200"), body)
                self.assertEqual(json.loads(body), {"models": ["model-a"]})
                draft = discover.call_args.args[0]["ai"]
                self.assertEqual(draft["base_url"], "https://draft.example/v1")
                self.assertEqual(draft["api_key"], expected_key)
                self.assertEqual(server.CONFIG_PATH.read_bytes(), before)
                self.assertEqual(server.load_config(server.CONFIG_PATH)["ai"]["api_key"], "saved-secret")
            with patch.object(server, "list_ai_models", return_value=[]) as discover:
                self._request("/api/config/models", "POST", {"ai": {"service": "deepseek", "clear_credentials": True}})
                self.assertNotIn("api_key", discover.call_args.args[0]["ai"])

    def test_model_list_rejects_bad_input_and_hides_unexpected_errors(self):
        for payload in ({}, {"ai": []}, {"ai": {"base_url": []}}, {"ai": {"api_key": 123}}):
            with patch.object(server, "list_ai_models") as discover:
                status, _, _ = self._request("/api/config/models", "POST", payload)
                self.assertTrue(status.startswith("400"))
                discover.assert_not_called()
        with patch.object(server, "_sanitize_config_for_write", return_value={}), patch.object(server, "list_ai_models", side_effect=RuntimeError("private-secret")):
            status, _, body = self._request("/api/config/models", "POST", {"ai": {}})
        self.assertTrue(status.startswith("500"))
        self.assertNotIn("private-secret", body)

    def _upload_resume(self, filename: str, content: bytes, content_type: str):
        boundary = "----BossHunterResumeUpload"
        body = (
            (
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
            + content
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        status_headers = {}

        def start_response(status, headers, exc_info=None):
            status_headers["status"] = status
            status_headers["headers"] = dict(headers)

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/resume/upload",
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8686",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }

        response_body = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
            for chunk in server.app(environ, start_response)
        ).decode("utf-8")
        return status_headers["status"], status_headers["headers"], response_body

    @patch.object(server.app, "run")
    def test_run_server_uses_threaded_wsgi_server(self, run):
        # Act
        server.run_server(open_browser=False)

        # Assert
        server_class = run.call_args.kwargs["server_class"]
        self.assertTrue(issubclass(server_class, ThreadingMixIn))
        self.assertTrue(issubclass(server_class, WSGIServer))
        self.assertTrue(server_class.daemon_threads)

    def test_web_api_missing_api_route_returns_json_404_not_spa_html(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))

            # Act
            status, headers, body = self._request("/api/does-not-exist")

        # Assert
        self.assertTrue(status.startswith("404"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), {"error": "Not found"})
        self.assertNotIn("<!doctype html", body.lower())


    def test_web_assets_serve_javascript_with_windows_safe_mime_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            frontend_dir = Path(tmp)
            assets_dir = frontend_dir / "assets"
            assets_dir.mkdir()
            (assets_dir / "app.js").write_text("console.log('ok')\n", encoding="utf-8")

            with patch.object(server, "FRONTEND_DIR", frontend_dir):
                status, headers, body = self._request("/assets/app.js")

        self.assertTrue(status.startswith("200"))
        self.assertTrue(headers["Content-Type"].startswith("application/javascript"))
        self.assertIn("console.log", body)

    def test_web_api_workbench_preflight_full_returns_json_payload(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "resume.md"
            resume_path.write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(resume_path)},
                        "search": {"keywords": ["AI产品经理"]},
                        "ai": {"api_key": "test-api-key"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            ready_checks = [
                {
                    "id": "environment",
                    "title": "运行环境",
                    "status": "pass",
                    "message": "启动检查已通过",
                    "detail": "测试环境已就绪",
                    "action": "",
                }
            ]
            with patch.object(server, "collect_preflight_checks", return_value=ready_checks):
                status, headers, body = self._request("/api/workbench/preflight?mode=full")

        # Assert
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), {"ok": True, "messages": [], "checks": ready_checks})

    def test_web_api_workbench_preflight_supports_rescore_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "resume.md"
            resume_path.write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(resume_path)},
                        "ai": {"api_key": "test-api-key"},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)
            ready_checks = [
                {
                    "id": "ai_credentials",
                    "title": "AI API",
                    "status": "pass",
                    "message": "AI 已连接",
                    "detail": "",
                    "action": "",
                }
            ]

            with patch.object(server, "collect_preflight_checks", return_value=ready_checks) as collect:
                status, headers, body = self._request("/api/workbench/preflight?mode=rescore")

        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(collect.call_args.args[0], "rescore")

    def test_web_api_workbench_preflight_full_requires_ai_key(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "resume.md"
            resume_path.write_text("# Resume", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump(
                    {
                        "profile": {"resume_path": str(resume_path)},
                        "search": {"keywords": ["AI产品经理"]},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            browser_ready = {
                "node": {"available": True, "version": "v22"},
                "runtime": True,
                "chrome": True,
                "targets": [],
                "boss_tab": None,
                "errors": [],
                "runtime_url": "http://127.0.0.1:3456",
                "health": {"runtime": "bosshunter"},
                "browser_product": "Chrome/138.0",
            }
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("bosshunter.web.preflight.run_browser_diagnostics", return_value=browser_ready),
            ):
                status, headers, body = self._request("/api/workbench/preflight?mode=full")

        # Assert
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("尚未填写 AI API Key" in message for message in payload["messages"]))
        self.assertTrue(any(check["id"] == "ai_credentials" for check in payload["checks"]))

    def test_web_api_ai_diagnostics_returns_structured_feedback(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)
            checks = [
                {
                    "id": "ai_credentials",
                    "title": "AI API Key",
                    "status": "error",
                    "message": "尚未填写 AI API Key",
                    "detail": "请填写 API Key。",
                    "action": "config",
                }
            ]

            # Act
            with patch.object(server, "check_ai_connection", return_value=checks):
                status, headers, body = self._request("/api/diagnostics/ai")

        # Assert
        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["checks"], checks)
        self.assertIn("尚未填写 AI API Key", payload["messages"][0])

    def test_web_api_activity_returns_json_without_runtime_name_error(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))

            # Act
            status, headers, body = self._request("/api/activity?days=7")

        # Assert
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body), [])

    def test_job_search_filters_keyword_score_salary_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                matching = _job("matching")
                matching.update({"title": "实施顾问", "salary": "5-8K", "jd": "负责 SQL 系统实施"})
                insert_job(db, matching)
                update_job_score(db, "matching", 82, "数据库技能匹配")
                update_job_status(db, "matching", "ready")

                wrong_status = _job("wrong-status")
                wrong_status.update({"title": "实施工程师", "salary": "8-13K", "jd": "需要 SQL"})
                insert_job(db, wrong_status)
                update_job_score(db, "wrong-status", 85, "数据库技能匹配")
                update_job_status(db, "wrong-status", "filtered")

                low_score = _job("low-score")
                low_score.update({"salary": "10-15K", "jd": "需要 SQL"})
                insert_job(db, low_score)
                update_job_score(db, "low-score", 60, "数据库技能匹配")
                update_job_status(db, "low-score", "ready")

                unrelated = _job("unrelated")
                unrelated.update({"salary": "10-15K", "jd": "负责客户培训"})
                insert_job(db, unrelated)
                update_job_score(db, "unrelated", 88, "沟通能力匹配")
                update_job_status(db, "unrelated", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, headers, body = self._request(
                "/api/jobs/search?q=SQL&min_score=71&salary_min=7&salary_max=13&status=ready&limit=15&offset=0"
            )

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual([job["id"] for job in payload["items"]], ["matching"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["all_total"], 4)
        self.assertEqual(payload["limit"], 15)
        self.assertEqual(payload["offset"], 0)

    def test_job_search_hides_blocked_companies_before_pagination_without_deleting_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            config_path = base_dir / "config.yaml"
            config_path.write_text(yaml.safe_dump({"profile": {"blocked_companies": ["  BLOCKED  "]}}))
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for job_id, company, score, status_value in (
                    ("blocked-high", "Blocked Technology", 99, "ready"),
                    ("visible-high", "Visible A", 90, "ready"),
                    ("blocked-middle", "Another blocked Company", 85, "ready"),
                    ("visible-low", "Visible B", 80, "ready"),
                    ("visible-filtered", "Visible C", 60, "filtered"),
                    ("deleted", "Visible D", 95, "ready"),
                ):
                    insert_job(db, {**_job(job_id), "company": company})
                    update_job_score(db, job_id, score, "匹配")
                    update_job_status(db, job_id, status_value)
                db.execute("UPDATE jobs SET deleted_at = datetime('now') WHERE id = 'deleted'")
                db.commit()
                before = [tuple(row) for row in db.execute("SELECT id, score, status, deleted_at FROM jobs ORDER BY id")]
            finally:
                db.close()
            server.set_base_dir(base_dir)

            for offset, expected_id in ((0, "visible-high"), (1, "visible-low")):
                status, _, body = self._request(f"/api/jobs/search?status=ready&sort_by=score&limit=1&offset={offset}")
                self.assertTrue(status.startswith("200"), body)
                payload = json.loads(body)
                self.assertEqual([job["id"] for job in payload["items"]], [expected_id])
                self.assertEqual(payload["total"], 2)
                self.assertEqual(payload["all_total"], 3)

            config_path.write_text(yaml.safe_dump({"profile": {"blocked_companies": []}}))
            status, _, body = self._request("/api/jobs/search?status=ready&sort_by=score")
            self.assertTrue(status.startswith("200"), body)
            payload = json.loads(body)
            self.assertEqual([job["id"] for job in payload["items"]], ["blocked-high", "visible-high", "blocked-middle", "visible-low"])
            self.assertEqual(payload["total"], 4)
            self.assertEqual(payload["all_total"], 5)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                self.assertEqual(
                    [tuple(row) for row in db.execute("SELECT id, score, status, deleted_at FROM jobs ORDER BY id")], before
                )
            finally:
                db.close()

    def test_job_search_combines_blacklist_with_city_filters_and_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text(yaml.safe_dump({"profile": {"blocked_companies": ["blocked"]}}))
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for job_id, company, city, score in (
                    ("blocked-sh", "Blocked Technology", "上海·浦东", 99),
                    ("visible-sh", "Visible A", "上海·徐汇", 90),
                    ("visible-bj", "Visible B", "北京", 80),
                    ("outside-cities", "Visible C", "深圳", 70),
                ):
                    insert_job(db, {**_job(job_id), "company": company, "city": city, "title": "前端实习生"})
                    update_job_score(db, job_id, score, "匹配")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            for offset, expected_id in ((0, "visible-sh"), (1, "visible-bj")):
                status, _, body = self._request(
                    f"/api/jobs/search?city={quote('上海')}&city={quote('北京')}&sort_by=score&limit=1&offset={offset}"
                )
                self.assertTrue(status.startswith("200"), body)
                payload = json.loads(body)
                self.assertEqual([job["id"] for job in payload["items"]], [expected_id])
                self.assertEqual(payload["items"][0]["employment_type"], "internship")
                self.assertEqual(payload["total"], 2)
                self.assertEqual(payload["all_total"], 3)
                self.assertCountEqual(payload["city_options"], ["上海", "北京", "深圳"])

    def test_job_search_treats_blacklist_rules_as_literal_substrings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text(yaml.safe_dump({"profile": {"blocked_companies": ["100%", "_hold", " "]}}))
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for job_id, company in (("percent", "100% Real"), ("underscore", "My_Holdings"), ("visible", "100x Real Holdings")):
                    insert_job(db, {**_job(job_id), "company": company})
            finally:
                db.close()
            server.set_base_dir(base_dir)
            status, _, body = self._request("/api/jobs/search")
            self.assertTrue(status.startswith("200"), body)
            payload = json.loads(body)
            self.assertEqual([job["id"] for job in payload["items"]], ["visible"])
            self.assertEqual(payload["total"], 1)
            self.assertEqual(payload["all_total"], 1)

    def test_job_search_supports_repeated_multi_select_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                fixtures = [
                    ("boss-ready", "boss", "experienced", "本科", "ready"),
                    ("zhilian-filtered", "zhilian", "campus", "硕士", "filtered"),
                    ("liepin-ready", "liepin", "experienced", "大专", "ready"),
                ]
                for job_id, platform, recruitment_type, education, status_value in fixtures:
                    job = _job(job_id)
                    job.update({"source_platform": platform, "recruitment_type": recruitment_type, "education": education})
                    insert_job(db, job)
                    update_job_status(db, job_id, status_value)
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                "/api/jobs/search?source_platform=boss&source_platform=zhilian&"
                "recruitment_type=experienced&recruitment_type=campus&status=ready&status=filtered"
            )

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertCountEqual([job["id"] for job in payload["items"]], ["zhilian-filtered", "boss-ready"])

    def test_job_search_filters_by_city_and_reports_city_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for job_id, city in (("sh", "Shanghai"), ("sh-pd", "Shanghai·Pudong"), ("bj", "Beijing"), ("blank", "")):
                    job = _job(job_id)
                    job["city"] = city
                    insert_job(db, job)
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/jobs/search?city=Shanghai&city=Beijing")

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertCountEqual([job["id"] for job in payload["items"]], ["sh", "sh-pd", "bj"])
        self.assertCountEqual(payload["city_options"], ["Shanghai", "Beijing"])

    def test_job_search_decodes_chinese_query_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                bachelor = _job("bachelor")
                bachelor.update({"education": "本科", "city": "北京"})
                insert_job(db, bachelor)
                master = _job("master")
                master.update({"education": "硕士", "city": "上海"})
                insert_job(db, master)
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                f"/api/jobs/search?education={quote('本科')}&city={quote('北京')}"
            )

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual([job["id"] for job in payload["items"]], ["bachelor"])

    def test_job_search_salary_overlap_excludes_unparseable_and_paginates(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                fixtures = [
                    ("high", "12K", 90),
                    ("middle", "10-15K", 85),
                    ("low", "5-8K", 80),
                    ("daily", "150-200元/天", 95),
                ]
                for job_id, salary, score in fixtures:
                    job = _job(job_id)
                    job["salary"] = salary
                    insert_job(db, job)
                    update_job_score(db, job_id, score, "匹配")
                    update_job_status(db, job_id, "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                "/api/jobs/search?salary_min=7&salary_max=13&limit=2&offset=1"
            )

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(payload["total"], 3)
        self.assertEqual([job["id"] for job in payload["items"]], ["middle", "low"])

    def test_job_search_supports_whitelisted_column_sorting(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for job_id, score, education in (("low", 60, "本科"), ("high", 90, "博士")):
                    job = _job(job_id)
                    job["education"] = education
                    insert_job(db, job)
                    update_job_score(db, job_id, score, "评分")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/jobs/search?sort_by=score&sort_order=asc")
            invalid_sort_status, _, invalid_sort_body = self._request(
                "/api/jobs/search?sort_by=score%20DESC&sort_order=asc"
            )
            invalid_order_status, _, invalid_order_body = self._request(
                "/api/jobs/search?sort_by=score&sort_order=sideways"
            )

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual([job["id"] for job in json.loads(body)["items"]], ["low", "high"])
        self.assertTrue(invalid_sort_status.startswith("400"), invalid_sort_body)
        self.assertTrue(invalid_order_status.startswith("400"), invalid_order_body)

    def test_job_search_decodes_chinese_keyword_as_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                job = _job("chinese-keyword")
                job["company"] = "网易"
                insert_job(db, job)
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(f"/api/jobs/search?q={quote('网易')}")

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual([job["id"] for job in payload["items"]], ["chinese-keyword"])

    def test_job_search_orders_newest_jobs_before_higher_scored_older_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("older-high-score"))
                update_job_score(db, "older-high-score", 95, "高分旧岗位")
                insert_job(db, _job("newer-low-score"))
                update_job_score(db, "newer-low-score", 60, "低分新岗位")
                now = datetime.now(UTC).replace(tzinfo=None)
                db.execute(
                    "UPDATE jobs SET created_at = ? WHERE id = ?",
                    ((now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"), "older-high-score"),
                )
                db.execute(
                    "UPDATE jobs SET created_at = ? WHERE id = ?",
                    (now.strftime("%Y-%m-%d %H:%M:%S"), "newer-low-score"),
                )
                db.commit()
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/jobs/search")

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(
            [job["id"] for job in payload["items"]],
            ["newer-low-score", "older-high-score"],
        )

    def test_job_search_filters_jobs_by_collection_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                now = datetime.now(UTC).replace(tzinfo=None)
                fixtures = (
                    ("today", now),
                    ("recent", now - timedelta(days=2)),
                    ("older", now - timedelta(days=8)),
                )
                for job_id, created_at in fixtures:
                    insert_job(db, _job(job_id))
                    db.execute(
                        "UPDATE jobs SET created_at = ? WHERE id = ?",
                        (created_at.strftime("%Y-%m-%d %H:%M:%S"), job_id),
                    )
                db.commit()
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/jobs/search?created_within=3d")
            today_status, _, today_body = self._request("/api/jobs/search?created_within=today")

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual([job["id"] for job in payload["items"]], ["today", "recent"])
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["all_total"], 3)
        today_payload = json.loads(today_body)
        self.assertTrue(today_status.startswith("200"), today_body)
        self.assertEqual([job["id"] for job in today_payload["items"]], ["today"])

    def test_job_search_rejects_invalid_numeric_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            server.set_base_dir(Path(tmp))
            paths = [
                "/api/jobs/search?min_score=not-a-number",
                "/api/jobs/search?salary_min=14&salary_max=7",
                "/api/jobs/search?limit=0",
                "/api/jobs/search?created_within=30d",
            ]

            for path in paths:
                status, headers, body = self._request(path)
                self.assertTrue(status.startswith("400"), body)
                self.assertIn("application/json", headers["Content-Type"])
                self.assertIn("error", json.loads(body))

    def test_legacy_jobs_endpoint_still_returns_an_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("legacy"))
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/jobs?limit=100")

        self.assertTrue(status.startswith("200"), body)
        self.assertIsInstance(json.loads(body), list)

    def test_web_api_workbench_pending_confirmation_returns_ready_jobs(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("ready-job"))
                update_job_score(db, "ready-job", 82, "good match")
                update_job_status(db, "ready-job", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, headers, body = self._request("/api/workbench")

        # Assert
        payload = json.loads(body)
        self.assertTrue(status.startswith("200"))
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual([job["id"] for job in payload["pending_confirmation"]], ["ready-job"])


    def test_web_api_workbench_shows_approved_job_when_greeting_was_interrupted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("approved-without-greeting"))
                update_job_score(db, "approved-without-greeting", 84, "good match")
                update_job_status(db, "approved-without-greeting", "approved")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/workbench")

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(
            [job["id"] for job in payload["pending_confirmation"]],
            ["approved-without-greeting"],
        )

    def test_workbench_returns_today_and_cumulative_funnel_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                # Funnel "today" uses the machine's local calendar day. Keep fixtures
                # in the same clock so this remains stable around local midnight.
                now = datetime.now()
                fixtures = (
                    ("today-ready", "ready", now),
                    ("today-sent", "sent", now - timedelta(hours=1)),
                    ("older-sent", "sent", now - timedelta(days=2)),
                )
                for job_id, job_status, created_at in fixtures:
                    insert_job(db, _job(job_id))
                    update_job_score(db, job_id, 80, "匹配")
                    update_job_status(db, job_id, job_status)
                    db.execute(
                        "UPDATE jobs SET created_at = ? WHERE id = ?",
                        (created_at.strftime("%Y-%m-%d %H:%M:%S"), job_id),
                    )
                db.commit()
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request("/api/workbench")

        payload = json.loads(body)
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(payload["funnel"]["采集总数"], 3)
        self.assertEqual(payload["funnel"]["发送"], 2)
        self.assertEqual(payload["funnel_today"]["采集总数"], 2)
        self.assertEqual(payload["funnel_today"]["AI评分"], 2)
        self.assertEqual(payload["funnel_today"]["发送"], 1)
        self.assertEqual(len(payload["pending_confirmation"]), 1)


    def test_send_window_checker_uses_last_window_end_as_daily_deadline(self):
        checker = SendWindowChecker(["09:00-12:00", "14:00-17:30"])

        deadline = checker.latest_end_datetime(datetime(2026, 7, 28, 10, 15, 45))

        self.assertEqual(deadline, datetime(2026, 7, 28, 17, 30))


    def test_web_api_manual_status_updates_history_and_blocks_sent_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                ready = _job("manual-status-ready")
                sent = _job("manual-status-sent")
                insert_job(db, ready)
                insert_job(db, sent)
                update_job_status(db, sent["id"], "sent")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                "/api/jobs/status", method="POST",
                json_body={"job_ids": [ready["id"]], "status": "skipped"},
            )
            blocked_status, _, blocked_body = self._request(
                "/api/jobs/status", method="POST",
                json_body={"job_ids": [sent["id"]], "status": "ready"},
            )
            verify_db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                row = verify_db.execute("SELECT status FROM jobs WHERE id = ?", (ready["id"],)).fetchone()
                history = verify_db.execute("SELECT action, detail FROM history WHERE job_id = ?", (ready["id"],)).fetchall()
            finally:
                verify_db.close()

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(json.loads(body)["affected_count"], 1)
        self.assertTrue(blocked_status.startswith("409"), blocked_body)
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(history[0]["action"], "status_changed")
        self.assertIn("pending", history[0]["detail"])


    def test_web_api_workbench_reject_marks_selected_ready_jobs_rejected(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reject-a"))
                update_job_score(db, "reject-a", 82, "good match")
                update_job_status(db, "reject-a", "ready")

                insert_job(db, _job("reject-b"))
                update_job_score(db, "reject-b", 72, "ok match")
                update_job_status(db, "reject-b", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = json.dumps({"job_ids": ["reject-a", "reject-b"]}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/workbench/reject",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            response_body = b"".join(
                chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                for chunk in server.app(environ, start_response)
            ).decode("utf-8")

            verify_db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                statuses = {
                    row["id"]: row["status"]
                    for row in verify_db.execute(
                        "SELECT id, status FROM jobs WHERE id IN ('reject-a', 'reject-b')"
                    ).fetchall()
                }
                history_actions = [
                    dict(row)
                    for row in verify_db.execute(
                        "SELECT job_id, action, detail FROM history ORDER BY id"
                    ).fetchall()
                ]
            finally:
                verify_db.close()

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertEqual(json.loads(response_body), {"success": True, "count": 2})
        self.assertEqual(statuses, {"reject-a": "rejected", "reject-b": "rejected"})
        self.assertEqual(
            history_actions,
            [
                {"job_id": "reject-a", "action": "rejected", "detail": "Web Dashboard 放弃投递"},
                {"job_id": "reject-b", "action": "rejected", "detail": "Web Dashboard 放弃投递"},
            ],
        )

    def test_web_api_workbench_reject_rejects_mixed_status_batch_without_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reject-safe"))
                update_job_status(db, "reject-safe", "ready")
                insert_job(db, _job("reject-sent"))
                update_job_greeting(db, "reject-sent", "已发送文本")
                update_job_status(db, "reject-sent", "sent")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                "/api/workbench/reject",
                method="POST",
                json_body={"job_ids": ["reject-safe", "reject-sent"]},
            )

            verify_db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                rows = verify_db.execute(
                    "SELECT id, status FROM jobs WHERE id IN ('reject-safe', 'reject-sent') ORDER BY id"
                ).fetchall()
                history = verify_db.execute(
                    "SELECT action FROM history WHERE job_id IN ('reject-safe', 'reject-sent')"
                ).fetchall()
            finally:
                verify_db.close()

        payload = json.loads(body)
        self.assertTrue(status.startswith("409"), body)
        self.assertEqual(payload["code"], "reject_status_blocked")
        self.assertEqual(payload["invalid_ids"], ["reject-sent"])
        self.assertEqual([(row["id"], row["status"]) for row in rows], [
            ("reject-safe", "ready"),
            ("reject-sent", "sent"),
        ])
        self.assertEqual(history, [])

    def test_web_api_workbench_reject_allows_error_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reject-error"))
                update_job_status(db, "reject-error", "error")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                "/api/workbench/reject",
                method="POST",
                json_body={"job_ids": ["reject-error"]},
            )

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(json.loads(body), {"success": True, "count": 1})


    def test_save_generated_greeting_rejects_allowed_status_transition(self):
        # 审计 P2 回归：approved→error 等允许状态间的并发变化必须拒绝，不能写回 ready。
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("cas-transition"))
                update_job_status(db, "cas-transition", "approved")
                # 模拟读取（approved）之后、写入之前状态被并发改为 error
                update_job_status(db, "cas-transition", "error")

                self.assertFalse(
                    save_generated_greeting(
                        db, "cas-transition", "AI 新招呼语", expected_status="approved"
                    )
                )
                row = db.execute(
                    "SELECT greeting, status FROM jobs WHERE id = 'cas-transition'"
                ).fetchone()
                self.assertIsNone(row["greeting"])
                self.assertEqual(row["status"], "error")

                # 不传 expected_status 维持旧行为：允许集合内仍可写入
                self.assertTrue(save_generated_greeting(db, "cas-transition", "AI 新招呼语"))
                row = db.execute(
                    "SELECT greeting, status FROM jobs WHERE id = 'cas-transition'"
                ).fetchone()
                self.assertEqual(row["greeting"], "AI 新招呼语")
                self.assertEqual(row["status"], "ready")
            finally:
                db.close()

    def test_mark_existing_greeting_ready_rejects_allowed_status_transition(self):
        # Codex 审计 P2 回归：保留现有招呼语同样必须钉扎状态，approved→error 后不得复活为 ready。
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("preserve-transition"))
                update_job_greeting(db, "preserve-transition", "人工编辑的招呼语")
                update_job_status(db, "preserve-transition", "approved")
                # 模拟读取（approved）之后、保留之前状态被并发改为 error
                update_job_status(db, "preserve-transition", "error")

                self.assertFalse(
                    mark_existing_greeting_ready(
                        db,
                        "preserve-transition",
                        expected_greeting="人工编辑的招呼语",
                        expected_status="approved",
                    )
                )
                row = db.execute(
                    "SELECT greeting, status FROM jobs WHERE id = 'preserve-transition'"
                ).fetchone()
                self.assertEqual(row["status"], "error")
            finally:
                db.close()


    def test_db_greeting_mutations_refuse_terminal_or_stale_statuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp) / "bosshunter.db")
            try:
                insert_job(db, _job("db-ready"))
                update_job_status(db, "db-ready", "ready")
                self.assertTrue(save_generated_greeting(db, "db-ready", "AI 招呼语"))

                insert_job(db, _job("db-manual-race"))
                update_job_status(db, "db-manual-race", "ready")
                update_job_greeting(db, "db-manual-race", "并发人工编辑")
                self.assertFalse(save_generated_greeting(
                    db,
                    "db-manual-race",
                    "过期 AI 结果",
                    expected_greeting="旧快照",
                ))

                insert_job(db, _job("db-sent"))
                update_job_greeting(db, "db-sent", "已发送文本")
                update_job_status(db, "db-sent", "sent")
                self.assertFalse(save_generated_greeting(db, "db-sent", "不应保存"))
                self.assertFalse(edit_job_greeting(db, "db-sent", "不应编辑", expected_status="sent"))

                insert_job(db, _job("db-existing"))
                update_job_greeting(db, "db-existing", "人工文本")
                update_job_status(db, "db-existing", "approved")
                self.assertFalse(mark_existing_greeting_ready(
                    db,
                    "db-existing",
                    expected_greeting="旧快照",
                ))

                insert_job(db, _job("db-reject-ready"))
                update_job_status(db, "db-reject-ready", "ready")
                insert_job(db, _job("db-reject-scored"))
                update_job_status(db, "db-reject-scored", "scored")
                result = reject_jobs(db, ["db-reject-ready", "db-reject-scored"])
                self.assertEqual(result["affected_count"], 0)
                self.assertEqual(result["invalid_ids"], ["db-reject-scored"])

                rows = {
                    row["id"]: dict(row)
                    for row in db.execute(
                        "SELECT id, greeting, status FROM jobs WHERE id LIKE 'db-%'"
                    ).fetchall()
                }
                history = db.execute(
                    "SELECT action FROM history WHERE job_id LIKE 'db-%'"
                ).fetchall()
            finally:
                db.close()

        self.assertEqual((rows["db-manual-race"]["greeting"], rows["db-manual-race"]["status"]), ("并发人工编辑", "ready"))
        self.assertEqual((rows["db-sent"]["greeting"], rows["db-sent"]["status"]), ("已发送文本", "sent"))
        self.assertEqual(rows["db-existing"]["status"], "approved")
        self.assertEqual(rows["db-reject-ready"]["status"], "ready")
        self.assertEqual(rows["db-reject-scored"]["status"], "scored")
        self.assertEqual(history, [])

    def test_web_api_workbench_reject_removes_jobs_from_pending_confirmation(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("reject-visible"))
                update_job_score(db, "reject-visible", 82, "good match")
                update_job_status(db, "reject-visible", "ready")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            body = json.dumps({"job_ids": ["reject-visible"]}).encode("utf-8")
            status_headers = {}

            def start_response(status, headers, exc_info=None):
                status_headers["status"] = status
                status_headers["headers"] = dict(headers)

            environ = {
                "REQUEST_METHOD": "POST",
                "PATH_INFO": "/api/workbench/reject",
                "QUERY_STRING": "",
                "CONTENT_LENGTH": str(len(body)),
                "CONTENT_TYPE": "application/json",
                "SERVER_NAME": "127.0.0.1",
                "SERVER_PORT": "8686",
                "wsgi.version": (1, 0),
                "wsgi.url_scheme": "http",
                "wsgi.input": io.BytesIO(body),
                "wsgi.errors": io.StringIO(),
                "wsgi.multithread": False,
                "wsgi.multiprocess": False,
                "wsgi.run_once": False,
            }

            # Act
            response_body = b"".join(
                chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                for chunk in server.app(environ, start_response)
            ).decode("utf-8")
            workbench_status, _, workbench_body = self._request("/api/workbench")

        # Assert
        self.assertTrue(status_headers["status"].startswith("200"), response_body)
        self.assertTrue(workbench_status.startswith("200"), workbench_body)
        self.assertEqual(json.loads(workbench_body)["pending_confirmation"], [])

    def test_web_api_resume_delete_only_detaches_config_and_keeps_master_resume_file(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            resume_path = base_dir / "data" / "resumes" / "_AI_Homepage.md"
            resume_path.parent.mkdir(parents=True, exist_ok=True)
            resume_path.write_text("# 主简历\n\n完整事实库，不能删减。\n", encoding="utf-8")
            (base_dir / "config.yaml").write_text(
                yaml.dump({"profile": {"resume_path": str(resume_path)}}, allow_unicode=True),
                encoding="utf-8",
            )
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/resume", method="DELETE")
            config = yaml.safe_load((base_dir / "config.yaml").read_text(encoding="utf-8"))

            # Assert
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(json.loads(body), {"success": True})
            self.assertTrue(resume_path.exists())
            self.assertEqual(config["profile"]["resume_path"], "")

    def test_web_api_resume_upload_preserves_chinese_markdown_filename(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)
            content = "# 张三\n\n产品经理\n".encode("utf-8")

            # Act
            status, _, body = self._upload_resume("张三的中文简历.md", content, "text/markdown")
            payload = json.loads(body)
            stored_path = Path(payload["path"])
            config = yaml.safe_load((base_dir / "config.yaml").read_text(encoding="utf-8"))

            # Assert
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(payload["filename"], "张三的中文简历.md")
            self.assertEqual(stored_path.read_bytes(), content)
            self.assertEqual(config["profile"]["resume_path"], str(stored_path))

    def test_web_api_resume_upload_converts_docx_to_markdown(self):
        # Arrange
        document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body>
            <w:p>
              <w:pPr><w:pStyle w:val="Title"/></w:pPr>
              <w:r><w:t>李雷</w:t></w:r>
            </w:p>
            <w:p>
              <w:pPr><w:numPr><w:ilvl w:val="0"/></w:numPr></w:pPr>
              <w:r><w:t>5 年产品经验</w:t></w:r>
            </w:p>
          </w:body>
        </w:document>"""
        docx_buffer = io.BytesIO()
        with ZipFile(docx_buffer, "w") as archive:
            archive.writestr("word/document.xml", document_xml)

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._upload_resume(
                "李雷简历.docx",
                docx_buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            payload = json.loads(body)
            stored_path = Path(payload["path"])
            stored_content = stored_path.read_text(encoding="utf-8")

            # Assert
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(payload["filename"], "李雷简历.md")
            self.assertEqual(stored_path.suffix, ".md")
            self.assertIn("# 李雷", stored_content)
            self.assertIn("- 5 年产品经验", stored_content)

    def test_web_api_resume_upload_extracts_text_layer_from_pdf(self):
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        contents = DecodedStreamObject()
        contents.set_data(b"BT /F1 12 Tf 72 720 Td (Jane Resume - Product Manager) Tj ET")
        page[NameObject("/Contents")] = contents
        pdf = io.BytesIO()
        writer.write(pdf)

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)

            status, _, body = self._upload_resume("resume.pdf", pdf.getvalue(), "application/pdf")
            payload = json.loads(body)
            stored_path = Path(payload["path"])
            stored_text = stored_path.read_text(encoding="utf-8")

        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(payload["filename"], "resume.md")
        self.assertIn("Jane Resume - Product Manager", stored_text)

    def test_web_api_resume_upload_rejects_encrypted_pdf(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.encrypt("secret")
        pdf = io.BytesIO()
        writer.write(pdf)

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)
            status, _, body = self._upload_resume("encrypted.pdf", pdf.getvalue(), "application/pdf")

        self.assertTrue(status.startswith("400"), body)
        self.assertEqual(json.loads(body), {"error": "PDF 已加密，请上传未加密的简历"})

    def test_web_api_resume_upload_rejects_damaged_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)
            status, _, body = self._upload_resume("damaged.pdf", b"%PDF-1.7\nbroken", "application/pdf")

        self.assertTrue(status.startswith("400"), body)
        self.assertEqual(json.loads(body), {"error": "PDF 文件无效或已损坏"})

    def test_web_api_resume_upload_rejects_scanned_pdf_without_text_layer(self):
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        pdf = io.BytesIO()
        writer.write(pdf)

        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)
            status, _, body = self._upload_resume("scanned.pdf", pdf.getvalue(), "application/pdf")

        self.assertTrue(status.startswith("400"), body)
        self.assertIn("扫描版或无文字层", json.loads(body)["error"])

    def test_web_api_resume_upload_rejects_legacy_doc_format(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            (base_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._upload_resume("旧版简历.doc", b"not-a-word-file", "application/msword")

            # Assert
            self.assertTrue(status.startswith("400"), body)
            self.assertEqual(json.loads(body), {"error": "仅支持 .md、.docx 或 .pdf 格式"})


    def test_web_api_history_can_include_unresolved_resume_failures_outside_recent_limit(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("resume-failed-open"))
                insert_job(db, _job("resume-failed-resolved"))
                insert_job(db, _job("reply-open"))
                insert_job(db, _job("recent-job"))
                add_history(db, "resume-failed-open", "resume_failed", "仍需处理")
                add_history(db, "resume-failed-resolved", "resume_failed", "旧失败")
                add_history(db, "resume-failed-resolved", "resume_sent", "后来已成功")
                add_history(db, "reply-open", "reply_pending", "待确认回复")
                add_history(db, "recent-job", "sent", "最近记录")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/history?limit=1&include_unresolved=1")

        # Assert
        self.assertTrue(status.startswith("200"), body)
        payload = json.loads(body)
        self.assertEqual(
            {(item["job_id"], item["action"]) for item in payload},
            {
                ("recent-job", "sent"),
                ("reply-open", "reply_pending"),
                ("resume-failed-open", "resume_failed"),
            },
        )

    def test_web_api_history_includes_last_week_replies_outside_recent_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                for job_id in ("recent-reply", "recent-resume-sent", "expired-reply", "recent-noise"):
                    insert_job(db, _job(job_id))
                add_history(
                    db,
                    "recent-reply",
                    "auto_replied",
                    json.dumps({"schema": "auto_replied.v1", "ai_reply": "近一周回复"}),
                )
                add_history(
                    db,
                    "expired-reply",
                    "replied",
                    json.dumps({"schema": "replied.external.v1", "manual_reply": "过期回复"}),
                )
                add_history(
                    db,
                    "recent-resume-sent",
                    "needs_resume",
                    json.dumps({"schema": "needs_resume.v1", "conversation_tail": [{"sender": "hr", "text": "请发简历"}]}),
                )
                add_history(db, "recent-resume-sent", "resume_sent", "简历已发送")
                db.execute(
                    "UPDATE history SET created_at = datetime('now', '-8 days') WHERE job_id = ?",
                    ("expired-reply",),
                )
                db.commit()
                add_history(db, "recent-noise", "sent", "最近普通记录")
            finally:
                db.close()
            server.set_base_dir(base_dir)

            status, _, body = self._request(
                "/api/history?limit=1&include_monitor_conversations=1"
            )
            full_status, _, full_body = self._request(
                "/api/history?limit=50&include_monitor_conversations=1"
            )

        self.assertTrue(status.startswith("200"), body)
        self.assertTrue(full_status.startswith("200"), full_body)
        expected = {
            ("recent-noise", "sent"),
            ("recent-reply", "auto_replied"),
            ("recent-resume-sent", "needs_resume"),
            ("recent-resume-sent", "resume_sent"),
        }
        for payload in (body, full_body):
            self.assertEqual(
                {(item["job_id"], item["action"]) for item in json.loads(payload)},
                expected,
            )

    def test_web_api_history_exposes_structured_failure_reason_and_resolution_state(self):
        # Arrange
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("resume-failed-detail"))
                add_history(
                    db,
                    "resume-failed-detail",
                    "resume_failed",
                    json.dumps(
                        {
                            "schema": "resume_failed.v2",
                            "hr_question": "请发一份简历。",
                            "ai_reply": "",
                            "system_reason": "事实完整性校验失败：新增了 50%",
                            "conversation_tail": [],
                        },
                        ensure_ascii=False,
                    ),
                )
            finally:
                db.close()
            server.set_base_dir(base_dir)

            # Act
            status, _, body = self._request("/api/history?limit=10&include_unresolved=1")
            unresolved_item = json.loads(body)[0]

            db = get_db(base_dir / "data" / "bosshunter.db")
            try:
                db.execute(
                    "UPDATE jobs SET resume_path = ? WHERE id = ?",
                    ("/tmp/generated.md", "resume-failed-detail"),
                )
                db.commit()
            finally:
                db.close()
            _, _, resolved_body = self._request("/api/history?limit=10&include_unresolved=1")
            resolved_item = json.loads(resolved_body)[0]

        # Assert
        self.assertTrue(status.startswith("200"), body)
        self.assertEqual(unresolved_item["detail_payload"]["hr_question"], "请发一份简历。")
        self.assertEqual(
            unresolved_item["detail_payload"]["system_reason"],
            "事实完整性校验失败：新增了 50%",
        )
        self.assertFalse(unresolved_item["resolved"])
        self.assertEqual(unresolved_item["url"], "https://example.com/job")
        self.assertEqual(unresolved_item["source_platform"], "boss")
        self.assertTrue(resolved_item["resolved"])
        self.assertEqual(resolved_item["resume_path"], "/tmp/generated.md")

    def test_collection_resume_preflight_and_start_use_saved_search_without_saving_preferences(self):
        config = {"search": {"keywords": ["new defaults"], "cities": ["上海"]}}
        saved_options = server.normalize_collection_options({}, {"platform_order": ["boss"], "platforms": {
            "boss": {"keywords": ["original"], "cities": ["北京"], "max_pages": 2,
                     "filters": {"experience": ["1-3年"]}},
        }})
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, "DATA_DIR", Path(tmp)), \
             patch.object(server, "load_config", return_value=config), \
             patch.object(server, "_write_config") as write_config, \
             patch.object(server, "collect_preflight_checks", return_value=[]) as preflight, \
             patch.object(server, "_preflight_messages", return_value=[]), \
             patch.object(server.task_runner, "start", return_value={"id": "resumed"}) as start:
            db_path = Path(tmp) / "bosshunter.db"
            create_collection_run(db_path, run_id="original", options=saved_options,
                                  platform_states={"boss": {"status": "stopped"}}, enable_boss_resume=True)
            update_collection_run(db_path, "original", status="stopped")
            payload = {"mode": "collect", "options": {"resume_run_id": "original",
                       "platforms": {"boss": {"keywords": ["tampered"]}}}}
            status, _, body = self._request("/api/collection/runs")
            self.assertTrue(status.startswith("200"), body)
            self.assertTrue(json.loads(body)[0]["can_resume"])
            status, _, body = self._request("/api/workbench/preflight", "POST", payload)
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(preflight.call_args.args[2]["platforms"], saved_options["platforms"])
            status, _, body = self._request("/api/workbench/task", "POST", payload)
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(start.call_args.args[0], "collect")
            actual = start.call_args.args[1]["_collection_options"]
            self.assertEqual(actual, {**saved_options, "resume_run_id": "original"})
            write_config.assert_not_called()

            for run_status in ("running", "completed"):
                update_collection_run(db_path, "original", status=run_status)
                start.reset_mock()
                status, _, body = self._request("/api/workbench/task", "POST", payload)
                self.assertTrue(status.startswith("400"), body)
                start.assert_not_called()
            status, _, body = self._request("/api/workbench/task", "POST", {**payload, "mode": "full"})
            self.assertTrue(status.startswith("400"), body)

    def test_full_task_receives_global_boss_collection_options(self):
        config = {
            "search": {"keywords": ["旧关键词"], "cities": ["北京"]},
            "profile": {"target_cities": ["北京"]},
            "collection": {"default_order": ["boss"]},
            "platforms": {
                "boss": {"enabled": True, "search": {"keywords": ["全局关键词"], "cities": ["上海"], "max_pages": 2, "sort": "newest", "target_count": 4}},
                "zhilian": {"enabled": False, "search": {}},
            },
        }
        with patch.object(server, "load_config", return_value=config), patch.object(server, "_preflight_messages", return_value=[]), patch.object(
            server, "_write_config"
        ), patch.object(server.task_runner, "start", return_value={"id": "full-global-config"}) as start:
            status, _, body = self._request("/api/workbench/task", method="POST", json_body={"mode": "full"})

        self.assertTrue(status.startswith("200"), body)
        task_config = start.call_args.args[1]
        self.assertEqual(task_config["_collection_options"]["platforms"]["boss"]["keywords"], ["全局关键词"])
        self.assertEqual(task_config["_collection_options"]["platforms"]["boss"]["cities"], ["上海"])
        self.assertNotIn("target_count", task_config["_collection_options"]["platforms"]["boss"])
        self.assertTrue(task_config["_collection_options"]["auto_score"])


    def _seed_scoring_base(self, base_dir: Path):
        (base_dir / "config.yaml").write_text(
            yaml.dump({"ai": {"api_key": "test-api-key"}}, allow_unicode=True),
            encoding="utf-8",
        )
        server.set_base_dir(base_dir)
        db = get_db(server.DATA_DIR / "bosshunter.db")
        try:
            insert_job(db, _job("p1"))
        finally:
            db.close()

    def _seed_run(self, run_id: str, status: str):
        db_path = server.DATA_DIR / "bosshunter.db"
        create_scoring_run(
            db_path,
            run_id=run_id,
            options={"scope": "pending", "limit": None, "force_rescore": False},
            job_ids=["p1"],
        )
        update_scoring_run(db_path, run_id, status=status, pause_reason="AI 服务请求失败" if status == "paused" else None)

    def test_scoring_start_reports_paused_run_with_machine_readable_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_scoring_base(Path(tmp))
            self._seed_run("run-paused", "paused")

            with patch.object(server, "_preflight_messages", return_value=[]):
                status, _, body = self._request(
                    "/api/scoring/start",
                    method="POST",
                    json_body={"options": {"scope": "pending", "limit": None, "job_ids": [], "force_rescore": False}},
                )

        payload = json.loads(body)
        self.assertTrue(status.startswith("409"), body)
        self.assertEqual(payload.get("code"), "scoring_run_paused")
        self.assertIn("强制开始新任务", payload["error"])

    def test_scoring_start_ignores_non_boolean_force_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_scoring_base(Path(tmp))
            self._seed_run("run-paused", "paused")
            runner = MagicMock()

            for bad_force in ("false", "true", 1):
                with patch.object(server, "_preflight_messages", return_value=[]), patch.object(server, "task_runner", runner):
                    status, _, body = self._request(
                        "/api/scoring/start",
                        method="POST",
                        json_body={
                            "force": bad_force,
                            "options": {"scope": "pending", "limit": None, "job_ids": [], "force_rescore": False},
                        },
                    )

                payload = json.loads(body)
                self.assertTrue(status.startswith("409"), body)
                self.assertEqual(payload.get("code"), "scoring_run_paused")

            old_run = get_scoring_run(server.DATA_DIR / "bosshunter.db", "run-paused")
            self.assertEqual(old_run["status"], "paused")
            runner.start.assert_not_called()

    def test_scoring_start_with_force_ends_paused_run_and_starts_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_scoring_base(Path(tmp))
            self._seed_run("run-paused", "paused")
            runner = MagicMock()
            runner.start.return_value = {"id": "task-1", "status": "running"}

            with patch.object(server, "_preflight_messages", return_value=[]), patch.object(server, "task_runner", runner):
                status, _, body = self._request(
                    "/api/scoring/start",
                    method="POST",
                    json_body={
                        "force": True,
                        "options": {"scope": "pending", "limit": None, "job_ids": [], "force_rescore": False},
                    },
                )

            payload = json.loads(body)
            old_run = get_scoring_run(server.DATA_DIR / "bosshunter.db", "run-paused")
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(old_run["status"], "stopped")
            self.assertIn("强制结束", old_run["error"])
            self.assertEqual(payload["run"]["status"], "running")
            self.assertNotEqual(payload["run"]["id"], "run-paused")
            runner.start.assert_called_once()

    def test_scoring_start_with_force_still_rejects_active_running_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_scoring_base(Path(tmp))
            self._seed_run("run-active", "running")
            runner = MagicMock()

            with patch.object(server, "_preflight_messages", return_value=[]), patch.object(server, "task_runner", runner):
                status, _, body = self._request(
                    "/api/scoring/start",
                    method="POST",
                    json_body={
                        "force": True,
                        "options": {"scope": "pending", "limit": None, "job_ids": [], "force_rescore": False},
                    },
                )

        payload = json.loads(body)
        self.assertTrue(status.startswith("409"), body)
        self.assertIn("正在运行", payload["error"])
        runner.start.assert_not_called()

    def test_score_checkpoint_writes_error_for_ai_pause_but_not_user_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_scoring_base(Path(tmp))
            self._seed_run("run-checkpoint", "running")
            task = WorkbenchTask(id="task-ckpt", mode="score", label="单独 AI 评分")
            ai_pause = "AI 服务请求失败 (request_failed, status=404)"
            states = [
                {"remaining_job_ids": ["p1"], "status": "paused", "pause_reason": ai_pause, "error": ai_pause},
                {"remaining_job_ids": ["p1"], "status": "paused", "pause_reason": "用户暂停或任务中断", "error": ""},
            ]

            def fake_score_jobs(config, *args, **kwargs):
                callback = config.get("_workbench_score_checkpoint")
                for state in states:
                    callback(dict(state))

            with patch("bosshunter.ai.scorer.score_jobs", side_effect=fake_score_jobs), patch.object(
                server, "update_scoring_run", return_value=None
            ) as update_run:
                server._execute_score(task, {"_score_run_id": "run-checkpoint", "_score_options": {}})

            paused_calls = [call for call in update_run.call_args_list if call.kwargs.get("status") == "paused"]
            self.assertEqual(paused_calls[0].kwargs.get("error"), ai_pause)
            self.assertEqual(paused_calls[1].kwargs.get("error"), None)
            self.assertEqual(task.error, ai_pause)
            self.assertTrue(task.stop_requested.is_set())


    def test_outreach_resume_can_be_read_and_explicitly_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server.set_base_dir(root)
            source_path = root / "data" / "resumes" / "job-review.md"
            image_path = root / "data" / "resumes" / "job-review.png"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("# 候选人\n\n## 教育经历\n本科\n", encoding="utf-8")
            image_path.write_bytes(b"png")
            db = get_db(root / "data" / "bosshunter.db")
            try:
                insert_job(db, _job("job-review"))
                db.execute(
                    """
                    UPDATE jobs
                    SET resume_source_path = ?, resume_image_path = ?,
                        resume_review_status = 'needs_review', resume_generation_source = 'deepseek'
                    WHERE id = ?
                    """,
                    (str(source_path), str(image_path), "job-review"),
                )
                db.commit()
            finally:
                db.close()

            status, _, body = self._request("/api/jobs/job-review/outreach-resume")
            self.assertTrue(status.startswith("200"), body)
            payload = json.loads(body)
            self.assertEqual(payload["status"], "needs_review")
            self.assertEqual(payload["source"], "deepseek")
            self.assertIn("教育经历", payload["markdown"])
            self.assertNotIn(str(source_path), body)

            status, _, body = self._request(
                "/api/jobs/job-review/outreach-resume/review",
                method="POST",
                json_body={"confirmed": True},
            )
            self.assertTrue(status.startswith("200"), body)
            self.assertEqual(json.loads(body)["status"], "ready")

            db = get_db(root / "data" / "bosshunter.db")
            try:
                row = db.execute(
                    "SELECT resume_review_status, resume_reviewed_at FROM jobs WHERE id = ?",
                    ("job-review",),
                ).fetchone()
            finally:
                db.close()
            self.assertEqual(row["resume_review_status"], "ready")
            self.assertIsNotNone(row["resume_reviewed_at"])


if __name__ == "__main__":
    unittest.main()
