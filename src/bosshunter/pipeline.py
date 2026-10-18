"""Pipeline - collect and score jobs. Applying is left to the user."""

from rich.console import Console

from bosshunter.browser import check_chrome_connection, configure, find_boss_tab

console = Console()


def run_pipeline(config: dict) -> None:
    """Run the read-only pipeline: scrape → score."""
    configure(config)

    console.print("[bold]Step 1/3: 检测 Browser Runtime[/bold]")
    if not check_chrome_connection():
        console.print("[red]✗ Browser Runtime 未连接，请先运行 bosshunter connect 查看诊断[/red]")
        return
    if not find_boss_tab():
        console.print("[red]✗ 未发现 BOSS直聘 页面，请先登录[/red]")
        return
    console.print("[green]  ✓ 浏览器就绪[/green]\n")

    console.print("[bold]Step 2/3: 采集岗位[/bold]")
    from bosshunter.scraper.jobs import scrape_jobs
    count = scrape_jobs(config, config["search"]["keywords"])
    console.print(f"[green]  ✓ 采集 {count} 个新岗位[/green]\n")

    console.print("[bold]Step 3/3: AI 评分筛选[/bold]")
    from bosshunter.ai.scorer import score_jobs
    scored, filtered = score_jobs(config)
    console.print(f"[green]  ✓ {scored} 个通过, {filtered} 个过滤[/green]")
    console.print("\n打开工作台挑选岗位，到 BOSS 直聘自行投递：[bold]bosshunter web[/bold]")
