import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class PublicPrivacyTests(unittest.TestCase):
    def test_tracked_files_do_not_reference_company_api_brand(self):
        import subprocess

        blocked = "one" + "api"
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        )

        offenders = []
        for rel_path in result.stdout.splitlines():
            path = ROOT / rel_path
            if not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if blocked in source.lower():
                offenders.append(rel_path)

        self.assertEqual(offenders, [])


class VersionMetadataTests(unittest.TestCase):
    def test_release_version_is_consistent(self):
        import json

        import bosshunter
        from bosshunter.web.server import health

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        sidebar_source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "components"
            / "layout"
            / "Sidebar.tsx"
        ).read_text(encoding="utf-8")

        version = "0.1.0"
        frontend = ROOT / "src" / "bosshunter" / "web" / "frontend"
        package = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertIn(f'version = "{version}"', pyproject)
        self.assertEqual(bosshunter.__version__, version)
        self.assertEqual(json.loads(health())["version"], version)
        self.assertEqual(package["version"], version)
        self.assertEqual(lock["version"], version)
        self.assertEqual(lock["packages"][""]["version"], version)
        self.assertIn(f"v{version} · 本地 AI 岗位筛选", sidebar_source)
        self.assertIn(f"猪猪求职助手 v{version}</h1>", readme)
        self.assertIn(f"## v{version}\n", changelog)
        self.assertNotIn("v1.1.0", sidebar_source)


class ConfigExampleTests(unittest.TestCase):
    def test_example_uses_search_cities_list(self):
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))

        self.assertIn("cities", config["search"])
        self.assertIsInstance(config["search"]["cities"], list)
        self.assertNotIn("city", config["search"])

    def test_example_defaults_to_not_allowing_internships(self):
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))

        self.assertIs(config["profile"]["allow_internship"], False)

    def test_example_includes_salary_filter_controls(self):
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))

        self.assertEqual(config["profile"]["salary_ceil_ratio"], 1.5)
        self.assertIs(config["profile"]["filter_unparsed_salary"], True)


    def test_example_does_not_include_prefilter_threshold(self):
        config = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))

        self.assertNotIn("prefilter_threshold", config["scoring"])


class ConfigValidationTests(unittest.TestCase):
    def test_load_config_rejects_unsupported_ai_provider(self):
        from bosshunter.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("ai:\n  provider: openai\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Anthropic 或 OpenAI 兼容接口"):
                load_config(config_path)

    def test_load_config_defaults_to_not_allowing_internships(self):
        from bosshunter.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("profile:\n  salary_min: 10\n", encoding="utf-8")

            config = load_config(config_path)

        self.assertIs(config["profile"]["allow_internship"], False)
        self.assertEqual(config["profile"]["salary_ceil_ratio"], 1.5)
        self.assertIs(config["profile"]["filter_unparsed_salary"], True)
        self.assertNotIn("prefilter_threshold", config["scoring"])

    def test_load_config_defaults_to_disabled_follow_up(self):
        from bosshunter.config import load_config

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("profile:\n  salary_min: 10\n", encoding="utf-8")

            config = load_config(config_path)

        self.assertIs(config["follow_up"]["enabled"], False)


class AiPromptRegressionTests(unittest.TestCase):
    def test_scorer_prompt_uses_universal_weighted_evidence(self):
        from bosshunter.ai.scorer import SCORING_PROMPT

        self.assertIn("核心职责匹配", SCORING_PROMPT)
        self.assertIn("40分", SCORING_PROMPT)
        self.assertIn("可迁移证据", SCORING_PROMPT)
        self.assertIn("不要自行输出总分", SCORING_PROMPT)
        self.assertNotIn("卫星遥感", SCORING_PROMPT)
        self.assertNotIn("小红书/抖音", SCORING_PROMPT)

    def test_tailored_resume_prompt_preserves_platform_growth_cases(self):
        from bosshunter.ai.resume import RESUME_TAILOR_PROMPT

        self.assertIn("平台案例和量化结果", RESUME_TAILOR_PROMPT)
        self.assertIn("阅读/观看", RESUME_TAILOR_PROMPT)
        self.assertIn("粉丝增长", RESUME_TAILOR_PROMPT)


class PrefilterHardGateTests(unittest.TestCase):
    def test_anonymous_company_jobs_are_filtered_before_ai_scoring(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "salary_min": 0}}
        anonymous_companies = [
            "某互联网公司",
            "某500强上市公司",
            "北京某大型计算机软件上市公司",
            "上海某大型电子商务公司",
            "北京某中型企业数字化与AI服务公司",
        ]

        for company in anonymous_companies:
            with self.subTest(company=company):
                score, reason = quick_score(
                    {
                        "company": company,
                        "title": "AI产品经理",
                        "jd": "",
                        "salary": "20-30K",
                    },
                    config,
                )

                self.assertEqual(score, 0)
                self.assertEqual(reason, "匿名公司岗位")

    def test_named_company_jobs_still_pass_anonymous_company_filter(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "salary_min": 0}}
        score, reason = quick_score(
            {
                "company": "荣耀终端技术有限公司",
                "title": "AI产品经理",
                "jd": "",
                "salary": "20-30K",
            },
            config,
        )

        self.assertEqual(score, 100)
        self.assertEqual(reason, "预筛通过")

    def test_deal_breakers_still_match_title_only(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": ["外包"], "salary_min": 0}}
        job = {"title": "AI产品经理", "jd": "非外包项目，团队稳定", "salary": "20-30K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 100)
        self.assertEqual(reason, "预筛通过")

    def test_deal_breaker_in_title_is_filtered(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": ["外包"], "salary_min": 0}}
        job = {"title": "AI产品经理 外包", "jd": "", "salary": "20-30K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 0)
        self.assertEqual(reason, "触发排除词: 外包")

    def test_jd_deal_breaker_filters_technical_requirements(self):
        from bosshunter.ai.prefilter import quick_score

        config = {
            "profile": {
                "deal_breakers": [],
                "jd_deal_breakers": ["SQL", "Linux"],
                "salary_min": 0,
            }
        }
        job = {
            "title": "业务实施顾问",
            "jd": "需要熟悉 Linux 环境并能编写 SQL 脚本",
            "salary": "10-15K",
        }

        score, reason = quick_score(job, config)

        self.assertEqual(score, 0)
        self.assertEqual(reason, "触发JD排除词: SQL")

    def test_default_rejects_internship_titles(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "salary_min": 0}}
        job = {"title": "AI产品实习生", "jd": "", "salary": "3-5K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 0)
        self.assertEqual(reason, "实习/管培岗位")

    def test_default_rejects_management_trainee_titles(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "salary_min": 0}}
        job = {"title": "产品管培生", "jd": "", "salary": "8-12K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 0)
        self.assertEqual(reason, "实习/管培岗位")

    def test_allow_internship_lets_internship_titles_pass_prefilter(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "allow_internship": True, "salary_min": 0}}
        job = {"title": "AI Product Intern", "jd": "", "salary": "3-5K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 100)
        self.assertEqual(reason, "预筛通过")

    def test_salary_below_minimum_is_filtered(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "salary_min": 100}}
        job = {"title": "AI产品经理", "jd": "", "salary": "12K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 0)
        self.assertEqual(reason, "薪资低于硬性要求: 12K < 100K")

    def test_salary_ceiling_uses_range_lower_bound(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": [], "salary_min": 7, "salary_max": 10, "salary_ceil_ratio": 1.0}}

        score, reason = quick_score({"title": "AI产品经理", "jd": "", "salary": "6-11K"}, config)

        self.assertEqual(score, 100, reason)
        score, reason = quick_score({"title": "AI产品经理", "jd": "", "salary": "15-20K"}, config)
        self.assertEqual(score, 0)
        self.assertIn("薪资远超期望上限", reason)

    def test_unparsed_salary_is_filtered_by_default_and_can_be_kept(self):
        from bosshunter.ai.prefilter import quick_score

        job = {"title": "AI产品经理", "jd": "", "salary": "面议"}
        score, reason = quick_score(job, {"profile": {"deal_breakers": [], "salary_min": 7}})
        self.assertEqual(score, 0)
        self.assertIn("无法解析", reason)

        score, reason = quick_score(
            job,
            {"profile": {"deal_breakers": [], "salary_min": 7, "filter_unparsed_salary": False}},
        )
        self.assertEqual(score, 100)
        self.assertIn("交由 AI 判断", reason)

    def test_passing_job_returns_hard_gate_pass(self):
        from bosshunter.ai.prefilter import quick_score

        config = {"profile": {"deal_breakers": ["外包", "996"], "salary_min": 15}}
        job = {"title": "AI产品经理", "jd": "", "salary": "20-30K"}

        score, reason = quick_score(job, config)

        self.assertEqual(score, 100)
        self.assertEqual(reason, "预筛通过")


class DashboardPageTests(unittest.TestCase):
    def setUp(self):
        self.source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "pages"
            / "DashboardPage.tsx"
        ).read_text(encoding="utf-8")


    def test_dashboard_exposes_manual_refresh_button(self):
        self.assertIn("RefreshCw", self.source)
        self.assertIn("onClick={refresh}", self.source)
        self.assertIn("refreshing ? '刷新中' : '刷新'", self.source)
        self.assertIn("最后刷新：", self.source)
        self.assertIn("refreshing && 'animate-spin'", self.source)

    # Greeting queue progress is covered by rendered component assertions in
    # DashboardPage.test.tsx; the task panel no longer uses collapsed details.

    def test_dashboard_falls_back_to_concrete_task_status(self):
        self.assertNotIn("return '等待后端返回阶段'", self.source)
        self.assertIn("`${task.label}正在启动`", self.source)
        self.assertIn("`${task.label}正在停止`", self.source)
        self.assertIn("`${task.label}已完成`", self.source)
        self.assertIn("`${task.label}已停止`", self.source)
        self.assertIn("`${task.label}运行失败`", self.source)
        self.assertIn("currentTaskStage(visibleTask)", self.source)

    def test_dashboard_can_stop_after_start_response_arrives(self):
        active_branch = self.source.index("if (activeTask?.mode === mode)")
        pending_guard = self.source.index("if (modePending) return")

        self.assertLess(active_branch, pending_guard)

    def test_dashboard_refresh_prevents_overlapping_requests(self):
        hook_source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "hooks"
            / "useDashboard.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("refreshingRef.current", hook_source)
        self.assertIn("setLastRefreshedAt", hook_source)
        self.assertIn("setWorkbench(prev => ({ ...prev, task, last_task: task }))", hook_source)
        self.assertIn("scope === 'monitor' ? 2000 : 5000", hook_source)
        self.assertIn("visibilitychange", hook_source)
        self.assertIn("window.addEventListener('focus'", hook_source)

    def test_dashboard_filters_today_jobs_and_clears_hidden_selection(self):
        self.assertIn("filteredTodayJobs", self.source)
        self.assertIn("setSelected(filteredTodayJobs.map(job => job.id))", self.source)
        self.assertIn("visibleJobIds.has(id)", self.source)
        self.assertIn("没有符合当前条件的岗位", self.source)

    def test_workbench_stats_distinguish_today_total_and_current_pending(self):
        self.assertIn("type StatsScope = 'today' | 'total'", self.source)
        self.assertIn("今日数据", self.source)
        self.assertIn("累计数据", self.source)
        self.assertIn("workbench.funnel_today", self.source)
        self.assertIn("workbench.pending_confirmation.length", self.source)
        self.assertIn("今日新增岗位", self.source)
        self.assertIn("当前待处理", self.source)
        self.assertIn("今日已投递", self.source)

    def test_workbench_task_status_shows_current_run_metrics(self):
        self.assertIn("本轮扫描", self.source)
        self.assertIn("本轮新增", self.source)
        self.assertIn("重复岗位", self.source)
        self.assertIn("AI通过", self.source)
        self.assertIn("AI过滤", self.source)
        self.assertIn("AI失败", self.source)

    def test_shared_job_filter_bar_exposes_confirmed_controls(self):
        filter_source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "components"
            / "jobs"
            / "JobFilterBar.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("搜索职位、公司、JD 或评分理由", filter_source)
        self.assertIn("最低评分", filter_source)
        self.assertIn("最低薪资", filter_source)
        self.assertIn("最高薪资", filter_source)
        self.assertIn("全部状态", filter_source)
        self.assertNotIn("招聘者活跃度", filter_source)
        self.assertIn("采集时间：全部", filter_source)
        self.assertIn("近 3 天", filter_source)
        self.assertIn("近 7 天", filter_source)
        self.assertIn("筛选结果", filter_source)
        self.assertIn("重置筛选", filter_source)
        self.assertIn("2xl:grid-cols-4", filter_source)
        self.assertNotIn("xl:grid-cols-8", filter_source)
        self.assertIn("flex-wrap", filter_source)
        self.assertIn("min-w-0", filter_source)

    def test_jobs_pool_uses_server_search_and_controlled_pagination(self):
        search_hook_source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "hooks"
            / "useJobSearch.ts"
        ).read_text(encoding="utf-8")
        jobs_table_source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "components"
            / "dashboard"
            / "JobsTable.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("/api/jobs/search?", search_hook_source)
        self.assertIn("250", search_hook_source)
        self.assertIn("created_within", search_hook_source)
        self.assertIn("onPageChange", jobs_table_source)
        self.assertIn("pageSize", jobs_table_source)
        self.assertIn("招聘者活跃", jobs_table_source)
        self.assertIn("活跃度未知", jobs_table_source)
        self.assertIn("dateStr.replace(' ', 'T')", jobs_table_source)
        self.assertIn("`${dateStr.replace(' ', 'T')}Z`", jobs_table_source)

    def test_dashboard_exposes_batch_reject_for_selected_pending_jobs(self):
        # Arrange: DashboardPage source is loaded in setUp.

        # Act / Assert
        self.assertIn("rejectSelectedJobs", self.source)
        self.assertIn("/api/workbench/reject", self.source)
        self.assertIn("放弃已选", self.source)
        self.assertIn("确定放弃这", self.source)
        self.assertIn("setSelected(prev => prev.filter", self.source)

    def test_each_pending_job_card_has_a_reject_action(self):
        self.assertIn("onReject={() => rejectSelectedJobs([job.id])}", self.source)
        self.assertIn("放弃岗位", self.source)


    def test_dashboard_shows_automatic_task_deadline_and_stop_reason(self):
        self.assertIn("截止 ", self.source)
        self.assertIn("visibleTask.deadline_at", self.source)
        self.assertIn("visibleTask.stop_reason", self.source)


class SidebarTests(unittest.TestCase):
    def setUp(self):
        # Arrange
        self.source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "components"
            / "layout"
            / "Sidebar.tsx"
        ).read_text(encoding="utf-8")


class HeaderTests(unittest.TestCase):
    def setUp(self):
        # Arrange
        self.source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "components"
            / "layout"
            / "Header.tsx"
        ).read_text(encoding="utf-8")

    def test_header_version_metadata_right_side_omits_duplicate_console_label(self):
        # Act / Assert
        self.assertNotIn("v2.1 · 本地控制台", self.source)
        self.assertIn("本地服务运行中", self.source)


class ConfigPageTests(unittest.TestCase):
    def setUp(self):
        # Arrange
        self.source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "pages"
            / "ConfigPage.tsx"
        ).read_text(encoding="utf-8")
        self.hook_source = (
            ROOT
            / "src"
            / "bosshunter"
            / "web"
            / "frontend"
            / "src"
            / "hooks"
            / "useConfig.ts"
        ).read_text(encoding="utf-8")

    def test_config_page_does_not_render_prefilter_threshold(self):
        # Act / Assert
        self.assertNotIn("prefilter_threshold", self.source)
        self.assertNotIn("预筛阈值", self.source)

    def test_allow_internship_switch_appears_below_deal_breakers(self):
        # Act
        deal_breakers_index = self.source.index("排除关键词")
        allow_internship_index = self.source.index("接受实习/管培岗位")

        # Assert
        self.assertGreater(allow_internship_index, deal_breakers_index)
        self.assertIn("profile.allow_internship", self.source)

    def test_config_page_exposes_jd_deal_breakers(self):
        self.assertIn("JD 排除关键词", self.source)
        self.assertIn("profile.jd_deal_breakers", self.source)
        self.assertIn("完整 JD 含这些词时会在 AI 评分前跳过", self.source)

    def test_config_page_exposes_salary_filter_controls(self):
        self.assertIn("薪资上限放宽倍数", self.source)
        self.assertIn("profile.salary_ceil_ratio", self.source)
        self.assertIn("过滤面议/无法解析薪资", self.source)
        self.assertIn("profile.filter_unparsed_salary", self.source)

    def test_config_page_api_failure_displays_error_instead_of_infinite_loading(self):
        # Act / Assert
        self.assertIn("error", self.hook_source)
        self.assertIn("!configRes.ok", self.hook_source)
        self.assertIn("!schemaRes.ok", self.hook_source)
        self.assertIn("配置加载失败", self.source)
        self.assertIn("请确认后端服务已启动", self.source)
        self.assertIn("error", self.source)


    def test_config_page_merges_boss_safety_and_throttle_settings(self):
        self.assertEqual(self.source.count('title="采集安全设置"'), 1)
        self.assertNotIn('title="BOSS 直聘采集安全"', self.source)
        self.assertNotIn('title="反检测设置"', self.source)
        self.assertIn('label="BOSS 操作间隔倍率"', self.source)
        self.assertNotIn('label="BOSS 采集间隔倍数"', self.source)


    def test_minimum_and_maximum_fields_share_compact_range_controls(self):
        for label in (
            "期望薪资范围（K）",
            "BOSS 风险暂停范围（分钟）",
        ):
            self.assertIn(f'label="{label}"', self.source)

        for retired_label in (
            "BOSS 风险暂停最少分钟",
            "BOSS 风险暂停最多分钟",
            "BOSS 采集后投递冷却最少（分钟）",
            "BOSS 采集后投递冷却最多（分钟）",
            "最短间隔 (秒)",
            "最长间隔 (秒)",
            "浏览最短时长 (秒)",
            "浏览最长时长 (秒)",
        ):
            self.assertNotIn(f'label="{retired_label}"', self.source)

    def test_boss_page_limit_estimate_is_directly_below_max_pages(self):
        max_pages_field = self.source.index('<Field label="最大页数">')
        estimate = self.source.index('理论最多 {bossTheoreticalPages} 页', max_pages_field)
        field_end = self.source.index('</Field>', max_pages_field)

        self.assertLess(estimate, field_end)


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        import json

        self.schema_source = (
            ROOT / "src" / "bosshunter" / "web" / "config_schema.json"
        ).read_text(encoding="utf-8")
        self.schema = json.loads(self.schema_source)

    def test_schema_does_not_include_prefilter_threshold(self):
        self.assertNotIn("prefilter_threshold", self.schema_source)

    def test_schema_adds_allow_internship_after_deal_breakers(self):
        profile = next(section for section in self.schema["sections"] if section["key"] == "profile")
        keys = [field["key"] for field in profile["fields"]]

        self.assertIn("allow_internship", keys)
        self.assertGreater(keys.index("allow_internship"), keys.index("deal_breakers"))

        allow_field = profile["fields"][keys.index("allow_internship")]
        self.assertEqual(allow_field["label"], "接受实习/管培岗位")
        self.assertEqual(allow_field["type"], "switch")
        self.assertIs(allow_field["default"], False)

    def test_schema_includes_salary_filter_controls(self):
        profile = next(section for section in self.schema["sections"] if section["key"] == "profile")
        fields = {field["key"]: field for field in profile["fields"]}

        self.assertEqual(fields["salary_ceil_ratio"]["label"], "薪资上限放宽倍数")
        self.assertEqual(fields["salary_ceil_ratio"]["default"], 1.5)
        self.assertEqual(fields["filter_unparsed_salary"]["type"], "switch")
        self.assertIs(fields["filter_unparsed_salary"]["default"], True)

    def test_schema_defaults_to_disabled_follow_up(self):
        follow_up = next(section for section in self.schema["sections"] if section["key"] == "follow_up")
        enabled = next(field for field in follow_up["fields"] if field["key"] == "enabled")

        self.assertEqual(enabled["label"], "启用自动跟进")
        self.assertEqual(enabled["type"], "switch")
        self.assertIs(enabled["default"], False)


class ScorerPrefilterTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "src" / "bosshunter" / "ai" / "scorer.py").read_text(encoding="utf-8")

    def test_scorer_no_longer_depends_on_prefilter_threshold(self):
        self.assertNotIn("prefilter_threshold", self.source)
        self.assertIn("if qs == 0:", self.source)

    def test_scorer_prompt_includes_salary_ceiling_context(self):
        self.assertIn("期望薪资区间", self.source)
        self.assertIn("薪资上限放宽线", self.source)
        self.assertIn("salary_ceil_ratio", self.source)


if __name__ == "__main__":
    unittest.main()
