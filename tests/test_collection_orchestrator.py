import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from bosshunter.collection.base import CollectorHooks
from bosshunter.collection.models import JobCandidate, PlatformCollectionResult
from bosshunter.collection.orchestrator import (
    CollectionOrchestrator,
    normalize_collection_options,
    validate_collection_options,
)
from bosshunter.collection.registry import CollectorRegistry
from bosshunter.db import get_db, insert_job


def _candidate(platform: str, source_id: str, title: str = "正常岗位") -> JobCandidate:
    return JobCandidate(
        platform=platform,
        source_job_id=source_id,
        title=title,
        company="示例公司",
        city="北京",
        city_code="530" if platform == "zhilian" else "101010100",
        jd="负责岗位相关工作",
        url=f"https://example.test/{platform}/{source_id}",
    )


def _options(*, order=None, auto_score=False):
    order = order or ["boss"]
    values = {}
    for platform in order:
        values[platform] = {
            "keywords": ["AI"],
            "cities": ["北京"],
            "city_codes": {"北京": "530"} if platform == "zhilian" else {"北京": "101010100"},
            "max_pages": 1,
            "sort": "default",
        }
    return {"platform_order": order, "auto_score": auto_score, "platforms": values}


class _FakeCollector:
    def __init__(self, platform, events, candidates, *, stop=False):
        self.platform = platform
        self.events = events
        self.candidates = candidates
        self.stop = stop

    def collect(self, _request, hooks: CollectorHooks):
        self.events.append(f"start:{self.platform}")
        for candidate in self.candidates:
            if hooks.stop_event and hooks.stop_event.is_set():
                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
            if not hooks.on_list_candidate(candidate):
                continue
            if not hooks.on_candidate(candidate):
                self.events.append(f"target:{self.platform}")
                return PlatformCollectionResult(self.platform, "completed", "target_reached", "达到目标")
            if self.stop:
                hooks.stop_event.set()
                return PlatformCollectionResult(self.platform, "stopped", "user_stopped", "用户已停止")
        return PlatformCollectionResult(self.platform, "completed", "search_exhausted", "无更多结果")


class _BlockedCollector:
    def __init__(self, platform, events, reason_code):
        self.platform = platform
        self.events = events
        self.reason_code = reason_code

    def collect(self, _request, _hooks: CollectorHooks):
        self.events.append(f"start:{self.platform}")
        return PlatformCollectionResult(self.platform, "blocked", self.reason_code, "平台已阻断")


class CollectionOrchestratorTests(TestCase):
    def test_collection_failures_preserve_context_and_remain_bounded(self):
        from bosshunter.collection_run_store import get_collection_run

        class FailureCollector:
            def collect(self, request, hooks):
                hooks.on_event(city="深圳", keyword="AI", page=2)
                for i in range(201):
                    hooks.on_parse_failed(f"详情缺少字段 {i}")
                candidate = _candidate("boss", "save-error", "保存失败测试岗位")
                if hooks.on_list_candidate(candidate):
                    hooks.on_candidate(candidate)
                return PlatformCollectionResult("boss", "completed_with_shortage", "incomplete", "部分岗位处理失败")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "errors.db"
            with patch("bosshunter.collection.orchestrator.insert_job_if_new", side_effect=RuntimeError("private data")):
                result = CollectionOrchestrator({}, db_path=path, registry=CollectorRegistry({"boss": FailureCollector})).run(_options())
            state = get_collection_run(path, result["run_id"])["platform_states"]["boss"]
            self.assertEqual(state["parse_failed"], 201)
            self.assertEqual(state["save_failed"], 1)
            records = state["error_records"]
            self.assertEqual(len(records), 200)
            self.assertEqual(records[0]["reason"], "详情缺少字段 2")
            self.assertEqual(records[0]["kind"], "parse")
            self.assertEqual(records[0]["city"], "深圳")
            self.assertEqual(records[0]["keyword"], "AI")
            self.assertEqual(records[0]["page"], 2)
            self.assertEqual(records[0]["title"], "")
            self.assertEqual(records[-1]["kind"], "save")
            self.assertEqual(records[-1]["title"], "保存失败测试岗位")
            self.assertEqual(records[-1]["reason"], "保存岗位失败：RuntimeError")
            self.assertNotIn("private data", str(records))
            self.assertEqual(result["collected_job_ids"], [])

    def test_filter_records_are_bounded_persisted_and_separate_from_jobs(self):
        from bosshunter.collection_run_store import get_collection_run

        candidates = [_candidate("boss", str(i), "外包岗位") for i in range(205)]
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", [], candidates)})
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.db"
            result = CollectionOrchestrator(
                {"profile": {"deal_breakers": ["外包"]}}, db_path=db_path, registry=registry,
            ).run(_options())
            saved = get_collection_run(db_path, result["run_id"])
            state = saved["platform_states"]["boss"]
            self.assertEqual(state["filtered"], 205)
            self.assertEqual(len(state["filter_records"]), 200)
            self.assertEqual(state["filter_records"][0]["source_job_id"], "5")
            self.assertEqual(state["filter_records"][-1]["source_job_id"], "204")
            self.assertIn("外包", state["filter_records"][0]["reason"])
            self.assertEqual(state["filter_records"][0]["company"], "示例公司")
            conn = get_db(db_path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            conn.close()


    def test_boss_zero_new_reports_duplicate_and_filtered_counts(self):
        candidates = [_candidate("boss", "duplicate"), _candidate("boss", "filtered", "外包岗位")]
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", [], candidates)})
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.db"
            conn = get_db(db_path)
            insert_job(conn, candidates[0].as_job_record())
            conn.close()
            result = CollectionOrchestrator(
                {"profile": {"deal_breakers": ["外包"]}}, db_path=db_path, registry=registry,
            ).run(_options())
        state = result["platforms"]["boss"]
        self.assertEqual(result["status"], "completed_with_shortage")
        self.assertEqual(state["reason_code"], "no_new_jobs")
        for text in ("新增 0 条", "读取 2 条", "重复 1 条", "过滤 1 条"):
            self.assertIn(text, state["message"])
        self.assertEqual(result["collected_job_ids"], [])

    def test_boss_summary_preserves_empty_search_reason(self):
        collector = _BlockedCollector("boss", [], "no_jobs_extracted")
        collector.collect = lambda *_: PlatformCollectionResult(
            "boss", "completed_with_shortage", "no_jobs_extracted", "BOSS 本轮未读取到岗位",
        )
        registry = CollectorRegistry({"boss": lambda: collector})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator(
                {}, db_path=Path(tmp) / "collection.db", registry=registry,
            ).run(_options())
        state = result["platforms"]["boss"]
        self.assertEqual(state["reason_code"], "no_jobs_extracted")
        self.assertIn("本轮未读取到岗位", state["message"])
        self.assertIn("新增 0 条", state["message"])


    def test_auto_score_is_opt_in_and_receives_only_this_run_ids(self):
        candidates = [_candidate("boss", "new-1")]
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", [], candidates)})
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "collection.db"
            with patch("bosshunter.ai.scorer.score_jobs") as score_jobs:
                result = CollectionOrchestrator({}, db_path=db_path, registry=registry).run(
                    _options(auto_score=True)
                )
        score_jobs.assert_called_once()
        kwargs = score_jobs.call_args.kwargs
        self.assertEqual(kwargs["scope"], "selected")
        self.assertEqual(kwargs["job_ids"], ["new-1"])
        self.assertFalse(kwargs["force_rescore"])
        self.assertEqual(result["collected_job_ids"], ["new-1"])

        with tempfile.TemporaryDirectory() as tmp:
            with patch("bosshunter.ai.scorer.score_jobs") as score_jobs:
                CollectionOrchestrator({}, db_path=Path(tmp) / "collection.db", registry=registry).run(
                    _options(auto_score=False)
                )
        score_jobs.assert_not_called()


    def test_legacy_search_values_override_empty_platform_defaults(self):
        result = normalize_collection_options({
            "search": {"keywords": ["后端"], "cities": ["上海"]},
            "platforms": {"boss": {"search": {"keywords": [], "cities": []}}},
            "profile": {"target_cities": ["北京"]},
        })
        self.assertEqual(result["platforms"]["boss"]["keywords"], ["后端"])
        self.assertEqual(result["platforms"]["boss"]["cities"], ["上海"])

    def test_boss_filters_survive_normalization_and_invalid_values_are_removed(self):
        result = normalize_collection_options({
            "search": {
                "keywords": ["后端"],
                "cities": ["北京"],
                "filters": {
                    "job_type": ["全职"],
                    "experience": ["1-3年", "任意经验"],
                    "industry": ["100001", "bad&sortType=2"],
                },
            },
        })

        self.assertEqual(result["platforms"]["boss"]["filters"], {
            "job_type": ["全职"],
            "experience": ["1-3年"],
            "industry": ["100001"],
        })


    def test_disabled_zhilian_is_not_in_implicit_default_queue(self):
        result = normalize_collection_options({
            "search": {"keywords": ["后端"], "cities": ["北京"]},
            "collection": {"default_order": ["boss", "zhilian"]},
            "platforms": {"zhilian": {"enabled": False, "search": {}}},
        })
        self.assertEqual(result["platform_order"], ["boss"])


class ValidateCollectionOptionsTests(TestCase):
    """validate_collection_options 错误路径覆盖。"""

    def test_rejects_empty_platform_order(self):
        with self.assertRaises(ValueError, msg="至少选择一个采集平台"):
            normalize_collection_options({}, {"platform_order": [], "platforms": {}})

    def test_rejects_duplicate_platforms(self):
        with self.assertRaises(ValueError, msg="不能重复"):
            normalize_collection_options({}, {
                "platform_order": ["boss", "boss"],
                "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"]}},
            })

    def test_rejects_unsupported_platform(self):
        with self.assertRaises(ValueError, msg="只支持"):
            normalize_collection_options({}, {
                "platform_order": ["unknown"],
                "platforms": {"unknown": {"keywords": ["AI"], "cities": ["北京"]}},
            })

    def test_rejects_missing_keywords(self):
        with self.assertRaises(ValueError, msg="关键词"):
            normalize_collection_options({}, {
                "platform_order": ["boss"],
                "platforms": {"boss": {"keywords": [], "cities": ["北京"]}},
            })

    def test_rejects_missing_cities(self):
        with self.assertRaises(ValueError, msg="城市"):
            normalize_collection_options({}, {
                "platform_order": ["zhilian"],
                "platforms": {"zhilian": {"keywords": ["AI"], "cities": []}},
            })

    def test_rejects_invalid_max_pages(self):
        with self.assertRaises(ValueError, msg="最大页数"):
            normalize_collection_options({}, {
                "platform_order": ["boss"],
                "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"], "max_pages": 0}},
            })

    def test_rejects_max_pages_over_10(self):
        with self.assertRaises(ValueError, msg="范围"):
            normalize_collection_options({}, {
                "platform_order": ["boss"],
                "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"], "max_pages": 11}},
            })

    def test_rejects_invalid_sort(self):
        with self.assertRaises(ValueError, msg="排序"):
            normalize_collection_options({}, {
                "platform_order": ["boss"],
                "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"], "sort": "invalid"}},
            })

    def test_rejects_non_bool_auto_score(self):
        with self.assertRaises(ValueError, msg="auto_score"):
            validate_collection_options({
                "platform_order": ["boss"],
                "auto_score": "yes",
                "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"]}},
            })


class CityCodeResolutionTests(TestCase):
    """51job / liepin 城市编码解析。"""


    def test_51job_unsupported_city_raises(self):
        with self.assertRaises(ValueError, msg="尚未支持"):
            normalize_collection_options({}, {
                "platform_order": ["51job"],
                "platforms": {"51job": {"keywords": ["AI"], "cities": ["未知城市"], "max_pages": 1}},
            })


    def test_liepin_unsupported_city_raises(self):
        with self.assertRaises(ValueError, msg="尚未支持"):
            normalize_collection_options({}, {
                "platform_order": ["liepin"],
                "platforms": {"liepin": {"keywords": ["AI"], "cities": ["未知城市"], "max_pages": 1}},
            })

    def test_zhilian_unsupported_city_raises(self):
        with self.assertRaises(ValueError, msg="暂未内置"):
            normalize_collection_options({}, {
                "platform_order": ["zhilian"],
                "platforms": {"zhilian": {"keywords": ["AI"], "cities": ["未知城市"], "max_pages": 1}},
            })


class NormalizeCollectionOptionsTests(TestCase):
    """normalize_collection_options 配置合并路径。"""


    def test_explicit_platform_order_filters_to_selected(self):
        result = normalize_collection_options({
            "search": {"keywords": ["后端"], "cities": ["北京"]},
        }, {
            "platform_order": ["boss", "zhilian"],
            "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"]}},
        })
        self.assertEqual(result["platform_order"], ["boss"])

    def test_platform_search_merges_with_config_defaults(self):
        result = normalize_collection_options({
            "platforms": {"boss": {"search": {"keywords": ["默认词"], "cities": ["北京"]}}},
        }, {
            "platform_order": ["boss"],
            "platforms": {"boss": {"keywords": ["覆盖词"]}},
        })
        self.assertEqual(result["platforms"]["boss"]["keywords"], ["覆盖词"])
        self.assertEqual(result["platforms"]["boss"]["cities"], ["北京"])

    def test_boss_defaults_to_target_cities_when_missing(self):
        result = normalize_collection_options({
            "profile": {"target_cities": ["上海", "深圳"]},
            "search": {"keywords": ["AI"]},
        })
        self.assertEqual(result["platforms"]["boss"]["cities"], ["上海", "深圳"])

    def test_non_bool_max_pages_raises(self):
        with self.assertRaises(ValueError, msg="整数"):
            normalize_collection_options({}, {
                "platform_order": ["boss"],
                "platforms": {"boss": {"keywords": ["AI"], "cities": ["北京"], "max_pages": "abc"}},
            })


class SharedProcessorTests(TestCase):
    """_SharedProcessor inspect/save 逻辑。"""

    def test_inspect_deduplicates_existing_job(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            db = get_db(db_path)
            insert_job(db, {**_candidate("boss", "dup").as_job_record()})
            db.close()
            registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", events, [_candidate("boss", "dup")])})
            result = CollectionOrchestrator({}, db_path=db_path, registry=registry).run(_options())
            self.assertEqual(result["platforms"]["boss"]["duplicate"], 1)
            self.assertEqual(result["platforms"]["boss"]["new"], 0)

    def test_inspect_filters_deal_breaker_in_title(self):
        events = []
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", events, [_candidate("boss", "x", "外包岗位")])})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator(
                {"profile": {"deal_breakers": ["外包"]}},
                db_path=Path(tmp) / "test.db", registry=registry,
            ).run(_options())
        self.assertEqual(result["platforms"]["boss"]["filtered"], 1)
        self.assertEqual(result["platforms"]["boss"]["new"], 0)

    def test_inspect_filters_blocked_company(self):
        events = []
        c = _candidate("boss", "x")
        c = JobCandidate(**{**c.__dict__, "company": "黑名单公司"})
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", events, [c])})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator(
                {"profile": {"blocked_companies": ["黑名单公司"]}},
                db_path=Path(tmp) / "test.db", registry=registry,
            ).run(_options())
        self.assertEqual(result["platforms"]["boss"]["filtered"], 1)

    def test_save_filters_jd_deal_breaker(self):
        events = []
        c = _candidate("boss", "x", "正常岗位")
        c = JobCandidate(**{**c.__dict__, "jd": "包含外包关键词的JD"})
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", events, [c])})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator(
                {"profile": {"jd_deal_breakers": ["外包"]}},
                db_path=Path(tmp) / "test.db", registry=registry,
            ).run(_options())
        self.assertEqual(result["platforms"]["boss"]["filtered"], 1)
        self.assertEqual(result["platforms"]["boss"]["new"], 0)


class OrchestrationFlowTests(TestCase):
    """CollectionOrchestrator.run 编排流程。"""

    def test_collection_error_becomes_blocked_result(self):
        from bosshunter.collection.base import CollectionError

        class _ErrorCollector:
            platform = "boss"
            def collect(self, _req, _hooks):
                raise CollectionError("test_error", "test error")

        registry = CollectorRegistry({"boss": lambda: _ErrorCollector()})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator({}, db_path=Path(tmp) / "test.db", registry=registry).run(_options())
        self.assertEqual(result["platforms"]["boss"]["status"], "blocked")
        self.assertEqual(result["platforms"]["boss"]["reason_code"], "test_error")

    def test_generic_exception_becomes_failed_result(self):
        class _CrashCollector:
            platform = "boss"
            def collect(self, _req, _hooks):
                raise RuntimeError("unexpected crash")

        registry = CollectorRegistry({"boss": lambda: _CrashCollector()})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator({}, db_path=Path(tmp) / "test.db", registry=registry).run(_options())
        self.assertEqual(result["platforms"]["boss"]["status"], "failed")
        self.assertEqual(result["platforms"]["boss"]["reason_code"], "network_error")

    def test_empty_result_reports_shortage(self):
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", [], [])})
        with tempfile.TemporaryDirectory() as tmp:
            result = CollectionOrchestrator({}, db_path=Path(tmp) / "test.db", registry=registry).run(_options())
        self.assertEqual(result["status"], "completed_with_shortage")
        self.assertEqual(result["platforms"]["boss"]["reason_code"], "no_new_jobs")
        self.assertEqual(result["platforms"]["boss"]["new"], 0)

    def test_progress_callback_is_invoked(self):
        progress_calls = []
        events = []
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", events, [_candidate("boss", "new-1")])})
        config = {"_workbench_collect_progress": lambda state: progress_calls.append(state)}
        with tempfile.TemporaryDirectory() as tmp:
            CollectionOrchestrator(config, db_path=Path(tmp) / "test.db", registry=registry).run(_options())
        self.assertGreater(len(progress_calls), 0)

    def test_auto_score_failure_does_not_crash(self):
        events = []
        registry = CollectorRegistry({"boss": lambda: _FakeCollector("boss", events, [_candidate("boss", "new-1")])})
        with tempfile.TemporaryDirectory() as tmp:
            with patch("bosshunter.ai.scorer.score_jobs", side_effect=RuntimeError("score crash")):
                result = CollectionOrchestrator({}, db_path=Path(tmp) / "test.db", registry=registry).run(
                    _options(auto_score=True)
                )
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["collected_job_ids"], ["new-1"])
