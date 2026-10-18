import json
import unittest
from threading import Event, Lock, Thread, get_ident
from time import sleep
from unittest.mock import MagicMock, patch

import httpx

from bosshunter.ai import credentials, scorer


def _job(job_id: str) -> dict:
    return {
        "id": job_id,
        "title": f"AI 产品经理 {job_id}",
        "company": f"公司 {job_id}",
        "salary": "20-30K",
        "experience": "3-5年",
        "jd": "负责 AI 产品规划、用户研究和项目落地。" * 120,
        "score_reason": "产品经验匹配",
    }


def _score_response(score: int = 82) -> str:
    components = {
        82: (34, 21, 12, 7, 8),
        78: (32, 20, 11, 7, 8),
    }[score]
    return json.dumps({
        "role_summary": "客户交付",
        "core_duties": {"score": components[0], "evidence": "匹配"},
        "transferable_evidence": {"score": components[1], "evidence": "匹配"},
        "hard_requirements": {"score": components[2], "evidence": "匹配"},
        "tools_industry": {"score": components[3], "evidence": "匹配"},
        "practical_fit": {"score": components[4], "evidence": "匹配"},
        "caps": [],
        "hard_gaps": [],
        "reason": "匹配",
        "missing": "",
    }, ensure_ascii=False)


class AiCredentialErrorTests(unittest.TestCase):
    def test_openai_compatible_quota_error_is_not_silently_swallowed(self):
        request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"code": "insufficient_quota", "message": "quota exceeded"}},
        )
        config = {
            "ai": {
                "service": "deepseek",
                "provider": "openai_compatible",
                "model": "deepseek-chat",
                "api_key": "secret",
            }
        }

        with patch("bosshunter.ai.credentials.httpx.post", return_value=response):
            with self.assertRaises(credentials.AIRequestError) as raised:
                credentials.call_openai_compatible_text("prompt", config, 256)

        self.assertEqual(raised.exception.kind, "token_quota")
        self.assertEqual(str(raised.exception), "AI Token 额度或账户余额不足 (token_quota, status=429)")
        self.assertNotIn("secret", str(raised.exception))

    def test_context_limit_error_has_actionable_category(self):
        error = RuntimeError(
            "maximum context length exceeded: max_tokens plus input tokens is too large"
        )

        normalized = credentials.normalize_ai_error(error)

        self.assertEqual(normalized.kind, "context_limit")
        self.assertIn("上下文限制", normalized.user_message)

    def test_openai_truncated_response_is_reported_separately(self):
        request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
        response = httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"score": 80'},
                    }
                ]
            },
        )
        config = {
            "ai": {
                "service": "deepseek",
                "provider": "openai_compatible",
                "model": "deepseek-chat",
                "api_key": "secret",
            }
        }

        with patch("bosshunter.ai.credentials.httpx.post", return_value=response):
            with self.assertRaises(credentials.AIRequestError) as raised:
                credentials.call_openai_compatible_text("prompt", config, 256)

        self.assertEqual(raised.exception.kind, "output_truncated")

    def test_error_str_and_repr_carry_kind_and_status(self):
        error = credentials.AIRequestError("request_failed", "AI 服务请求失败", 404)

        self.assertEqual(str(error), "AI 服务请求失败 (request_failed, status=404)")
        self.assertEqual(
            str(credentials.AIRequestError("network", "AI 服务连接失败或超时")),
            "AI 服务连接失败或超时 (network)",
        )
        self.assertIn("kind='request_failed'", repr(error))
        self.assertIn("status_code=404", repr(error))

    def test_quota_marker_wins_over_context_marker(self):
        error = RuntimeError("余额不足，请减少输入过长内容")

        normalized = credentials.normalize_ai_error(error)

        self.assertEqual(normalized.kind, "token_quota")

    def test_rate_limit_status_wins_over_context_marker(self):
        request = httpx.Request("POST", "https://api.example.com/chat/completions")
        response = httpx.Response(
            429,
            request=request,
            json={"error": {"message": "请求过于频繁，输入过长"}},
        )
        error = httpx.HTTPStatusError("Too Many Requests", request=request, response=response)

        normalized = credentials.normalize_ai_error(error)

        self.assertEqual(normalized.kind, "rate_limit")

    def test_auth_status_wins_over_quota_and_rate_markers(self):
        request = httpx.Request("POST", "https://api.example.com/v1/messages")
        response = httpx.Response(
            401,
            request=request,
            json={"error": {"message": "quota exceeded for this token"}},
        )
        error = httpx.HTTPStatusError("Unauthorized", request=request, response=response)

        self.assertEqual(credentials.normalize_ai_error(error).kind, "auth")

        response = httpx.Response(
            403,
            request=request,
            json={"error": {"message": "rate limit policy rejected this key"}},
        )
        error = httpx.HTTPStatusError("Forbidden", request=request, response=response)

        self.assertEqual(credentials.normalize_ai_error(error).kind, "auth")


class ScorerTokenResilienceTests(unittest.TestCase):
    def test_structured_score_is_summed_and_hard_technical_gap_caps_at_55(self):
        response = """{
          "role_summary": "负责客户交付和上线支持",
          "core_duties": {"score": 36, "evidence": "有实施交付经验"},
          "transferable_evidence": {"score": 22, "evidence": "有培训和需求梳理经验"},
          "hard_requirements": {"score": 12, "evidence": "多数要求符合"},
          "tools_industry": {"score": 8, "evidence": "熟悉SaaS业务"},
          "practical_fit": {"score": 9, "evidence": "地点薪资符合"},
          "caps": ["technical_required"],
          "hard_gaps": ["必须熟练使用Linux并独立部署"],
          "reason": "交付经验匹配，但存在硬技术缺口",
          "missing": "Linux部署"
        }"""

        result = scorer._validated_score_result(response)

        self.assertIsNotNone(result)
        self.assertEqual(result.score, 55)
        self.assertEqual(result.raw_score, 87)
        self.assertIn("职责36/40", result.reason)
        self.assertIn("硬技术缺口封顶55", result.reason)

    def test_structured_score_rejects_component_above_its_limit(self):
        response = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 41, "evidence": "不合法"},
          "transferable_evidence": {"score": 20, "evidence": "匹配"},
          "hard_requirements": {"score": 12, "evidence": "匹配"},
          "tools_industry": {"score": 8, "evidence": "匹配"},
          "practical_fit": {"score": 8, "evidence": "匹配"},
          "caps": [], "hard_gaps": [], "reason": "匹配", "missing": ""
        }"""

        self.assertIsNone(scorer._validated_score_result(response))

    def test_legacy_total_score_json_is_rejected(self):
        self.assertIsNone(
            scorer._validated_score_result('{"score": 82, "reason": "匹配", "missing": ""}')
        )

    def test_field_alias_transferable_is_normalized_and_scored(self):
        response = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 30, "evidence": "较匹配"},
          "transferable": {"score": 19, "evidence": "可迁移"},
          "hard_requirements": {"score": 10, "evidence": "基本符合"},
          "tools_industry": {"score": 7, "evidence": "相关"},
          "practical_fit": {"score": 8, "evidence": "符合"},
          "caps": [], "hard_gaps": [], "reason": "整体较匹配", "missing": ""
        }"""

        result = scorer._validated_score_result(response)

        self.assertIsNotNone(result)
        self.assertEqual(result.components["transferable_evidence"], 19)
        self.assertEqual(result.score, 74)
        self.assertEqual(result.raw_score, 74)

    def test_canonical_field_name_wins_over_alias(self):
        response = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 30, "evidence": "较匹配"},
          "transferable": {"score": 5, "evidence": "别名值"},
          "transferable_evidence": {"score": 22, "evidence": "正式字段值"},
          "hard_requirements": {"score": 10, "evidence": "基本符合"},
          "tools_industry": {"score": 7, "evidence": "相关"},
          "practical_fit": {"score": 8, "evidence": "符合"},
          "caps": [], "hard_gaps": [], "reason": "整体较匹配", "missing": ""
        }"""

        result = scorer._validated_score_result(response)

        self.assertIsNotNone(result)
        self.assertEqual(result.components["transferable_evidence"], 22)

    def test_failure_reason_lists_missing_fields(self):
        response = '{"role_summary": "客户成功", "core_duties": {"score": 30, "evidence": "较匹配"}}'

        self.assertIsNone(scorer._validated_score_result(response))
        reason = scorer._score_validation_failure_reason(response)
        self.assertIn("缺少字段", reason)
        for key in ("transferable_evidence", "hard_requirements", "tools_industry", "practical_fit"):
            self.assertIn(key, reason)

    def test_failure_reason_reports_unparseable_json(self):
        self.assertIsNone(scorer._validated_score_result("模型输出的不是 JSON"))
        self.assertIn("JSON", scorer._score_validation_failure_reason("模型输出的不是 JSON"))

    def test_failure_reason_reports_invalid_component_values(self):
        response = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 41, "evidence": "超过上限"},
          "transferable_evidence": {"score": 20, "evidence": "匹配"},
          "hard_requirements": {"score": 12, "evidence": "匹配"},
          "tools_industry": {"score": 8, "evidence": "匹配"},
          "practical_fit": {"score": 8, "evidence": "匹配"},
          "caps": [], "hard_gaps": [], "reason": "匹配", "missing": ""
        }"""

        self.assertIsNone(scorer._validated_score_result(response))
        self.assertIn("字段值无效", scorer._score_validation_failure_reason(response))

    def test_failure_reason_reports_empty_response(self):
        self.assertIn("AI 未返回评分内容", scorer._score_validation_failure_reason(None))
        self.assertIn("AI 未返回评分内容", scorer._score_validation_failure_reason("   "))

    def test_alias_response_scores_successfully_end_to_end(self):
        db = MagicMock()
        job = _job("alias")
        response = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 30, "evidence": "较匹配"},
          "transferable": {"score": 19, "evidence": "可迁移"},
          "hard_requirements": {"score": 10, "evidence": "基本符合"},
          "tools_industry": {"score": 7, "evidence": "相关"},
          "practical_fit": {"score": 8, "evidence": "符合"},
          "caps": [], "hard_gaps": [], "reason": "整体较匹配", "missing": ""
        }"""

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch("bosshunter.ai.scorer._call_claude", return_value=response),
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {"ai": {"scoring_concurrency": 1}, "scoring": {"threshold": 71}}
            )

        self.assertEqual((scored, filtered), (1, 0))

    def test_request_score_reports_specific_validation_failure(self):
        job = _job("badjson")

        with (
            patch("bosshunter.ai.scorer._call_claude", return_value="模型输出的不是 JSON"),
            patch("bosshunter.ai.scorer._notify"),
        ):
            outcome = scorer._request_score(job, "真实简历", {}, 3)

        self.assertIn("JSON", outcome.failure_detail)
        self.assertIn("无法解析", outcome.failure_detail)

    def test_borderline_structured_score_is_reviewed_and_averaged(self):
        db = MagicMock()
        job = _job("review")
        first = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 30, "evidence": "较匹配"},
          "transferable_evidence": {"score": 19, "evidence": "可迁移"},
          "hard_requirements": {"score": 10, "evidence": "基本符合"},
          "tools_industry": {"score": 7, "evidence": "相关"},
          "practical_fit": {"score": 8, "evidence": "符合"},
          "caps": [], "hard_gaps": [], "reason": "整体较匹配", "missing": "行业经验"
        }"""
        review = """{
          "role_summary": "客户成功",
          "core_duties": {"score": 28, "evidence": "部分匹配"},
          "transferable_evidence": {"score": 17, "evidence": "可以迁移"},
          "hard_requirements": {"score": 9, "evidence": "多数符合"},
          "tools_industry": {"score": 6, "evidence": "一般"},
          "practical_fit": {"score": 8, "evidence": "符合"},
          "caps": [], "hard_gaps": [], "reason": "匹配但有差距", "missing": "行业经验"
        }"""

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch("bosshunter.ai.scorer._call_claude", side_effect=[first, review]) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.persist_job_score_and_trace") as persist_score,
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "ai": {"scoring_concurrency": 1, "scoring_second_review": True},
                    "scoring": {"threshold": 71},
                }
            )

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(persist_score.call_args.args[2], 71)
        self.assertIn("二次复核", persist_score.call_args.args[3])

    def test_ai_calls_run_with_configured_concurrency_but_db_writes_stay_on_main_thread(self):
        db = MagicMock()
        jobs = [_job(str(index)) for index in range(5)]
        lock = Lock()
        active = 0
        peak = 0
        main_thread = get_ident()
        write_threads: list[int] = []

        def call_ai(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            sleep(0.03)
            with lock:
                active -= 1
            return _score_response(82)

        def record_write(*_args, **_kwargs):
            write_threads.append(get_ident())

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=jobs),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch("bosshunter.ai.scorer._call_claude", side_effect=call_ai),
            patch("bosshunter.ai.scorer.update_job_quick_score", side_effect=record_write),
            patch("bosshunter.ai.scorer.update_job_score", side_effect=record_write),
            patch("bosshunter.ai.scorer.update_job_status", side_effect=record_write),
        ):
            scored, filtered = scorer.score_jobs(
                {"ai": {"scoring_concurrency": 3}, "scoring": {"threshold": 71}}
            )

        self.assertEqual((scored, filtered), (5, 0))
        self.assertEqual(peak, 3)
        self.assertTrue(write_threads)
        self.assertEqual(set(write_threads), {main_thread})

    def test_stop_returns_without_waiting_for_inflight_concurrent_ai_calls(self):
        db = MagicMock()
        stop_event = Event()
        ai_started = Event()
        release_ai = Event()
        finished = Event()

        def blocking_ai(*_args, **_kwargs):
            ai_started.set()
            release_ai.wait(2)
            return _score_response(82)

        def run_scoring():
            try:
                scorer.score_jobs(
                    {
                        "ai": {"scoring_concurrency": 3},
                        "scoring": {"threshold": 71},
                        "_workbench_stop_event": stop_event,
                    }
                )
            finally:
                finished.set()

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[_job("1"), _job("2")]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch("bosshunter.ai.scorer._call_claude", side_effect=blocking_ai),
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            thread = Thread(target=run_scoring)
            thread.start()
            self.assertTrue(ai_started.wait(0.5))
            stop_event.set()
            self.assertTrue(finished.wait(0.5))
            release_ai.set()
            thread.join(1)

        db.close.assert_called_once()

    def test_scoring_processes_all_unscored_pending_jobs(self):
        db = MagicMock()
        old_job = _job("old")
        new_job = _job("new")

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[old_job, new_job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                return_value=_score_response(82),
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score") as update_quick_score,
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {"scoring": {"threshold": 70}},
            )

        self.assertEqual((scored, filtered), (2, 0))
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(
            [item.args for item in update_quick_score.call_args_list],
            [(db, "old", 80), (db, "new", 80)],
        )

    def test_invalid_score_json_retries_and_reports_progress(self):
        db = MagicMock()
        job = _job("invalid-json")
        progress_updates: list[dict] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    "这不是完整 JSON",
                    _score_response(82),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "ai": {"scoring_max_attempts": 2},
                    "scoring": {"threshold": 70},
                    "_workbench_score_progress": progress_updates.append,
                }
            )

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(progress_updates[-1]["completed"], 1)
        self.assertEqual(progress_updates[-1]["scored"], 1)

    def test_context_limit_retry_preserves_full_resume_and_jd(self):
        db = MagicMock()
        job = _job("long")
        resume = "简历内容" * 1000

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value=resume),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("context_limit", "请求内容超过当前模型的上下文限制"),
                    _score_response(78),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, _ = scorer.score_jobs({
                "ai": {"scoring_second_review": False},
                "scoring": {"threshold": 70},
            })

        self.assertEqual(scored, 1)
        self.assertEqual(call_ai.call_count, 2)
        full_prompt = call_ai.call_args_list[0].args[0]
        retry_prompt = call_ai.call_args_list[1].args[0]
        self.assertEqual(retry_prompt, full_prompt)
        self.assertIn(resume, full_prompt)
        self.assertIn(job["jd"], full_prompt)
        self.assertEqual(call_ai.call_args_list[1].args[2], 1024)

    def test_context_limit_does_not_score_from_incomplete_evidence(self):
        with patch(
            "bosshunter.ai.scorer._call_claude",
            side_effect=credentials.AIRequestError("context_limit", "上下文过长"),
        ) as call_ai:
            outcome = scorer._request_score(_job("long"), "简历内容" * 1000, {}, 2)

        self.assertIsNone(outcome.result)
        self.assertIn("完整简历与JD仍超过", outcome.failure_detail)
        self.assertEqual(call_ai.call_count, 2)

    def test_context_retry_does_not_increase_a_smaller_output_budget(self):
        with patch(
            "bosshunter.ai.scorer._call_claude",
            side_effect=[
                credentials.AIRequestError("context_limit", "上下文过长"),
                _score_response(82),
            ],
        ) as call_ai:
            outcome = scorer._request_score(
                _job("long"), "完整简历", {"ai": {"scoring_max_tokens": 512}}, 2,
            )

        self.assertIsNotNone(outcome.result)
        self.assertEqual(call_ai.call_args_list[1].args[2], 512)

    def test_output_limit_retries_once_with_lower_single_request_limit(self):
        db = MagicMock()
        job = _job("output")
        logs: list[str] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("output_limit", "当前模型不接受设置的输出 Token 上限"),
                    _score_response(82),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_log": logs.append,
                }
            )

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_count, 2)
        self.assertEqual(call_ai.call_args_list[1].args[2], 128)
        self.assertTrue(any("降低输出 Token 上限后重试评分" in message for message in logs))

    def test_truncated_score_retries_with_larger_output_limit(self):
        db = MagicMock()
        job = _job("truncated")
        logs: list[str] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("output_truncated", "AI 返回内容因输出 Token 上限被截断"),
                    _score_response(82),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_log": logs.append,
                }
            )

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_args_list[1].args[2], 16384)
        self.assertTrue(any("回答被截断" in message and "增大输出 Token" in message for message in logs))

    def test_quota_error_pauses_without_changing_pending_jobs(self):
        db = MagicMock()
        jobs = [_job("1"), _job("2")]
        logs: list[str] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=jobs),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=credentials.AIRequestError("token_quota", "AI Token 额度或账户余额不足"),
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status") as update_status,
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_log": logs.append,
                }
            )

        self.assertEqual((scored, filtered), (0, 0))
        self.assertEqual(call_ai.call_count, 1)
        self.assertTrue(any("安全暂停" in message and "下次运行会继续处理" in message for message in logs))

    def test_pause_reason_carries_error_kind_and_status_code(self):
        db = MagicMock()
        job = _job("1")
        checkpoints: list[dict] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=credentials.AIRequestError("request_failed", "AI 服务请求失败", 404),
            ),
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_score_checkpoint": checkpoints.append,
                }
            )

        self.assertEqual((scored, filtered), (0, 0))
        self.assertEqual(checkpoints[-1]["status"], "paused")
        self.assertEqual(
            checkpoints[-1]["pause_reason"],
            "AI 服务请求失败 (request_failed, status=404)",
        )

    def test_truncation_retry_pause_keeps_retry_context(self):
        db = MagicMock()
        job = _job("1")
        checkpoints: list[dict] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("output_truncated", "AI 返回内容因输出 Token 上限被截断"),
                    credentials.AIRequestError("auth", "AI API Key 无效或当前模型没有访问权限", 401),
                ],
            ),
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status"),
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_score_checkpoint": checkpoints.append,
                }
            )

        self.assertEqual((scored, filtered), (0, 0))
        self.assertEqual(checkpoints[-1]["status"], "paused")
        pause_reason = checkpoints[-1]["pause_reason"]
        self.assertIn("增大输出 Token 重试后失败", pause_reason)
        self.assertIn("(auth, status=401)", pause_reason)

    def test_empty_response_retries_then_scores_job(self):
        db = MagicMock()
        job = _job("empty-retry")

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("empty_response", "AI 服务没有返回文本内容，可能只返回了思考过程"),
                    _score_response(82),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status") as update_status,
        ):
            scored, filtered = scorer.score_jobs({"scoring": {"threshold": 70}})

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_count, 2)

    def test_persistent_empty_response_fails_single_job_and_continues_batch(self):
        db = MagicMock()
        jobs = [_job("1"), _job("2")]
        logs: list[str] = []
        checkpoints: list[dict] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=jobs),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("empty_response", "AI 服务没有返回文本内容，可能只返回了思考过程"),
                    credentials.AIRequestError("empty_response", "AI 服务没有返回文本内容，可能只返回了思考过程"),
                    _score_response(82),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score") as update_score,
            patch("bosshunter.ai.scorer.update_job_status") as update_status,
            patch("bosshunter.ai.scorer.persist_job_score_and_trace") as persist_score,
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_log": logs.append,
                    "_workbench_score_checkpoint": checkpoints.append,
                }
            )

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_count, 3)
        self.assertEqual(update_score.call_count, 1)
        self.assertEqual(
            update_score.call_args_list[0].args,
            (db, "1", 0, "AI评分失败: AI 未返回评分内容"),
        )
        self.assertEqual(persist_score.call_count, 1)
        self.assertEqual(persist_score.call_args.args[1], "2")
        self.assertEqual(persist_score.call_args.args[2], 82)
        self.assertTrue(any("已跳过 公司 1｜AI 产品经理 1" in message for message in logs))
        self.assertEqual(checkpoints[-1]["status"], "completed_with_errors")

    def test_deleted_job_mid_batch_is_skipped_and_batch_continues(self):
        db = MagicMock()
        jobs = [_job("1"), _job("2")]
        checkpoints: list[dict] = []

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=jobs),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[_score_response(82), _score_response(78)],
            ),
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status") as update_status,
            patch(
                "bosshunter.ai.scorer.persist_job_score_and_trace",
                side_effect=[ValueError("岗位不存在或已进入回收站，未保存评分追踪"), None],
            ) as persist_score,
        ):
            scored, filtered = scorer.score_jobs(
                {
                    "scoring": {"threshold": 70},
                    "_workbench_log": lambda message: None,
                    "_workbench_score_checkpoint": checkpoints.append,
                }
            )

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(persist_score.call_count, 2)
        self.assertEqual(checkpoints[-1]["status"], "completed")

    def test_truncation_retry_empty_response_stays_job_level(self):
        db = MagicMock()
        job = _job("1")

        with (
            patch("bosshunter.ai.scorer.get_db", return_value=db),
            patch("bosshunter.ai.scorer._load_resume", return_value="真实简历"),
            patch("bosshunter.ai.scorer.get_jobs_by_status", return_value=[job]),
            patch("bosshunter.ai.scorer.quick_score", return_value=(80, "通过")),
            patch(
                "bosshunter.ai.scorer._call_claude",
                side_effect=[
                    credentials.AIRequestError("output_truncated", "AI 返回内容因输出 Token 上限被截断"),
                    credentials.AIRequestError("empty_response", "AI 服务没有返回文本内容，可能只返回了思考过程"),
                    _score_response(82),
                ],
            ) as call_ai,
            patch("bosshunter.ai.scorer.update_job_quick_score"),
            patch("bosshunter.ai.scorer.update_job_score"),
            patch("bosshunter.ai.scorer.update_job_status") as update_status,
        ):
            scored, filtered = scorer.score_jobs({"scoring": {"threshold": 70}})

        self.assertEqual((scored, filtered), (1, 0))
        self.assertEqual(call_ai.call_count, 3)


if __name__ == "__main__":
    unittest.main()
