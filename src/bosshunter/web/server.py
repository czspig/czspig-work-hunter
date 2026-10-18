"""BossHunter Web Server - Bottle HTTP service + API routes.

Serves:
- /api/* → JSON data endpoints
- /* → Frontend static files (dist/)
"""

from bosshunter.employment import classify_employment, internship_rejection

import json
from ipaddress import ip_address
import math
import mimetypes
import time
from copy import deepcopy
from pathlib import Path
from socketserver import ThreadingMixIn
from threading import Lock
from uuid import uuid4
from wsgiref.simple_server import WSGIServer

import yaml
from bottle import Bottle, HTTPResponse, request, response, static_file, abort

from bosshunter import __version__
from bosshunter.ai.credentials import AIRequestError, get_ai_api_key, list_ai_models
from bosshunter.ai.scorer import sanitize_score_trace
from bosshunter.cities import CityRefreshError, get_city_map, load_city_snapshot, refresh_city_cache
from bosshunter.config import (
	AI_SERVICE_PRESETS,
	load_config,
	remove_retired_collection_settings,
	save_config,
	validate_config_send_windows,
)
from bosshunter.db import (
	REJECT_ALLOWED_STATUSES,
	JobDeletionConflictError,
	JobManualSentConflictError,
	add_history,
	get_daily_activity,
	get_db,
	get_funnel_stats,
	get_jobs_pending_confirmation,
	get_recent_history,
	get_recent_monitor_replies,
	get_score_trace,
	get_unresolved_reply_pending,
	get_unresolved_resume_failures,
	get_stats,
	get_top_companies,
	mark_external_jobs_sent,
	permanent_delete_jobs,
	query_jobs,
	reject_jobs,
	restore_jobs,
	serialize_job,
	recompute_outsourcing,
	soft_delete_jobs,
	update_jobs_manual_status,
)
from bosshunter.collection.orchestrator import CollectionOrchestrator, normalize_collection_options
from bosshunter.collection_run_store import (
	boss_resume_options,
	get_collection_run,
	list_collection_runs,
	mark_orphaned_collection_runs_stopped,
)
from bosshunter.job_filters import city_base_name, city_match_clause, matching_blocked_company, parse_monthly_salary_k
from bosshunter.job_export import InvalidJobSelectionError, export_jobs, export_row_count
from bosshunter.scoring_run_store import (
	create_scoring_run,
	get_scoring_run,
	list_scoring_runs,
	mark_orphaned_scoring_runs_paused,
	update_scoring_run,
)
from bosshunter.scoring_selection import preview_scoring, select_scoring_jobs, validate_options
from bosshunter.web.preflight import check_ai_connection, collect_preflight_checks, error_messages
from bosshunter.web.resume_info import (
	build_resume_info_payload,
	is_default_resume_placeholder,
	load_resume_info,
	resolve_resume_filesystem_path,
)
from bosshunter.web.resume_names import resolve_active_resume_path, select_resume_markdown_filename
from bosshunter.web.resume_original import (
	remove_companion_pdf,
	resolve_configured_resume_files,
	upload_keeps_original_pdf,
	write_resume_artifacts,
)
from bosshunter.web.resume_upload import ResumeUploadError, prepare_resume_content
from bosshunter.web.city_lookup import CityLookupError, lookup_city
from bosshunter.web.tasks import (
	TaskAlreadyRunningError,
	WorkbenchTask,
	WorkbenchTaskRunner,
)

mimetypes.add_type("application/javascript", ".js", strict=True)
mimetypes.add_type("application/javascript", ".mjs", strict=True)
mimetypes.add_type("text/javascript", ".cjs", strict=True)
mimetypes.add_type("text/css", ".css", strict=True)

app = Bottle()
task_runner = WorkbenchTaskRunner()
job_mutation_lock = Lock()


def _is_loopback_address(value: str) -> bool:
	try:
		address = ip_address(value)
		return address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
	except ValueError:
		return False


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
	"""Handle Chrome preconnect sockets without blocking other requests."""

	daemon_threads = True
	allow_reuse_address = True

# Paths
FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"
SCHEMA_PATH = Path(__file__).parent / "config_schema.json"


def _default_base_dir() -> Path:
	"""Resolve the runtime project directory even when launched outside repo root."""
	source_root = Path(__file__).resolve().parents[3]
	if (source_root / "config.yaml").exists():
		return source_root

	cwd = Path.cwd()
	if (cwd / "config.yaml").exists():
		return cwd

	return cwd


BASE_DIR = _default_base_dir()
DATA_DIR = BASE_DIR / "data"
RESUME_DIR = DATA_DIR / "resumes"
CONFIG_PATH = BASE_DIR / "config.yaml"


def set_base_dir(base_dir: Path | str) -> None:
	"""Set the runtime directory used for config.yaml, data, and uploads."""
	global BASE_DIR, DATA_DIR, RESUME_DIR, CONFIG_PATH
	BASE_DIR = Path(base_dir).resolve()
	DATA_DIR = BASE_DIR / "data"
	RESUME_DIR = DATA_DIR / "resumes"
	CONFIG_PATH = BASE_DIR / "config.yaml"
	mark_orphaned_scoring_runs_paused(DATA_DIR / "bosshunter.db")
	mark_orphaned_collection_runs_stopped(DATA_DIR / "bosshunter.db")


def _get_web_db():
	"""Open the dashboard database from the resolved runtime data directory."""
	from bosshunter.outsourcing import load_rules

	db = get_db(DATA_DIR / "bosshunter.db")
	try:
		recompute_outsourcing(db, load_rules(load_config(CONFIG_PATH)))
		return db
	except Exception:
		db.close()
		raise


def _json_response(data, status_code=200):
	"""Return JSON response with proper headers."""
	response.content_type = "application/json; charset=utf-8"
	response.status = status_code
	return json.dumps(data, ensure_ascii=False, default=str)


def _serialize_history_items(items):
	"""Expose structured history details while retaining the legacy detail field."""
	serialized = []
	for item in items:
		record = dict(item)
		detail = record.get("detail")
		if isinstance(detail, str) and detail.lstrip().startswith("{"):
			try:
				payload = json.loads(detail)
			except (json.JSONDecodeError, TypeError):
				payload = None
			if isinstance(payload, dict):
				record["detail_payload"] = payload
		record["resolved"] = bool(record.get("resolved"))
		serialized.append(record)
	return serialized


def _serialize_job(item, *, config=None):
	"""Expose outsourcing, employment, and greeting state in one API representation."""
	record = serialize_job(dict(item))
	record["employment_type"] = classify_employment(record)
	record["employment_review"] = internship_rejection(record, config if config is not None else load_config(CONFIG_PATH))
	raw_issues = record.get("greeting_style_issues")
	if isinstance(raw_issues, str):
		try:
			parsed = json.loads(raw_issues)
		except (TypeError, ValueError):
			parsed = []
		record["greeting_style_issues"] = [str(issue) for issue in parsed] if isinstance(parsed, list) else []
	elif not isinstance(raw_issues, list):
		record["greeting_style_issues"] = []
	return record


def _mask_api_key(key):
	"""Return a display-safe API key marker."""
	if not key:
		return ""
	if len(key) > 8:
		return key[:4] + "***" + key[-4:]
	return "***"


def _redact_config_for_response(config):
	"""Hide secrets before returning config to the browser."""
	redacted = deepcopy(config)
	ai_cfg = redacted.get("ai")
	if isinstance(ai_cfg, dict):
		key = ai_cfg.pop("api_key", None)
		if key:
			ai_cfg["api_key_masked"] = _mask_api_key(str(key))
		auth_token = ai_cfg.pop("auth_token", None)
		if auth_token:
			ai_cfg["auth_token_masked"] = _mask_api_key(str(auth_token))
	return redacted


def _config_download_payload(config: dict) -> str:
	"""Serialize a shareable config backup without credentials."""
	redacted = _redact_config_for_response(config)
	ai_cfg = redacted.get("ai")
	if isinstance(ai_cfg, dict):
		ai_cfg.pop("api_key_masked", None)
		ai_cfg.pop("auth_token_masked", None)
	return yaml.dump(redacted, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _write_config(config: dict) -> None:
	"""Persist public settings separately from local AI credentials."""
	save_config(config, CONFIG_PATH)


def _sanitize_config_for_write(data):
	"""Remove browser-only fields and preserve existing secrets on blank posts."""
	cleaned = remove_retired_collection_settings(deepcopy(data))
	ai_cfg = cleaned.get("ai")
	if not isinstance(ai_cfg, dict):
		return cleaned

	ai_cfg.pop("api_key_masked", None)
	ai_cfg.pop("has_api_key", None)
	ai_cfg.pop("auth_token_masked", None)
	ai_cfg.pop("has_auth_token", None)

	existing_ai = load_config(CONFIG_PATH).get("ai", {})
	service = ai_cfg.get("service") or existing_ai.get("service")
	if service not in AI_SERVICE_PRESETS:
		provider = ai_cfg.get("provider") or existing_ai.get("provider") or "anthropic"
		service = "custom" if provider == "openai_compatible" else "anthropic"
	ai_cfg["service"] = service
	ai_cfg["provider"] = AI_SERVICE_PRESETS[service]["provider"]

	clear_credentials = bool(ai_cfg.pop("clear_credentials", False))

	for field in ("api_key", "auth_token"):
		if clear_credentials:
			posted_value = ai_cfg.get(field)
			if posted_value is None or str(posted_value).strip() == "":
				ai_cfg.pop(field, None)
			continue
		posted_value = ai_cfg.get(field)
		existing_value = existing_ai.get(field)
		existing_mask = _mask_api_key(str(existing_value)) if existing_value else ""
		should_preserve = (
			posted_value is None
			or str(posted_value).strip() == ""
			or (existing_mask and posted_value == existing_mask)
		)

		if should_preserve:
			if existing_value:
				ai_cfg[field] = existing_value
			else:
				ai_cfg.pop(field, None)

	return cleaned


def _preflight_messages(mode: str, config: dict, options: dict | None = None) -> list[str]:
	"""Return user-actionable blockers before starting a dashboard task."""
	messages: list[str] = []
	if mode not in {"full", "collect", "rescore"}:
		messages.append(f"不支持的任务模式：{mode}")
	if mode == "collect":
		try:
			collection_options = normalize_collection_options(config, options)
		except ValueError as exc:
			messages.append(str(exc))
			return messages
		if collection_options.get("auto_score"):
			resume_path = config.get("profile", {}).get("resume_path", "")
			if not resume_path or not Path(str(resume_path)).exists():
				messages.append("自动评分前请先在配置页上传 .md、.docx 或 .pdf 简历。")
			if not get_ai_api_key(config):
				messages.append("选择自动评分后，请先配置当前 AI 服务的 API Key。")
			return messages

	profile = config.get("profile", {})
	resume_path = profile.get("resume_path", "")
	if mode in {"full", "rescore"} and (not resume_path or not Path(str(resume_path)).exists()):
		messages.append("请先在配置页上传 .md、.docx 或 .pdf 简历。")

	if mode == "full":
		try:
			full_options = normalize_collection_options(config, options)
		except ValueError as exc:
			messages.append(str(exc))
		else:
			if not full_options.get("platform_order"):
				messages.append("运行全流程至少需要选择一个采集平台。")

	if mode in {"full", "rescore"} and not get_ai_api_key(config):
		messages.append("请先在配置页填写当前 AI 服务的 API Key，或设置对应的标准环境变量。")

	return messages


def _task_config(extra: dict | None = None) -> dict:
	config = load_config(CONFIG_PATH)
	if extra:
		config.update(extra)
	return config


def _log(task: WorkbenchTask, message: str) -> None:
	task.logs.append(message)


def _record_collect_progress(task: WorkbenchTask, state: dict) -> None:
	task.metrics.update({
		"collect_seen": int(state.get("seen") or 0),
		"collect_new": int(state.get("new") or 0),
		"collect_duplicate": int(state.get("duplicate") or 0),
		"collect_filtered": int(state.get("filtered") or 0),
		"collect_parse_failed": int(state.get("parse_failed") or 0),
		"collect_save_failed": int(state.get("save_failed") or 0),
		"collect_search_pages": int(state.get("search_pages") or 0),
	})
	if isinstance(state.get("progress"), dict):
		task.progress = deepcopy(state["progress"])


def _record_score_progress(task: WorkbenchTask, state: dict) -> None:
	task.metrics.update({
		"ai_completed": int(state.get("completed") or 0),
		"ai_total": int(state.get("total") or 0),
		"ai_passed": int(state.get("scored") or 0),
		"ai_filtered": int(state.get("filtered") or 0),
		"ai_failed": int(state.get("failed") or 0),
	})
	_log(
		task,
		f"AI 评分进度 {state['completed']}/{state['total']}：通过 {state['scored']}，过滤 {state['filtered']}，失败 {state['failed']}",
	)


def _execute_collect(task: WorkbenchTask, config: dict) -> None:
	_log(task, "开始采集岗位")
	collect_config = dict(config)
	collect_config["_workbench_stop_event"] = task.stop_requested
	collect_config["_workbench_collect_progress"] = lambda state: _record_collect_progress(task, state)
	collect_config["_workbench_log"] = lambda message: _log(task, message)
	collect_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
	if "_collection_options" not in config:
		# Preserve the old private executor seam used by legacy callers. New Web
		# collection tasks always inject normalized options before starting.
		from bosshunter.ai.scorer import score_jobs
		from bosshunter.scraper.jobs import scrape_jobs
		keywords = config.get("search", {}).get("keywords", [])
		collected_job_ids: list[str] = []
		scrape_jobs(collect_config, keywords, collected_job_ids=collected_job_ids)
		collect_report = collect_config.get("_workbench_collect_report", {})
		_stop_or_log_boss_collection_reason(task, str(collect_report.get("stop_reason") or ""))
		if task.stop_requested.is_set():
			return
		task.context["boss_collection_completed_monotonic"] = time.monotonic()
		_log(task, f"本轮采集完成：扫描 {task.metrics.get('collect_seen', 0)}，新增 {task.metrics.get('collect_new', 0)}，重复 {task.metrics.get('collect_duplicate', 0)}")
		_log(task, f"开始 AI 评分：处理全部未评分岗位（本轮新增 {len(collected_job_ids)} 个）")
		score_config = dict(config)
		score_config["_workbench_stop_event"] = task.stop_requested
		score_config["_workbench_log"] = lambda message: _log(task, message)
		score_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
		score_jobs(score_config)
		return

	result = CollectionOrchestrator(
		collect_config,
		db_path=DATA_DIR / "bosshunter.db",
		task_id=task.id,
	).run(config.get("_collection_options"))
	task.progress = {
		"run_id": result.get("run_id", ""),
		"outcome": result.get("status", "completed"),
		"platforms": result.get("platforms", {}),
		"collected_job_ids": result.get("collected_job_ids", []),
	}
	boss_state = result.get("platforms", {}).get("boss", {})
	if isinstance(boss_state, dict):
		_stop_or_log_boss_collection_reason(task, str(boss_state.get("reason_code") or ""))
		if boss_state.get("status") not in {None, "queued"}:
			task.context["boss_collection_completed_monotonic"] = time.monotonic()
	_log(task, f"本轮采集完成：新增 {len(result.get('collected_job_ids', []))}，状态 {result.get('status', 'completed')}")
	if task.stop_requested.is_set():
		return


def _stop_or_log_boss_collection_reason(task: WorkbenchTask, stop_reason: str) -> None:
	limit_labels = {
		"daily_search_page_limit": "BOSS 单日搜索页上限",
		"daily_detail_page_limit": "BOSS 单日详情页上限",
		"daily_platform_page_limit": "BOSS 单日页面访问总上限",
		"persistent_risk_lock": "BOSS 风险冷却锁",
	}
	risk_labels = {
		"captcha": "BOSS 验证码",
		"blocked": "BOSS 账号或请求拦截",
		"rate_limit": "BOSS 频率限制",
		"login_required": "BOSS 登录状态失效",
		"consecutive_page_failures": "BOSS 连续页面失败",
	}
	if stop_reason in limit_labels:
		_log(task, f"为了账户安全，已达到{limit_labels[stop_reason]}，仅停止 BOSS 访问；智联和 51job 不占用该额度")
	elif stop_reason in risk_labels:
		reason = f"为了账户安全，检测到{risk_labels[stop_reason]}，已立即停止并进入安全冷却"
		task.stop_reason = reason
		task.stop_requested.set()
		_log(task, reason)


def _execute_rescore(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.ai.scorer import score_jobs

	score_config = dict(config)
	score_config["_workbench_stop_event"] = task.stop_requested
	score_config["_workbench_log"] = lambda message: _log(task, message)
	score_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
	_log(task, "开始重新评分")
	score_jobs(score_config, rescore_filtered=True)


def _execute_score(task: WorkbenchTask, config: dict) -> None:
	from bosshunter.ai.scorer import score_jobs

	run_id = str(config.get("_score_run_id") or "")
	options = config.get("_score_options", {}) if isinstance(config.get("_score_options"), dict) else {}
	db_path = DATA_DIR / "bosshunter.db"

	def checkpoint(state: dict) -> None:
		remaining = [str(job_id) for job_id in state.get("remaining_job_ids", []) if str(job_id)]
		status = str(state.get("status") or "running")
		pause_reason = str(state.get("pause_reason") or "") if status == "paused" else None
		# AI 失败暂停时同步写入 error 列与 task.error，前端任务列表才能看到真实原因（issue #100）。
		error = str(state.get("error") or "") if status == "paused" else None
		update_scoring_run(
			db_path,
			run_id,
			status=status,
			remaining_job_ids=remaining,
			progress={**task.metrics, "remaining": len(remaining)},
			pause_reason=pause_reason,
			error=error or None,
		)
		if status == "paused":
			task.stop_reason = str(state.get("pause_reason") or "评分任务已暂停")
			if error:
				task.error = error
			task.stop_requested.set()

	score_config = dict(config)
	score_config["_workbench_stop_event"] = task.stop_requested
	score_config["_workbench_log"] = lambda message: _log(task, message)
	score_config["_workbench_score_progress"] = lambda state: _record_score_progress(task, state)
	score_config["_workbench_score_checkpoint"] = checkpoint
	_log(task, f"开始单独 AI 评分：{len(options.get('job_ids', []))} 个岗位")
	try:
		score_jobs(
			score_config,
			scope="selected",
			limit=None,
			job_ids=list(options.get("job_ids", [])),
			force_rescore=bool(options.get("force_rescore")),
		)
	except Exception as exc:
		update_scoring_run(db_path, run_id, status="failed", error=str(exc)[:1000])
		raise


def _execute_full(task: WorkbenchTask, config: dict) -> None:
	"""Collect + score. Applying is always done by the user on the platform."""
	full_config = dict(config)
	options = full_config.get("_collection_options")
	if not isinstance(options, dict):
		options = normalize_collection_options(full_config, None)
	full_config["_collection_options"] = {**options, "auto_score": True}
	_execute_collect(task, full_config)
	if not task.stop_requested.is_set():
		_log(task, "采集与评分完成，请在「今日待处理」中挑选岗位，到 BOSS 直聘自行投递")


task_runner._executors.update({
	"full": _execute_full,
	"collect": _execute_collect,
	"rescore": _execute_rescore,
	"score": _execute_score,
})


# ─── Health ───────────────────────────────────────────────

@app.route("/api/health")
def health():
	return _json_response({"status": "ok", "version": __version__})


# ─── Dashboard APIs ──────────────────────────────────────

@app.route("/api/funnel")
def api_funnel():
	db = _get_web_db()
	try:
		data = get_funnel_stats(db)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/stats")
def api_stats():
	db = _get_web_db()
	try:
		data = get_stats(db)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/activity")
def api_activity():
	days = int(request.params.get("days", 7))
	db = _get_web_db()
	try:
		data = get_daily_activity(db, days)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/jobs")
def api_jobs():
	try:
		deleted = request.params.get("deleted", "active").strip()
		limit = int(request.params.get("limit", 100))
		offset = int(request.params.get("offset", 0))
		if deleted not in {"active", "only", "all"} or not 1 <= limit <= 500 or offset < 0:
			raise ValueError("岗位查询参数无效")
	except (TypeError, ValueError) as exc:
		return _json_response({"error": str(exc)}, 400)

	db = _get_web_db()
	try:
		jobs, total = query_jobs(db, deleted=deleted, limit=limit, offset=offset)
		response.headers["X-Total-Count"] = str(total)
		return _json_response([_serialize_job(job) for job in jobs])
	finally:
		db.close()


def _optional_float_param(name: str, *, minimum: float = 0, maximum: float | None = None):
	raw_value = request.params.get(name)
	if raw_value in (None, ""):
		return None
	try:
		value = float(raw_value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{name} 必须是数字") from exc
	if not math.isfinite(value):
		raise ValueError(f"{name} 必须是有限数字")
	if value < minimum or (maximum is not None and value > maximum):
		raise ValueError(f"{name} 超出允许范围")
	return value


def _integer_param(name: str, default: int, *, minimum: int, maximum: int | None = None):
	raw_value = request.params.get(name, str(default))
	try:
		value = int(raw_value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{name} 必须是整数") from exc
	if value < minimum or (maximum is not None and value > maximum):
		raise ValueError(f"{name} 超出允许范围")
	return value


def _score_trace_missing_state(job: dict) -> str:
	"""Classify a missing trace without inferring an AI failure from incomplete evidence."""
	reason = str(job.get("score_reason") or "").strip()
	if reason.startswith("预筛不通过:"):
		return "prefilter_only"
	if reason.startswith(("AI评分失败:", "AI 评分失败:", "评分失败:")):
		return "failed"
	if reason or str(job.get("status") or "") in {
		"scored",
		"ready",
		"approved",
		"rejected",
		"sent",
		"replied",
		"resume_sent",
		"needs_resume",
		"follow_up_sent",
	}:
		return "legacy_missing"
	return "unavailable"


def _query_values(name: str) -> list[str]:
	"""Read repeated query values while keeping the old single-value form valid.

	``decode()`` is required: raw query values keep WSGI latin-1 bytes, so Chinese
	filter values (学历、城市) would never match the database without it.
	"""
	values = request.query.decode().getall(name)
	return [str(value).strip() for value in values if str(value).strip()]


@app.route("/api/jobs/search")
def api_job_search():
	try:
		minimum_score = _optional_float_param("min_score", maximum=100)
		salary_min = _optional_float_param("salary_min")
		salary_max = _optional_float_param("salary_max")
		limit = _integer_param("limit", 15, minimum=1, maximum=100)
		offset = _integer_param("offset", 0, minimum=0)
		if salary_min is not None and salary_max is not None and salary_min > salary_max:
			raise ValueError("最低薪资不能高于最高薪资")
		created_within = request.params.get("created_within", "").strip()
		if created_within and created_within not in {"today", "3d", "7d"}:
			raise ValueError("created_within 参数无效")
		recruitment_types = _query_values("recruitment_type")
		if any(value not in {"campus", "experienced", "unknown"} for value in recruitment_types):
			raise ValueError("recruitment_type 参数无效")
		education_filters = _query_values("education")
		if any(value not in {"博士", "硕士", "本科", "大专", "不限", "其他", "unknown"} for value in education_filters):
			raise ValueError("education 参数无效")
		sort_by = request.params.get("sort_by", "created_at").strip()
		if sort_by not in {"salary", "education", "score", "status", "hr_active", "created_at"}:
			raise ValueError("sort_by 参数无效")
		sort_order = request.params.get("sort_order", "desc").strip().lower()
		if sort_order not in {"asc", "desc"}:
			raise ValueError("sort_order 参数无效")
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)

	query = "SELECT * FROM jobs"
	conditions = ["deleted_at IS NULL"]
	params = []
	keyword = (request.query.getunicode("q") or "").strip()
	status_filters = _query_values("status")
	if keyword:
		conditions.append("(title LIKE ? OR company LIKE ? OR jd LIKE ? OR score_reason LIKE ?)")
		keyword_param = f"%{keyword}%"
		params.extend([keyword_param] * 4)
	if minimum_score is not None:
		conditions.append("score >= ?")
		params.append(minimum_score)
	if status_filters:
		placeholders = ",".join("?" for _ in status_filters)
		conditions.append(f"status IN ({placeholders})")
		params.extend(status_filters)
	source_platforms = _query_values("source_platform")
	if source_platforms:
		if any(value not in {"boss", "zhilian", "51job", "liepin"} for value in source_platforms):
			return _json_response({"error": "source_platform 参数无效"}, 400)
		placeholders = ",".join("?" for _ in source_platforms)
		conditions.append(f"COALESCE(source_platform, 'boss') IN ({placeholders})")
		params.extend(source_platforms)
	city_filters = _query_values("city")
	if city_filters:
		city_clause, city_params = city_match_clause(city_filters)
		conditions.append(city_clause)
		params.extend(city_params)
	if recruitment_types:
		placeholders = ",".join("?" for _ in recruitment_types)
		conditions.append(f"COALESCE(recruitment_type, 'unknown') IN ({placeholders})")
		params.extend(recruitment_types)
	if education_filters:
		education_conditions = []
		for value in education_filters:
			if value == "unknown":
				education_conditions.append("COALESCE(TRIM(education), '') = ''")
			else:
				education_conditions.append("education LIKE ?")
				params.append(f"%{value}%")
		conditions.append(f"({' OR '.join(education_conditions)})")
	if created_within == "today":
		conditions.append("created_at >= datetime('now', 'localtime', 'start of day', 'utc')")
	elif created_within == "3d":
		conditions.append("created_at >= datetime('now', '-3 days')")
	elif created_within == "7d":
		conditions.append("created_at >= datetime('now', '-7 days')")
	if conditions:
		query += " WHERE " + " AND ".join(conditions)
	sort_expressions = {
		"salary": "CAST(REPLACE(substr(COALESCE(salary, ''), 1, CASE WHEN instr(salary, 'K') > 0 THEN instr(salary, 'K') - 1 ELSE length(salary) END), ',', '') AS REAL)",
		"education": "CASE TRIM(COALESCE(education, '')) WHEN '博士' THEN 5 WHEN '硕士' THEN 4 WHEN '本科' THEN 3 WHEN '大专' THEN 2 WHEN '不限' THEN 1 ELSE 0 END",
		"score": "COALESCE(score, 0)",
		"status": "COALESCE(status, '')",
		"hr_active": "COALESCE(hr_active, '')",
		"created_at": "COALESCE(created_at, '')",
	}
	query += f" ORDER BY {sort_expressions[sort_by]} {sort_order.upper()}, created_at DESC, score DESC"

	config = load_config(CONFIG_PATH)
	blocked_companies = config.get("profile", {}).get("blocked_companies", [])
	db = _get_web_db()
	try:
		city_options = sorted({
			city_base_name(row[0]) for row in db.execute(
				"SELECT DISTINCT city FROM jobs"
				" WHERE deleted_at IS NULL AND TRIM(COALESCE(city, '')) <> ''"
			).fetchall()
		})
		if blocked_companies:
			all_total = sum(
				matching_blocked_company(row["company"], blocked_companies) is None
				for row in db.execute("SELECT company FROM jobs WHERE deleted_at IS NULL")
			)
		else:
			all_total = db.execute("SELECT COUNT(*) FROM jobs WHERE deleted_at IS NULL").fetchone()[0]
		rows = [dict(row) for row in db.execute(query, params).fetchall()]
		if blocked_companies:
			rows = [row for row in rows if matching_blocked_company(row.get("company", ""), blocked_companies) is None]
		if salary_min is not None or salary_max is not None:
			filtered_rows = []
			for row in rows:
				salary_range = parse_monthly_salary_k(row.get("salary", ""))
				if salary_range is None:
					continue
				job_min, job_max = salary_range
				if salary_min is not None and job_max < salary_min:
					continue
				if salary_max is not None and job_min > salary_max:
					continue
				filtered_rows.append(row)
			rows = filtered_rows
		total = len(rows)
		return _json_response({
			"items": [_serialize_job(row, config=config) for row in rows[offset:offset + limit]],
			"total": total,
			"all_total": all_total,
			"city_options": city_options,
			"limit": limit,
			"offset": offset,
		})
	finally:
		db.close()


@app.route("/api/top-companies")
def api_top_companies():
	limit = int(request.params.get("limit", 5))
	db = _get_web_db()
	try:
		data = get_top_companies(db, limit)
		return _json_response(data)
	finally:
		db.close()


@app.route("/api/history")
def api_history():
	limit = int(request.params.get("limit", 15))
	include_unresolved = request.params.get("include_unresolved", "").lower() in ("1", "true", "yes")
	include_monitor_conversations = request.params.get("include_monitor_conversations", "").lower() in ("1", "true", "yes")
	db = _get_web_db()
	try:
		data = get_recent_history(db, limit)
		if include_unresolved or include_monitor_conversations:
			monitor_replies = get_recent_monitor_replies(db) if include_monitor_conversations else []
			if include_monitor_conversations:
				retained_reply_ids = {item["id"] for item in monitor_replies}
				data = [
					item for item in data
					if item["action"] not in ("replied", "auto_replied", "resume_sent") or item["id"] in retained_reply_ids
				]
			seen_ids = {item["id"] for item in data}
			extra_groups = []
			if include_unresolved:
				extra_groups.extend((
					get_unresolved_reply_pending(db),
					get_unresolved_resume_failures(db),
				))
			if include_monitor_conversations:
				extra_groups.append(monitor_replies)
			for extra_items in extra_groups:
				data.extend(item for item in extra_items if item["id"] not in seen_ids)
				seen_ids.update(item["id"] for item in extra_items)
			data.sort(
				key=lambda item: (str(item.get("created_at") or ""), int(item.get("id") or 0)),
				reverse=True,
			)
		return _json_response(_serialize_history_items(data))
	finally:
		db.close()


@app.route("/api/workbench")
def api_workbench():
	db = _get_web_db()
	try:
		config = load_config(CONFIG_PATH)
		threshold = config.get("scoring", {}).get("threshold", 60)
		status = task_runner.status()
		return _json_response({
			"funnel": get_funnel_stats(db),
			"funnel_today": get_funnel_stats(db, today=True),
			"pending_confirmation": [
				_serialize_job(job) for job in get_jobs_pending_confirmation(db)
				if int(job.get("score") or 0) >= threshold
			],
			"task": status["active"],
			"last_task": status["last_task"],
		})
	finally:
		db.close()


@app.route("/api/workbench/preflight", method=["GET", "POST"])
def api_workbench_preflight():
	body = request.json if request.method == "POST" else {}
	body = body if isinstance(body, dict) else {}
	mode = str(body.get("mode") or request.params.get("mode", ""))
	options = body.get("options") if isinstance(body.get("options"), dict) else None
	try:
		config = load_config(CONFIG_PATH)
		options = _resolve_collection_resume(mode, options)
		checks = collect_preflight_checks(mode, config, options)
		messages = error_messages(checks)
		return _json_response({"ok": not messages, "messages": messages, "checks": checks})
	except ValueError as e:
		return _json_response({"ok": False, "messages": [str(e)]}, 400)
	except Exception as e:
		return _json_response({"ok": False, "messages": [str(e)]}, 500)


@app.route("/api/diagnostics/ai")
def api_ai_diagnostics():
	try:
		checks = check_ai_connection(load_config(CONFIG_PATH), required=True)
		messages = error_messages(checks)
		return _json_response({"ok": not messages, "messages": messages, "checks": checks})
	except Exception as e:
		return _json_response({"ok": False, "messages": [str(e)]}, 500)


def _scoring_options_from_body(body: dict) -> dict:
	raw_options = body.get("options", body)
	if not isinstance(raw_options, dict):
		raise ValueError("评分参数必须是对象")
	options = validate_options(
		raw_options.get("scope", "pending"),
		raw_options.get("limit"),
		raw_options.get("job_ids", []),
		raw_options.get("force_rescore", False),
	)
	# This cap applies to IDs supplied by the page, not internally selected jobs.
	if len(options["job_ids"]) > 1000:
		raise ValueError("一次最多选择 1000 个岗位")
	return options


@app.route("/api/scoring/preview", method="POST")
def api_scoring_preview():
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			raise ValueError("请求体必须是对象")
		options = _scoring_options_from_body(body)
		config = load_config(CONFIG_PATH)
		max_attempts = config.get("ai", {}).get("scoring_max_attempts", 2)
		db = _get_web_db()
		try:
			return _json_response(preview_scoring(db, **options, max_attempts_per_job=max_attempts))
		finally:
			db.close()
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)


@app.route("/api/scoring/start", method="POST")
def api_scoring_start():
	run_id = str(uuid4())
	db_path = DATA_DIR / "bosshunter.db"
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			raise ValueError("请求体必须是对象")
		options = _scoring_options_from_body(body)
		# force=true 允许结束已暂停的旧评分记录后强制开新任务；running 任务不在此列（issue #100）。
		# 只接受真正的布尔 true：bool("false") 也为真，异常请求会误触发强制重启（#117 review）。
		force = body.get("force", False) is True
		config = load_config(CONFIG_PATH)
		messages = _preflight_messages("rescore", config)
		if messages:
			return _json_response({"error": "请先处理评分启动检查", "messages": messages}, 400)
		active_runs = [run for run in list_scoring_runs(db_path, limit=100) if run.get("status") in {"running", "paused"}]
		running_runs = [run for run in active_runs if run.get("status") == "running"]
		paused_runs = [run for run in active_runs if run.get("status") == "paused"]
		if running_runs:
			return _json_response({"error": "已有独立评分任务正在运行，请等待其结束或先停止该任务"}, 409)
		if paused_runs:
			if not force:
				return _json_response({
					"error": "已有等待恢复的评分任务，请先继续或结束该任务；确认问题已修复后也可强制开始新任务",
					"code": "scoring_run_paused",
				}, 409)
			for run in paused_runs:
				update_scoring_run(
					db_path,
					str(run.get("id") or ""),
					status="stopped",
					error="已被新的评分任务强制结束",
				)
		db = _get_web_db()
		try:
			selected = select_scoring_jobs(db, **options)
		finally:
			db.close()
		job_ids = [str(job["id"]) for job in selected]
		if not job_ids:
			return _json_response({"error": "没有符合条件的待评分岗位"}, 400)
		stored_options = {
			"scope": options["scope"],
			"limit": options["limit"],
			"force_rescore": options["force_rescore"],
		}
		create_scoring_run(db_path, run_id=run_id, options=stored_options, job_ids=job_ids)
		runtime_options = {"job_ids": job_ids, "force_rescore": options["force_rescore"]}
		with job_mutation_lock:
			update_scoring_run(db_path, run_id, status="running")
			task = task_runner.start("score", _task_config({
				"_score_run_id": run_id,
				"_score_options": runtime_options,
			}))
		update_scoring_run(db_path, run_id, task_id=str(task["id"]))
		return _json_response({"run": get_scoring_run(db_path, run_id), "task": task})
	except TaskAlreadyRunningError as exc:
		update_scoring_run(db_path, run_id, status="stopped", error=str(exc))
		return _json_response({"error": str(exc)}, 409)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	except Exception as exc:
		update_scoring_run(db_path, run_id, status="failed", error=str(exc)[:1000])
		return _json_response({"error": "启动评分失败"}, 500)


@app.route("/api/scoring/runs")
def api_scoring_runs():
	return _json_response(list_scoring_runs(DATA_DIR / "bosshunter.db"))


@app.route("/api/scoring/runs/<run_id>/pause", method="POST")
def api_scoring_pause(run_id):
	db_path = DATA_DIR / "bosshunter.db"
	run = get_scoring_run(db_path, run_id)
	if not run:
		return _json_response({"error": "评分任务不存在"}, 404)
	if run.get("status") != "running":
		return _json_response(run)
	try:
		task = task_runner.stop(str(run.get("task_id") or ""), "用户暂停独立评分")
	except KeyError:
		task = {"status": "stopped"}
	latest = get_scoring_run(db_path, run_id) or run
	if task.get("status") not in {"completed", "failed"} and latest.get("remaining_job_ids"):
		latest = update_scoring_run(db_path, run_id, status="paused", pause_reason="用户暂停独立评分") or latest
	return _json_response(latest)


@app.route("/api/scoring/runs/<run_id>/resume", method="POST")
def api_scoring_resume(run_id):
	db_path = DATA_DIR / "bosshunter.db"
	run = get_scoring_run(db_path, run_id)
	if not run:
		return _json_response({"error": "评分任务不存在"}, 404)
	if run.get("status") != "paused":
		return _json_response({"error": "只有已暂停的评分任务可以恢复"}, 409)
	remaining = [str(job_id) for job_id in run.get("remaining_job_ids", []) if str(job_id)]
	if not remaining:
		return _json_response({"error": "该评分任务没有剩余岗位"}, 400)
	config = load_config(CONFIG_PATH)
	messages = _preflight_messages("rescore", config)
	if messages:
		return _json_response({"error": "请先处理评分启动检查", "messages": messages}, 400)
	force_rescore = bool(run.get("options", {}).get("force_rescore"))
	db = _get_web_db()
	try:
		eligible = select_scoring_jobs(
			db,
			scope="selected",
			limit=None,
			job_ids=remaining,
			force_rescore=force_rescore,
		)
	finally:
		db.close()
	remaining = [str(job["id"]) for job in eligible]
	if not remaining:
		completed = update_scoring_run(db_path, run_id, status="completed", remaining_job_ids=[])
		return _json_response(completed)
	try:
		with job_mutation_lock:
			update_scoring_run(db_path, run_id, status="running", remaining_job_ids=remaining)
			task = task_runner.start("score", _task_config({
				"_score_run_id": run_id,
				"_score_options": {"job_ids": remaining, "force_rescore": force_rescore},
			}))
		update_scoring_run(db_path, run_id, task_id=str(task["id"]))
		return _json_response({"run": get_scoring_run(db_path, run_id), "task": task})
	except TaskAlreadyRunningError as exc:
		update_scoring_run(db_path, run_id, status="paused", pause_reason=str(exc))
		return _json_response({"error": str(exc)}, 409)


@app.route("/api/scoring/runs/<run_id>/end", method="POST")
def api_scoring_end(run_id):
	db_path = DATA_DIR / "bosshunter.db"
	run = get_scoring_run(db_path, run_id)
	if not run:
		return _json_response({"error": "评分任务不存在"}, 404)
	if run.get("status") == "running" and run.get("task_id"):
		try:
			task_runner.stop(str(run["task_id"]), "用户结束独立评分")
		except KeyError:
			pass
	ended = update_scoring_run(db_path, run_id, status="stopped", remaining_job_ids=[])
	return _json_response(ended)


def _resolve_collection_resume(mode: str, options: dict | None) -> dict | None:
	if options and options.get("resume_run_id"):
		if mode != "collect" or not isinstance(options["resume_run_id"], str):
			raise ValueError("请在岗位采集中继续原 BOSS 任务")
		return boss_resume_options(DATA_DIR / "bosshunter.db", options["resume_run_id"])
	return options


@app.route("/api/workbench/task", method="POST")
def api_workbench_task_start():
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			return _json_response({"error": "请求体必须是对象"}, 400)
		mode = str(body.get("mode", ""))
		base_config = load_config(CONFIG_PATH)
		options = body.get("options") if isinstance(body.get("options"), dict) else None
		try:
			options = _resolve_collection_resume(mode, options)
		except ValueError as exc:
			return _json_response({"error": str(exc)}, 400)
		collection_options = None
		if mode == "collect":
			try:
				collection_options = normalize_collection_options(base_config, options)
			except ValueError as exc:
				return _json_response({"error": str(exc)}, 400)
		elif mode == "full":
			try:
				collection_options = normalize_collection_options(base_config, options)
			except ValueError as exc:
				return _json_response({"error": str(exc)}, 400)
			collection_options["auto_score"] = True
		messages = _preflight_messages(mode, base_config, collection_options)
		if messages:
			return _json_response({"error": "请先处理启动前检查", "messages": messages}, 400)
		extra = {"_collection_options": collection_options} if collection_options is not None else {}
		before_start = None
		if collection_options is not None and not collection_options.get("resume_run_id"):
			# Persist only non-secret collection preferences so the next dialog can
			# restore each platform's independent fields and queue order.
			base_config["collection"] = {
				**(base_config.get("collection") if isinstance(base_config.get("collection"), dict) else {}),
				"default_order": collection_options["platform_order"],
				"auto_score_default": collection_options["auto_score"],
			}
			platform_configs = deepcopy(base_config.get("platforms")) if isinstance(base_config.get("platforms"), dict) else {}
			selected_platforms = set(collection_options["platform_order"])
			for platform, value in collection_options["platforms"].items():
				platform_configs[platform] = {
					**(platform_configs.get(platform) if isinstance(platform_configs.get(platform), dict) else {}),
					"enabled": platform in selected_platforms,
					"search": value,
				}
			for platform in ("boss",):
				if platform not in selected_platforms and isinstance(platform_configs.get(platform), dict):
					platform_configs[platform]["enabled"] = False
			base_config["platforms"] = platform_configs
			before_start = lambda: _write_config(base_config)
		with job_mutation_lock:
			task = task_runner.start(mode, {**base_config, **extra}, before_start=before_start)
		return _json_response(task)
	except TaskAlreadyRunningError as e:
		return _json_response({"error": str(e)}, 409)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/collection/runs")
def api_collection_runs():
	try:
		limit = int(request.params.get("limit", 20))
		return _json_response(list_collection_runs(DATA_DIR / "bosshunter.db", limit=limit))
	except (TypeError, ValueError) as exc:
		return _json_response({"error": str(exc)}, 400)


@app.route("/api/collection/runs/<run_id>")
def api_collection_run_detail(run_id):
	run = get_collection_run(DATA_DIR / "bosshunter.db", run_id)
	if not run:
		return _json_response({"error": "采集运行记录不存在"}, 404)
	return _json_response(run)


@app.route("/api/workbench/task/<task_id>/stop", method="POST")
def api_workbench_task_stop(task_id):
	try:
		return _json_response(task_runner.stop(task_id))
	except KeyError:
		return _json_response({"error": "任务不存在"}, 404)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/workbench/reject", method="POST")
def api_workbench_reject():
	try:
		body = request.json or {}
		job_ids = [str(job_id) for job_id in body.get("job_ids", []) if str(job_id)]
		if not job_ids:
			return _json_response({"error": "请选择要放弃的岗位"}, 400)

		db = _get_web_db()
		try:
			placeholders = ",".join("?" for _ in job_ids)
			rows = db.execute(
				f"SELECT id, status FROM jobs WHERE deleted_at IS NULL AND id IN ({placeholders})",
				job_ids,
			).fetchall()
			expected_statuses = {str(row["id"]): str(row["status"] or "") for row in rows}
			invalid_ids = [
				job_id
				for job_id in job_ids
				if expected_statuses.get(job_id) not in REJECT_ALLOWED_STATUSES
			]
			if invalid_ids:
				return _json_response({
					"error": "所选岗位状态不允许放弃",
					"code": "reject_status_blocked",
					"invalid_ids": invalid_ids,
				}, 409)
			result = reject_jobs(db, job_ids, expected_statuses=expected_statuses)
			if result["invalid_ids"]:
				return _json_response({
					"error": "岗位状态已变化，放弃操作未执行",
					"code": "reject_status_blocked",
					"invalid_ids": result["invalid_ids"],
				}, 409)
		finally:
			db.close()

		return _json_response({"success": True, "count": result["affected_count"]})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/jobs/<job_id>")
def api_job_detail(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT * FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)).fetchone()
		if not row:
			return _json_response({"error": "岗位不存在"}, 404)
		return _json_response(_serialize_job(row))
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/score-trace")
def api_job_score_trace(job_id):
	"""Expose the latest validated score explanation without changing job-list payloads."""
	db = _get_web_db()
	try:
		job = db.execute(
			"SELECT id, status, score_reason FROM jobs WHERE id = ? AND deleted_at IS NULL",
			(job_id,),
		).fetchone()
		if not job:
			return _json_response({"error": "job_not_found"}, 404)
		found, stored_trace = get_score_trace(db, job_id)
		trace = sanitize_score_trace(stored_trace) if found else None
		if trace is not None:
			return _json_response({"job_id": job_id, "state": "available", "trace": trace})
		state = "unavailable" if found else _score_trace_missing_state(dict(job))
		return _json_response({"job_id": job_id, "state": state})
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/tailored-resume", method="POST")
def api_job_tailored_resume(job_id):
	from bosshunter.ai.resume import generate_tailored_resume, get_last_resume_failure_reason

	try:
		resume_path = generate_tailored_resume(job_id, _task_config())
	except Exception as exc:
		return _json_response({"error": f"定制简历生成失败：{exc}"}, 500)
	if not resume_path:
		return _json_response({"error": get_last_resume_failure_reason(job_id) or "定制简历生成失败"}, 400)
	return _json_response({"success": True, "resume_path": str(resume_path)})


@app.route("/api/jobs/<job_id>/resume/download")
def api_job_resume_download(job_id):
	db = _get_web_db()
	try:
		row = db.execute("SELECT resume_path FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)).fetchone()
		if not row or not row["resume_path"]:
			return _json_response({"error": "定制简历不存在"}, 404)
		resume_path = Path(row["resume_path"])
		if not resume_path.exists():
			return _json_response({"error": "定制简历文件不存在"}, 404)
		return static_file(resume_path.name, root=str(resume_path.parent), download=resume_path.name)
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/outreach-resume")
def api_job_outreach_resume(job_id):
	"""Return the editable source and review state without exposing local paths."""
	db = _get_web_db()
	try:
		row = db.execute(
			"SELECT * FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)
		).fetchone()
		if not row:
			return _json_response({"error": "岗位不存在"}, 404)
		job = dict(row)
		markdown_text = ""
		source_value = str(job.get("resume_source_path") or "")
		if source_value:
			source_path = Path(source_value)
			if source_path.exists():
				markdown_text = source_path.read_text(encoding="utf-8")
		return _json_response({
			"status": job.get("resume_review_status") or "missing",
			"source": job.get("resume_generation_source"),
			"failure_reason": job.get("resume_failure_reason"),
			"reviewed_at": job.get("resume_reviewed_at"),
			"markdown": markdown_text,
			"image_url": f"/api/jobs/{job_id}/outreach-resume/image" if job.get("resume_image_path") else None,
		})
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/outreach-resume", method="PUT")
def api_job_outreach_resume_save(job_id):
	body = request.json or {}
	markdown_text = str(body.get("markdown") or "")
	if len(markdown_text) > 30000:
		return _json_response({"error": "图片简历内容过长"}, 400)
	source = "codex" if body.get("source") == "codex" else "human_edit"
	try:
		from bosshunter.ai.resume import save_resume_draft

		result = save_resume_draft(job_id, markdown_text, _task_config(), source=source)
		db = _get_web_db()
		try:
			add_history(db, job_id, "outreach_resume_edited", f"已保存并重新渲染图片简历，来源：{source}")
		finally:
			db.close()
		return _json_response({"success": True, "status": "needs_review", "resume_path": result.name})
	except KeyError:
		return _json_response({"error": "岗位不存在"}, 404)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 409)
	except Exception as exc:
		return _json_response({"error": f"保存图片简历失败：{exc}"}, 500)


@app.route("/api/jobs/<job_id>/outreach-resume/review", method="POST")
def api_job_outreach_resume_review(job_id):
	body = request.json or {}
	if body.get("confirmed") is not True:
		return _json_response({"error": "确认图片简历需要 confirmed=true"}, 400)
	db = _get_web_db()
	try:
		row = db.execute(
			"SELECT resume_image_path FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)
		).fetchone()
		if not row:
			return _json_response({"error": "岗位不存在"}, 404)
		image_path = Path(str(row["resume_image_path"] or ""))
		if not row["resume_image_path"] or not image_path.exists():
			return _json_response({"error": "图片简历不存在，请先生成或保存草稿"}, 409)
		db.execute(
			"""
			UPDATE jobs
			SET resume_review_status = 'ready', resume_reviewed_at = CURRENT_TIMESTAMP,
				resume_failure_reason = NULL, updated_at = CURRENT_TIMESTAMP
			WHERE id = ? AND deleted_at IS NULL
			""",
			(job_id,),
		)
		add_history(db, job_id, "outreach_resume_reviewed", "用户已确认图片简历的事实、隐私和版式")
		return _json_response({"success": True, "status": "ready"})
	finally:
		db.close()


@app.route("/api/jobs/<job_id>/outreach-resume/image")
def api_job_outreach_resume_image(job_id):
	db = _get_web_db()
	try:
		row = db.execute(
			"SELECT resume_image_path FROM jobs WHERE id = ? AND deleted_at IS NULL", (job_id,)
		).fetchone()
		if not row or not row["resume_image_path"]:
			return _json_response({"error": "图片简历不存在"}, 404)
		image_path = Path(str(row["resume_image_path"]))
		if not image_path.exists():
			return _json_response({"error": "图片简历文件不存在"}, 404)
		download = request.params.get("download", "").lower() in {"1", "true", "yes"}
		return static_file(
			image_path.name,
			root=str(image_path.parent),
			download=image_path.name if download else False,
		)
	finally:
		db.close()


# ─── Config APIs ─────────────────────────────────────────

@app.route("/api/config")
def api_config_get():
	try:
		config = _redact_config_for_response(load_config(CONFIG_PATH))
		return _json_response(config)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config", method="POST")
def api_config_post():
	try:
		data = request.json
		if not data:
			return _json_response({"error": "Empty body"}, 400)
		if not isinstance(data, dict):
			return _json_response({"error": "Config body must be an object"}, 400)
		data = _sanitize_config_for_write(data)
		try:
			validate_config_send_windows(data)
		except ValueError as exc:
			return _json_response({"error": str(exc)}, 400)

		# Basic validation
		profile = data.get("profile", {})
		if profile.get("salary_min", 0) > profile.get("salary_max", 0) and profile.get("salary_max", 0) > 0:
			return _json_response({"error": "salary_min must be <= salary_max"}, 400)

		# Write YAML (backend exclusively owns YAML serialization)
		_write_config(data)

		return _json_response({"success": True, "message": "配置已保存"})
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config/models", method="POST")
def api_config_models():
	"""Use draft AI settings and saved credentials without persisting the draft."""
	try:
		data = request.json
		if not isinstance(data, dict) or not isinstance(data.get("ai"), dict):
			return _json_response({"error": "请提供 AI 配置"}, 400)
		ai = data["ai"]
		for field in ("service", "provider", "base_url", "api_key", "auth_token"):
			if field in ai and not isinstance(ai[field], str):
				return _json_response({"error": "AI 配置字段必须是文本"}, 400)
		config = _sanitize_config_for_write({"ai": ai})
		return _json_response({"models": list_ai_models(config)})
	except AIRequestError as exc:
		return _json_response({"error": exc.user_message}, 400)
	except Exception:
		return _json_response({"error": "获取模型列表失败，请检查 AI 配置后重试"}, 500)


@app.route("/api/config/schema")
def api_config_schema():
	try:
		with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
			schema = json.load(f)
		return _json_response(schema)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/config/download")
def api_config_download():
	if CONFIG_PATH.exists():
		response.content_type = "application/x-yaml; charset=utf-8"
		response.headers["Content-Disposition"] = "attachment; filename=config.yaml"
		return _config_download_payload(load_config(CONFIG_PATH))
	abort(404, "config.yaml not found")


# ─── Local Agent API ───────────────────────────────────────


@app.route("/api/config/cities")
def api_cities():
	return _json_response(get_city_map(BASE_DIR))


@app.route("/api/config/cities/lookup", method="POST")
def api_city_lookup():
	try:
		body = request.json or {}
		city = str(body.get("city") or "").strip()
		city_code = get_city_map(BASE_DIR).get(city)
		if city_code:
			return _json_response({"name": city, "code": city_code})
		return _json_response(lookup_city(city))
	except CityLookupError as exc:
		return _json_response({"error": str(exc)}, 400)


@app.route("/api/cities")
def api_city_snapshot():
	try:
		snapshot = load_city_snapshot(BASE_DIR)
		return _json_response({
			"ok": True,
			"source": snapshot.get("source", "bundled"),
			"count": len(snapshot.get("cities", [])),
			"updated_at": snapshot.get("fetched_at"),
			"cities": snapshot.get("cities", []),
		})
	except Exception:
		return _json_response({
			"ok": False,
			"source": "bundled",
			"count": 0,
			"cities": [],
			"error": "本地城市列表不可用",
		}, 500)


@app.route("/api/cities/refresh", method="POST")
def api_city_refresh():
	try:
		snapshot = refresh_city_cache(DATA_DIR / "cities.cache.json")
		return _json_response({
			"ok": True,
			"source": "cache",
			"count": len(snapshot.get("cities", [])),
			"updated_at": snapshot.get("fetched_at"),
			"cities": snapshot.get("cities", []),
		})
	except CityRefreshError as exc:
		return _json_response({
			"ok": False,
			"source": "local",
			"using_local_data": True,
			"error": str(exc),
		}, 502)


@app.route("/api/jobs/export", method="POST")
def api_jobs_export():
	db = _get_web_db()
	try:
		body = request.json or {}
		if not isinstance(body, dict):
			return _json_response({"error": "请求体必须是对象"}, 400)
		format_value = body.get("format", "xlsx")
		scope = body.get("scope", "all")
		job_ids = body.get("job_ids", [])
		filters = body.get("filters", {})
		if not isinstance(job_ids, list):
			return _json_response({"error": "岗位 ID 必须是数组"}, 400)
		if not isinstance(filters, dict):
			return _json_response({"error": "筛选条件必须是对象"}, 400)
		content, content_type, filename = export_jobs(
			db,
			format=format_value,
			scope=scope,
			job_ids=job_ids,
			filters=filters,
		)
		exported_count = export_row_count(db, scope=scope, job_ids=job_ids, filters=filters)
		response.content_type = content_type
		response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
		response.headers["Content-Length"] = str(len(content))
		response.headers["X-Exported-Count"] = str(exported_count)
		return content
	except InvalidJobSelectionError as exc:
		return _json_response({
			"error": str(exc),
			"code": "invalid_job_ids",
			"invalid_ids": exc.invalid_ids,
		}, 400)
	except ValueError as exc:
		return _json_response({"error": str(exc)}, 400)
	except Exception:
		return _json_response({"error": "岗位导出失败"}, 500)
	finally:
		db.close()


def _job_action_payload():
	body = request.json or {}
	if not isinstance(body, dict):
		raise ValueError("请求体必须是对象")
	job_ids = body.get("job_ids")
	if not isinstance(job_ids, list):
		raise ValueError("岗位 ID 必须是数组")
	return body, job_ids


def _job_action_error(exc: ValueError):
	payload = {"error": str(exc), "code": getattr(exc, "code", "invalid_request")}
	if isinstance(exc, (JobDeletionConflictError, JobManualSentConflictError)):
		payload["blocked"] = exc.blocked
		payload["not_found"] = exc.not_found
	return _json_response(payload, 409 if isinstance(exc, (JobDeletionConflictError, JobManualSentConflictError)) else 400)


def _active_task_mutation_error():
	active = task_runner.status().get("active")
	if not active:
		return None
	return _json_response({
		"error": f"当前后台任务「{active.get('label', '未知任务')}」仍在运行，请停止或等待结束后再修改岗位状态",
		"code": "active_task_conflict",
		"task_id": active.get("id"),
	}, 409)


@app.route("/api/jobs/soft-delete", method="POST")
def api_jobs_soft_delete():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = soft_delete_jobs(
				db,
				job_ids,
				confirmed=body.get("confirmed") is True,
				reason=str(body.get("reason") or "用户移入回收站"),
			)
		return _json_response(result)
	except (ValueError, JobDeletionConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


@app.route("/api/jobs/restore", method="POST")
def api_jobs_restore():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = restore_jobs(db, job_ids, confirmed=body.get("confirmed") is True)
		return _json_response(result)
	except (ValueError, JobDeletionConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


@app.route("/api/jobs/manual-sent", method="POST")
def api_jobs_manual_sent():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = mark_external_jobs_sent(
				db,
				job_ids,
				confirmed=body.get("confirmed") is True,
			)
		return _json_response(result)
	except (ValueError, JobManualSentConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


@app.route("/api/jobs/status", method="POST")
def api_jobs_status():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = update_jobs_manual_status(db, job_ids, str(body.get("status") or ""))
		return _json_response(result)
	except ValueError as exc:
		return _json_response({"error": str(exc), "code": "status_change_blocked"}, 409)
	finally:
		db.close()


@app.route("/api/jobs/permanent-delete", method="POST")
def api_jobs_permanent_delete():
	db = _get_web_db()
	try:
		body, job_ids = _job_action_payload()
		with job_mutation_lock:
			conflict = _active_task_mutation_error()
			if conflict is not None:
				return conflict
			result = permanent_delete_jobs(
				db,
				job_ids,
				confirmed=body.get("confirmed") is True,
				confirmation=body.get("confirmation", ""),
			)
		return _json_response(result)
	except (ValueError, JobDeletionConflictError) as exc:
		return _job_action_error(exc)
	finally:
		db.close()


# ─── Resume APIs ─────────────────────────────────────────

@app.route("/api/resume")
def api_resume_get():
	try:
		config = load_config(CONFIG_PATH)
		resume_path = config.get("profile", {}).get("resume_path", "")
		if not resume_path or not str(resume_path).strip():
			return _json_response(None)
		configured = resolve_resume_filesystem_path(resume_path, BASE_DIR)
		info = load_resume_info(configured)
		if info is None:
			# Default ./resume.md from config DEFAULTS is a placeholder, not an error.
			if is_default_resume_placeholder(resume_path):
				return _json_response(None)
			return _json_response({"error": "配置的简历文件不存在或无法读取"}, 404)
		canonical = str(info["path"])
		if Path(canonical).resolve() != configured.resolve():
			# Point AI/config at Markdown after PDF-only or legacy PDF paths.
			config.setdefault("profile", {})["resume_path"] = canonical
			_write_config(config)
		return _json_response(info)
	except ResumeUploadError as e:
		return _json_response({"error": str(e)}, 400)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/resume/original")
def api_resume_original():
	"""Serve the companion original PDF for in-panel preview."""
	try:
		config = load_config(CONFIG_PATH)
		resume_path = config.get("profile", {}).get("resume_path", "")
		# Match GET /api/resume: blank/whitespace means no resume configured.
		if not resume_path or not str(resume_path).strip():
			abort(404, "No resume configured")
		configured = resolve_resume_filesystem_path(resume_path, BASE_DIR)
		_, pdf_path = resolve_configured_resume_files(configured)
		if not pdf_path.is_file():
			abort(404, "Original PDF not found")
		return static_file(pdf_path.name, root=str(pdf_path.parent), mimetype="application/pdf")
	except HTTPResponse:
		raise
	except Exception as e:
		return _json_response({"error": str(e)}, 500)



@app.route("/api/resume/upload", method="POST")
def api_resume_upload():
	try:
		upload = request.files.get("file")
		if not upload:
			return _json_response({"error": "No file uploaded"}, 400)

		# Validate size (10MB max)
		content = upload.file.read()
		if len(content) > 10 * 1024 * 1024:
			return _json_response({"error": "文件大小超过 10MB 限制"}, 400)

		# Bottle's normalized `filename` strips non-ASCII characters. Use the
		# raw browser filename and apply our own Unicode-safe sanitization.
		raw_name = upload.raw_filename or upload.filename
		safe_name, stored_content = prepare_resume_content(raw_name, content)
		RESUME_DIR.mkdir(parents=True, exist_ok=True)
		config = load_config(CONFIG_PATH)
		active_path = resolve_active_resume_path(
			config.get("profile", {}).get("resume_path") or None,
			BASE_DIR,
		)
		final_name = select_resume_markdown_filename(
			RESUME_DIR,
			safe_name,
			stored_content,
			active_path,
		)
		dest = RESUME_DIR / final_name
		original_pdf_bytes = content if upload_keeps_original_pdf(raw_name) else None
		write_resume_artifacts(dest, stored_content, original_pdf_bytes=original_pdf_bytes)

		# Always store an absolute path so AI/preflight can Path(...).exists() directly.
		config.setdefault("profile", {})["resume_path"] = str(dest.resolve())
		_write_config(config)

		info = build_resume_info_payload(
			filename=final_name,
			size=len(stored_content),
			mtime=dest.stat().st_mtime,
			content=stored_content.decode("utf-8"),
			path=str(dest.resolve()),
			has_original_pdf=original_pdf_bytes is not None,
			original_pdf_path=str(dest.with_suffix(".pdf").resolve()) if original_pdf_bytes is not None else None,
		)
		return _json_response({"success": True, **info})
	except ResumeUploadError as e:
		return _json_response({"error": str(e)}, 400)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


@app.route("/api/resume", method="DELETE")
def api_resume_delete():
	try:
		config = load_config(CONFIG_PATH)

		# Detach from config always. Only drop the companion PDF when a Markdown
		# master already exists — never force PDF→MD conversion here, or a bad
		# PDF-only resume would block DELETE.
		resume_path = config.get("profile", {}).get("resume_path", "")
		if resume_path:
			configured = resolve_resume_filesystem_path(resume_path, BASE_DIR)
			markdown_path, pdf_path = resolve_configured_resume_files(configured)
			if markdown_path.is_file() and pdf_path.is_file():
				remove_companion_pdf(configured)

		config.setdefault("profile", {})["resume_path"] = ""
		_write_config(config)

		return _json_response({"success": True})
	except ResumeUploadError as e:
		return _json_response({"error": str(e)}, 400)
	except Exception as e:
		return _json_response({"error": str(e)}, 500)


# ─── Static Files + SPA Fallback ─────────────────────────

_STATIC_MIME_TYPES = {
	".css": "text/css; charset=utf-8",
	".cjs": "text/javascript; charset=utf-8",
	".html": "text/html; charset=utf-8",
	".js": "application/javascript; charset=utf-8",
	".json": "application/json; charset=utf-8",
	".mjs": "application/javascript; charset=utf-8",
	".svg": "image/svg+xml",
}


def _serve_static(filename: str, root: Path):
	"""Serve static assets with stable MIME types while retaining range/cache support.

	Reads the file bytes directly instead of relying on ``bottle.static_file``,
	which gates on ``os.access(..., os.R_OK)``. On macOS (sandboxed/TCC-restricted
	processes) that check can report a false 403 denial even though the file is
	actually readable, which makes the dashboard fail to load.
	"""
	file_path = (root / filename).resolve()
	resolved_root = root.resolve()
	if str(file_path).startswith(str(resolved_root)):
		try:
			data = file_path.read_bytes()
		except OSError:
			pass
		else:
			mimetype = _STATIC_MIME_TYPES.get(Path(filename).suffix.lower(), "auto")
			if mimetype == "auto":
				mimetype, _ = mimetypes.guess_type(filename)
			response.content_type = mimetype or "application/octet-stream"
			response.headers["Content-Length"] = str(len(data))
			return data

	return static_file(filename, root=str(root), mimetype="auto")


@app.route("/assets/<filepath:path>")
def serve_assets(filepath):
	return _serve_static(filepath, FRONTEND_DIR / "assets")


@app.route("/")
@app.route("/<filepath:path>")
def serve_spa(filepath="index.html"):
	if str(filepath).startswith("api/"):
		return _json_response({"error": "Not found"}, 404)

	# Try serving the exact file first
	file_path = FRONTEND_DIR / filepath
	if file_path.is_file():
		return _serve_static(filepath, FRONTEND_DIR)
	# SPA fallback: return index.html for all non-API routes
	return _serve_static("index.html", FRONTEND_DIR)


# ─── Error Handlers ──────────────────────────────────────

@app.error(404)
def error404(error):
	if request.path.startswith("/api/"):
		response.content_type = "application/json; charset=utf-8"
		return json.dumps({"error": "Not found"}, ensure_ascii=False)
	# SPA fallback for non-API 404s
	return _serve_static("index.html", FRONTEND_DIR)


@app.error(500)
def error500(error):
	response.content_type = "application/json; charset=utf-8"
	return json.dumps({"error": str(error.body)}, ensure_ascii=False)


# ─── Run ─────────────────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 8686, open_browser: bool = True):
	"""Start the web server."""
	if not (FRONTEND_DIR / "index.html").is_file():
		raise SystemExit(
			"前端资源未构建：请在 src/bosshunter/web/frontend 下执行 "
			"`npm ci && npm run build`（或安装官方发布的 wheel）后重试。"
		)

	if open_browser:
		import webbrowser
		import threading
		def _open():
			time.sleep(1)
			webbrowser.open(f"http://{host}:{port}")
		threading.Thread(target=_open, daemon=True).start()

	app.run(
		host=host,
		port=port,
		quiet=False,
		reloader=False,
		server_class=ThreadingWSGIServer,
	)
