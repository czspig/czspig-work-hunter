import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import DashboardPage from './DashboardPage'
import type { WorkbenchTask } from '@/hooks/useDashboard'

let workbenchPayload: Record<string, unknown> = {}
let stopResponse: () => Response = () => jsonResponse({})
let preflightResponse: () => Response = () => jsonResponse({})

function jsonResponse(body: unknown, ok = true) {
  return new Response(JSON.stringify(body), {
    status: ok ? 200 : 500,
    headers: { 'Content-Type': 'application/json' },
  })
}

function buildTask(overrides: Partial<WorkbenchTask>): WorkbenchTask {
  return {
    id: 'task-1',
    mode: 'collect',
    label: '单独采集',
    status: 'running',
    logs: [],
    stop_requested: false,
    ...overrides,
  }
}

function baseWorkbench(overrides: Record<string, unknown> = {}) {
  return {
    funnel: {},
    funnel_today: {},
    pending_confirmation: [],
    task: null,
    last_task: null,
    ...overrides,
  }
}

const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input)
  if (url === '/api/workbench' && (!init?.method || init.method === 'GET')) {
    return jsonResponse(workbenchPayload)
  }
  if (url.includes('/stop') && init?.method === 'POST') {
    return stopResponse()
  }
  if (url.startsWith('/api/workbench/preflight')) {
    return preflightResponse()
  }
  return jsonResponse({})
})

describe('DashboardPage workbench task panel', () => {
  it('groups all collection counters with separate parsing and saving failure records', async () => {
    workbenchPayload = baseWorkbench({ task: buildTask({ mode: 'full',
      metrics: { collect_seen: 8, collect_new: 2, collect_parse_failed: 1, collect_save_failed: 1 },
      progress: { outcome: 'running', platforms: { boss: {
        status: 'running', new: 2, target: null, seen: 8, duplicate: 2, filtered: 2, parse_failed: 1, save_failed: 1,
        message: '保存岗位失败：RuntimeError', error_records: [
          { kind: 'parse', title: '', company: '', city: '深圳', keyword: 'AI', page: 2, reason: '列表缺少有效链接' },
          { kind: 'save', title: '测试岗位', company: '测试公司', city: '深圳', keyword: 'AI', page: 2, reason: '保存岗位失败：RuntimeError' },
        ],
      } } },
    }) })
    render(<DashboardPage view="workbench" />)
    fireEvent.click(await screen.findByText('采集详情'))
    expect(within(screen.getByLabelText('任务状态摘要')).queryByText(/本轮扫描|本轮新增|保存失败/)).toBeNull()
    expect(screen.getByText('扫描 8 · 新增 2 · 重复 2 · 过滤 2 · 解析失败 1 · 保存失败 1')).toBeTruthy()
    const records = screen.getByRole('list', { name: 'BOSS 直聘采集异常记录' })
    expect(records.classList.contains('max-h-60')).toBe(true)
    expect(within(records).getByText('解析失败')).toBeTruthy()
    expect(within(records).getByText('保存失败')).toBeTruthy()
    expect(within(records).getByText('测试岗位 · 测试公司')).toBeTruthy()
    expect(within(records).getAllByText('深圳 · AI · 第 2 页')).toHaveLength(2)
    expect(screen.getAllByText('保存岗位失败：RuntimeError')).toHaveLength(1)
  })

  it('keeps scoring details and a collection warning that differs from filtering records', async () => {
    workbenchPayload = baseWorkbench({ task: buildTask({ mode: 'full',
      metrics: { ai_completed: 2, ai_total: 3, ai_passed: 1, ai_filtered: 1 },
      progress: { outcome: 'scoring', platforms: { boss: {
        status: 'completed_with_shortage', new: 3, target: null, filtered: 1,
        message: '翻页未生效，已停止采集',
        filter_records: [{ title: '岗位', company: '公司', source_job_id: '1', reason: '薪资低于要求' }],
      } } },
    }) })
    render(<DashboardPage view="workbench" />)
    fireEvent.click(await screen.findByText('任务详情'))
    const metrics = screen.getByLabelText('任务详细统计')
    expect(within(metrics).getByText('AI已完成')).toBeTruthy()
    expect(within(metrics).getByText('AI总数')).toBeTruthy()
    expect(within(metrics).getByText('AI过滤')).toBeTruthy()
    expect(within(metrics).queryByText('AI通过')).toBeNull()
    fireEvent.click(screen.getByText('采集详情'))
    expect(screen.getByText('翻页未生效，已停止采集')).toBeTruthy()
    expect(screen.getByText('薪资低于要求')).toBeTruthy()
  })

  it('shows bounded filtering details for a finished task without exposing job descriptions', async () => {
    workbenchPayload = baseWorkbench({ last_task: buildTask({ mode: 'full', status: 'completed',
      progress: { outcome: 'completed_with_shortage', platforms: { boss: {
        status: 'completed', new: 0, target: null, filtered: 201, message: '薪资低于硬性要求: 10K < 12K',
        filter_records: [{ title: '测试岗位', company: '测试公司', source_job_id: 'filtered-1', reason: '薪资低于硬性要求: 10K < 12K' }],
      } } },
    }) })
    render(<DashboardPage view="workbench" />)
    fireEvent.click(await screen.findByText('采集详情'))
    const records = screen.getByRole('list', { name: 'BOSS 直聘过滤记录' })
    expect(records.classList.contains('max-h-60')).toBe(true)
    expect(records.classList.contains('overflow-y-auto')).toBe(true)
    expect(records.tabIndex).toBe(0)
    expect(within(records).getByText('测试岗位 · 测试公司')).toBeTruthy()
    expect(screen.getAllByText('薪资低于硬性要求: 10K < 12K')).toHaveLength(1)
    expect(screen.getByText('已保留 1 条，其余记录未保留或来自旧版采集')).toBeTruthy()
    expect(screen.getByText('过滤记录 · 201 条')).toBeTruthy()
    fireEvent.click(screen.getByText('过滤记录 · 201 条'))
    expect(records.closest('details')?.open).toBe(false)
  })

  it.each([false, true])('keeps collection statistics in their proper scope (multiple platforms: %s)', async multiple => {
    const state = { status: 'running', new: 32, seen: 42, target: null, filtered: 9,
      city: '深圳', keyword: 'AI', page: 3, max_pages: 6,
      message: 'BOSS 本轮搜索已结束；本轮新增 32 条，读取 42 条，重复 0 条，过滤 9 条，解析失败 0 条，保存失败 0 条' }
    workbenchPayload = baseWorkbench({ task: buildTask({ mode: 'full', logs: ['开始采集岗位'],
      metrics: { collect_seen: 42, collect_new: 32, collect_filtered: 9 },
      progress: { outcome: 'running', platforms: multiple ? { boss: state, zhilian: state } : { boss: state } },
    }) })
    render(<DashboardPage view="workbench" />)
    await screen.findByLabelText('任务状态摘要')
    expect(screen.queryByText('任务详情')).toBeNull()
    expect(screen.queryByText('浏览器无反应时，请检查 BOSS 登录状态和 Chrome 连接。')).toBeNull()
    const details = screen.getByText('采集详情').closest('details')!
    expect(details.open).toBe(false)
    expect(within(details.querySelector('summary')!).getAllByText(/新增 32 条/)).toHaveLength(multiple ? 2 : 1)
    fireEvent.click(screen.getByText('采集详情'))
    expect(details.open).toBe(true)
    expect(within(details).getAllByText('BOSS 本轮搜索已结束')).toHaveLength(multiple ? 2 : 1)
    expect(within(details).queryAllByText(/扫描 42 · 新增 32/)).toHaveLength(multiple ? 2 : 1)
    expect(within(details).getAllByText(/过滤 9/)).toHaveLength(multiple ? 2 : 1)
  })

  beforeEach(() => {
    workbenchPayload = baseWorkbench()
    stopResponse = () => jsonResponse({})
    preflightResponse = () => jsonResponse({})
    vi.stubGlobal('fetch', fetchMock)
    fetchMock.mockClear()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('starts an interactive guide from an empty workbench and reuses preflight', async () => {
    render(<DashboardPage view="workbench" />)

    const guide = await screen.findByRole('region', { name: '开始使用' })
    expect(within(guide).getByRole('link', { name: /连接 Chrome/ }).getAttribute('href')).toContain('QUICKSTART.md')
    expect(within(guide).getByText(/单独采集且关闭自动评分时/)).toBeTruthy()
    const started = vi.fn()
    window.addEventListener('bosshunter:start-onboarding', started, { once: true })
    fireEvent.click(within(guide).getByRole('button', { name: '开始交互引导' }))
    expect(started).toHaveBeenCalledOnce()

    fireEvent.click(within(guide).getByRole('button', { name: '检查准备情况' }))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/workbench/preflight?mode=full'))
  })

  it('does not keep the getting started card above the workbench after jobs have been collected', async () => {
    workbenchPayload = baseWorkbench({ funnel: { collected: 2 } })
    render(<DashboardPage view="workbench" />)

    expect(await screen.findByRole('heading', { name: '今日求职行动' })).toBeTruthy()
    expect(screen.queryByRole('region', { name: '开始使用' })).toBeNull()
  })

  it('shows setup check failures next to the getting started steps', async () => {
    preflightResponse = () => jsonResponse({
      ok: false,
      checks: [{
        id: 'resume', title: '简历文件', status: 'error', message: '尚未配置有效简历',
        detail: '打开“配置 → 个人信息”，上传简历。', action: 'config',
      }],
    })
    render(<DashboardPage view="workbench" />)

    const guide = await screen.findByRole('region', { name: '开始使用' })
    fireEvent.click(within(guide).getByRole('button', { name: '检查准备情况' }))
    const results = await within(guide).findByRole('region', { name: '启动检查结果' })
    expect(within(results).getByRole('status').textContent).toContain('尚未配置有效简历')
  })

  it('shows the latest queue summary and preserves full progress in expandable details', async () => {
    const latestProgress = '招呼语进度：2/3\n成功 1，失败 1，待处理 1'
    workbenchPayload = baseWorkbench({
      task: buildTask({
        mode: 'full',
        label: '全流程',
        logs: ['招呼语进度：1/3', latestProgress, '正在等待下一次发送窗口'],
      }),
    })
    render(<DashboardPage view="workbench" />)
    const summary = await screen.findByLabelText('任务状态摘要')
    expect(within(summary).getByText('招呼语进度：2/3')).toBeTruthy()
    fireEvent.click(screen.getByText('任务详情'))
    const progress = screen.getByText(latestProgress, { normalizer: text => text })
    expect(progress.textContent).toBe(latestProgress)
    expect(progress.classList.contains('whitespace-pre-line')).toBe(true)
    expect(progress.closest('details:not([open]), [hidden], [aria-hidden="true"]')).toBeNull()
    expect(screen.queryByText('招呼语进度：1/3')).toBeNull()
    expect(screen.getByRole('button', { name: '停止任务' })).toBeTruthy()
  })

  it('recovers the notice and surfaces the real error when stopping fails', async () => {
    workbenchPayload = baseWorkbench({
      task: buildTask({ mode: 'collect', label: '单独采集', status: 'running' }),
    })
    stopResponse = () => jsonResponse({ error: '任务已结束，无法停止' }, false)
    const confirmSpy = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmSpy)
    render(<DashboardPage view="workbench" />)
    const stopButton = await screen.findByRole('button', { name: '停止任务' })
    fireEvent.click(stopButton)
    expect(confirmSpy).toHaveBeenCalled()
    await waitFor(() => {
      expect(screen.getAllByText('单独采集停止失败：任务已结束，无法停止').length).toBeGreaterThan(0)
    })
  })

  it('confirms the stop request when the backend accepts it', async () => {
    workbenchPayload = baseWorkbench({
      task: buildTask({ mode: 'collect', label: '单独采集', status: 'running' }),
    })
    stopResponse = () => jsonResponse(buildTask({ mode: 'collect', label: '单独采集', status: 'stopping', stop_requested: true }))
    vi.stubGlobal('confirm', () => true)
    render(<DashboardPage view="workbench" />)
    const stopButton = await screen.findByRole('button', { name: '停止任务' })
    fireEvent.click(stopButton)
    await waitFor(() => {
      expect(screen.getAllByText('单独采集已请求停止。').length).toBeGreaterThan(0)
    })
  })
  it('summarizes collection limits while keeping full diagnostics in closed details', async () => {
    workbenchPayload = baseWorkbench({ task: buildTask({
      mode: 'full', metrics: { collect_seen: 327, collect_new: 150, collect_parse_failed: 0, collect_save_failed: 2 },
      progress: { outcome: 'completed_with_shortage', platforms: { boss: {
        status: 'completed', new: 150, target: null, seen: 327,
        reason_code: 'safety_limit', message: 'BOSS 采集已达安全上限：daily_detail_page_limit；读取 327 条',
      } } },
    }) })
    render(<DashboardPage view="workbench" />)
    const reason = await screen.findByText('今日详情页次数已用完')
    expect(reason.closest('details')?.open).toBe(false)
    expect(screen.getByText('采集已结束，数量不足')).toBeTruthy()
    expect(screen.queryByText('completed_with_shortage')).toBeNull()
    const metrics = screen.getByLabelText('任务关键统计')
    expect(within(metrics).queryByText('保存失败 2')).toBeNull()
    expect(within(metrics).queryByText('解析失败')).toBeNull()
    expect(screen.queryByText('任务详情')).toBeNull()
  })

})

describe('DashboardPage scoring dialog confirm behaviour', () => {
  const scoringPreview = {
    eligible_jobs: 3,
    skipped_jobs: 0,
    first_attempt_requests: 3,
    max_attempts_per_job: 2,
    max_possible_requests: 6,
    note: '',
  }

  function stubDashboardFetch(overrides: Record<string, () => Response | undefined> = {}) {
    const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const override = overrides[url]
      if (override) {
        const response = override()
        if (response) return response
      }
      if (url === '/api/workbench' && (!init?.method || init.method === 'GET')) return jsonResponse(workbenchPayload)
      if (url.startsWith('/api/jobs/search')) return jsonResponse({ items: [], total: 0, all_total: 0 })
      if (url === '/api/scoring/preview' && init?.method === 'POST') return jsonResponse(scoringPreview)
      if (url === '/api/scoring/runs') return jsonResponse([])
      if (url === '/api/scoring/start' && init?.method === 'POST') {
        return jsonResponse({ run: { remaining_job_ids: ['j1', 'j2', 'j3'] } })
      }
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fn)
    return fn
  }

  beforeEach(() => {
    workbenchPayload = baseWorkbench()
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  async function openScoringDialog() {
    render(<DashboardPage view="jobs" />)
    fireEvent.click(await screen.findByText('评分选项'))
    expect(await screen.findByText('符合条件')).toBeTruthy()
  }

  it('closes the scoring dialog and shows the started notice after scoring starts', async () => {
    stubDashboardFetch()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await openScoringDialog()
    fireEvent.click(screen.getByText('确认开始评分'))
    expect((await screen.findAllByText('独立评分已启动，共 3 个岗位。')).length).toBeGreaterThan(0)
    await waitFor(() => expect(screen.queryByText('确认开始评分')).toBeNull())
  })

  it('keeps the scoring dialog open and shows the failure inside it when the scoring start fails', async () => {
    stubDashboardFetch({
      '/api/scoring/start': () => jsonResponse({ error: 'AI 额度不足' }, false),
    })
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    await openScoringDialog()
    fireEvent.click(screen.getByText('确认开始评分'))
    expect(await screen.findByText('AI 额度不足')).toBeTruthy()
    expect(screen.getByText('确认开始评分')).toBeTruthy()
  })
})
