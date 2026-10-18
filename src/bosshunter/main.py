"""猪猪求职助手 CLI - 主入口"""

import os
from pathlib import Path

import click
from rich.console import Console

from bosshunter import __version__
from bosshunter.config import load_config, migrate_legacy_credentials

console = Console()
GITHUB_URL = "https://github.com/czspig/czspig-work-hunter"


def _hint_web():
    """Print a one-line hint about the web dashboard."""
    console.print("[dim]💡 运行 bosshunter web 可打开可视化看板[/dim]")


def _project_root() -> Path:
    """Return the installed project root used for config.yaml and runtime data."""
    return Path(__file__).resolve().parents[2]


def _runtime_base_dir(config_path: Path | None = None) -> Path:
    """Resolve where config.yaml and data/ should be read from."""
    if config_path is not None:
        return config_path.resolve().parent
    root = _project_root()
    if (root / "config.yaml").exists():
        return root
    if Path("config.yaml").exists():
        return Path.cwd()
    return Path.cwd()


def _hint_star_support(base_dir: Path | None = None):
    """Print a lightweight GitHub Star support hint once per local checkout."""
    marker = (base_dir or _runtime_base_dir()) / "data" / f".star_hint_{__version__}"
    if marker.exists():
        return
    console.print(f"[dim]⭐ 如果猪猪求职助手帮你节省了时间，欢迎 Star：{GITHUB_URL}[/dim]")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("shown\n", encoding="utf-8")
    except OSError:
        pass


def _is_first_run(config_path: Path | None = None) -> bool:
    """Check if this is the first run (no config.yaml exists)."""
    path = config_path or _runtime_base_dir() / "config.yaml"
    return not path.exists()


def _prompt_setup(port: int = 8686) -> None:
    """Show first-run setup prompt pointing to Web Dashboard."""
    console.print()
    console.print("[bold cyan]═══ 欢迎使用猪猪求职助手 ═══[/bold cyan]")
    console.print()
    console.print("[yellow]检测到尚未配置，建议先进入 Web 端完成初始设置：[/yellow]")
    console.print()
    console.print(f"  [bold]bosshunter web[/bold]  →  打开配置面板 (http://127.0.0.1:{port})")
    console.print()
    console.print("[dim]在面板中可以设置：[/dim]")
    console.print("[dim]  • 简历路径、期望薪资、一票否决词[/dim]")
    console.print("[dim]  • 搜索关键词、目标城市[/dim]")
    console.print("[dim]  • AI 评分阈值、发送频率限制[/dim]")
    console.print("[dim]  • 发送时间窗口、每日上限[/dim]")
    console.print()
    console.print("[dim]如需跳过，可手动创建 config.yaml（参考 config.example.yaml）[/dim]")
    console.print()


@click.group(name="bosshunter", invoke_without_command=True)
@click.version_option(version=__version__, prog_name="bosshunter")
@click.option("--config", "config_path", default=None, type=click.Path(exists=False), help="配置文件路径（默认 config.yaml）")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    """猪猪求职助手 - 本地运行、只读的 AI 岗位筛选助手"""
    ctx.ensure_object(dict)
    path = Path(config_path).resolve() if config_path else None
    base_dir = _runtime_base_dir(path)
    if path is None and (base_dir / "config.yaml").exists():
        path = base_dir / "config.yaml"
    ctx.obj["base_dir"] = base_dir
    os.chdir(base_dir)
    if path is not None:
        migrate_legacy_credentials(path)
    ctx.obj["config"] = load_config(path)
    from bosshunter.browser import configure
    configure(ctx.obj["config"])

    # First run: no subcommand and no config → prompt setup
    if ctx.invoked_subcommand is None:
        if _is_first_run(path):
            _prompt_setup()
        else:
            console.print("[bold cyan]猪猪求职助手[/bold cyan] 已就绪")
            console.print("[dim]运行 bosshunter --help 查看可用命令[/dim]")
            _hint_web()


@cli.command()
@click.pass_context
def connect(ctx: click.Context) -> None:
    """检测 BossHunter Browser Runtime 与 Chrome 连接"""
    from bosshunter.browser import configure
    from bosshunter.browser.diagnostics import print_browser_diagnostics

    config = ctx.obj["config"]
    configure(config)
    ok = print_browser_diagnostics(config, console)
    if not ok:
        raise SystemExit(1)

    console.print("\n[bold green]连接检测完成[/bold green]")


@cli.command(name="ai-status")
@click.pass_context
def ai_status(ctx: click.Context) -> None:
    """安全检测 AI 服务配置与连接状态（不显示 API Key）"""
    from bosshunter.web.preflight import check_ai_connection

    checks = check_ai_connection(ctx.obj["config"], required=True)
    has_error = False
    for check in checks:
        status = check.get("status")
        if status == "pass":
            marker = "[green]✓[/green]"
        elif status == "warning":
            marker = "[yellow]![/yellow]"
        else:
            marker = "[red]✗[/red]"
            has_error = True
        console.print(f"{marker} {check.get('message', 'AI 检测结果')}")
        console.print(f"  [dim]{check.get('detail', '')}[/dim]")
    if has_error:
        raise SystemExit(1)


@cli.command()
@click.option("--keyword", "-k", default=None, help="搜索关键词（覆盖配置文件）")
@click.option("--limit", "-l", default=None, type=int, help="最多抓取岗位数（默认不限制）")
@click.pass_context
def scrape(ctx: click.Context, keyword: str | None, limit: int | None) -> None:
    """采集岗位信息"""
    from bosshunter.scraper.jobs import scrape_jobs

    config = ctx.obj["config"]
    keywords = [keyword] if keyword else config["search"]["keywords"]

    console.print(f"[bold]开始采集岗位...[/bold] 关键词: {keywords}")
    count = scrape_jobs(config, keywords, limit)
    console.print(f"[green]✓[/green] 采集完成，共获取 {count} 个新岗位")
    _hint_web()
    _hint_star_support()


@cli.command()
@click.option("--rescore-filtered", is_flag=True, help="同时重新评分之前被 AI 判为低分的岗位")
@click.pass_context
def score(ctx: click.Context, rescore_filtered: bool) -> None:
    """对采集的岗位进行AI评分"""
    from bosshunter.ai.scorer import score_jobs

    config = ctx.obj["config"]
    console.print("[bold]开始AI评分...[/bold]")
    scored, filtered = score_jobs(config, rescore_filtered=rescore_filtered)
    console.print(f"[green]✓[/green] 评分完成: {scored} 个通过, {filtered} 个过滤")
    _hint_star_support()


@cli.command()
@click.option("--full", is_flag=True, help="完整仪表盘视图")
@click.pass_context
def status(ctx: click.Context, full: bool) -> None:
    """查看投递状态统计"""
    if full:
        from bosshunter.tracker.status import show_dashboard
        show_dashboard()
    else:
        from bosshunter.tracker.status import show_status
        show_status()


@cli.command()
@click.pass_context
def run(ctx: click.Context) -> None:
    """一键运行: 采集→AI评分（不会自动投递）"""
    from bosshunter.pipeline import run_pipeline

    config = ctx.obj["config"]
    console.print("[bold cyan]═══ 猪猪求职助手 启动 ═══[/bold cyan]\n")
    run_pipeline(config)
    _hint_web()
    _hint_star_support()


@cli.command()
@click.option("--job-id", default=None, help="指定岗位ID生成简历")
@click.pass_context
def resume(ctx: click.Context, job_id: str | None) -> None:
    """为指定岗位生成定制简历PDF"""
    from bosshunter.ai.resume import generate_tailored_resume

    config = ctx.obj["config"]
    if job_id:
        generate_tailored_resume(job_id, config)
    else:
        console.print("[yellow]请指定 --job-id[/yellow]")


@cli.command()
@click.option("--port", "-p", default=8686, help="服务端口（默认 8686）")
@click.option("--no-open", is_flag=True, help="不自动打开浏览器")
@click.pass_context
def web(ctx: click.Context, port: int, no_open: bool) -> None:
    """启动 Web Dashboard（本地看板 + 配置管理）"""
    from bosshunter.web.server import run_server, set_base_dir

    set_base_dir(ctx.obj["base_dir"])
    console.print("[bold cyan]═══ 猪猪求职助手 工作台 ═══[/bold cyan]")
    console.print(f"[dim]http://127.0.0.1:{port}[/dim]")
    _hint_star_support(ctx.obj["base_dir"])
    console.print()
    run_server(host="127.0.0.1", port=port, open_browser=not no_open)


if __name__ == "__main__":
    cli()
