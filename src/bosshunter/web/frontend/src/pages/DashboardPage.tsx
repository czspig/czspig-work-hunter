import { OutsourcingBadge } from '@/components/jobs/OutsourcingBadge'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useDashboard, type CollectionProgress, type HistoryItem, type Job, type WorkbenchTask } from '@/hooks/useDashboard'
import { useJobSearch, type JobSortKey, type JobSortOrder } from '@/hooks/useJobSearch'
import { Button } from '@/components/ui/button'
import { START_ONBOARDING_EVENT } from '@/components/onboarding/OnboardingTour'
import { JobsTable } from '@/components/dashboard/JobsTable'
import { RecycleBinPanel } from '@/components/dashboard/RecycleBinPanel'
import { ScoreJobsDialog } from '@/components/dashboard/ScoreJobsDialog'
import { CollectJobsDialog } from '@/components/dashboard/CollectJobsDialog'
import { TrendChart } from '@/components/dashboard/TrendChart'
import { TopCompanies } from '@/components/dashboard/TopCompanies'
import { PipelineFlow } from '@/components/dashboard/PipelineFlow'
import { RecentActivity } from '@/components/dashboard/RecentActivity'
import type { ActivityData, TopCompany } from '@/hooks/useDashboard'
import { JobFilterBar } from '@/components/jobs/JobFilterBar'
import { PLATFORM_LABELS } from '@/lib/platforms'
import {
  EMPTY_JOB_FILTERS,
  filterJobs,
  hasInvalidSalaryRange,
  jobCityBaseName,
  useDebouncedValue,
  type JobFilters,
} from '@/lib/jobFilters'
import { getStatusLabel } from '@/lib/status'
import { cn } from '@/lib/utils'
import {
  AlertTriangle,
  BriefcaseBusiness,
  ChevronDown,
  Download,
  ExternalLink,
  Eye,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Trash2,
  XCircle,
} from 'lucide-react'

type WorkbenchMode = 'full' | 'collect' | 'rescore'
type DashboardView = 'workbench' | 'jobs'
type StatsScope = 'today' | 'total'

const TASK_STAGE_LABELS = [
  '开始采集岗位',
  '开始 AI 评分',
  '开始重新评分',
  'AI 评分进度',
]

function currentTaskStage(task: WorkbenchTask) {
  if (task.progress?.outcome === 'scoring' && ['running', 'stopping'].includes(task.status)) {
    const completed = task.metrics?.ai_completed
    const total = task.metrics?.ai_total
    return typeof total === 'number'
      ? `AI 评分进度 ${completed || 0}/${total}`
      : '采集已结束，正在为本轮新增岗位进行 AI 评分'
  }
  const logs = task.logs || []
  for (const log of logs.slice().reverse()) {
    if (log.includes('AI 评分进度')) return log
    if (log.includes('招呼语进度')) return log
    if (log.includes('招呼语发送结果')) return log
    if (log.includes('发送招呼语')) return '发送招呼语'
    if (log.includes('招呼语生成结果')) return log
    if (log.includes('生成招呼语')) return log
    if (log.includes('本轮监测完成')) return log
    const stage = TASK_STAGE_LABELS.find(label => log.includes(label))
    if (stage) return stage
  }
  if (task.status === 'running') return `${task.label}正在启动`
  if (task.status === 'stopping') return `${task.label}正在停止`
  if (task.status === 'completed') return `${task.label}已完成`
  if (task.status === 'stopped') return `${task.label}已停止`
  if (task.status === 'failed') return `${task.label}运行失败`
  return `${task.label}状态未知`
}

function taskStatusText(status: string) {
  if (status === 'failed') return '运行失败'
  if (status === 'completed') return '已结束'
  if (status === 'stopped') return '已停止'
  if (status === 'stopping') return '停止中'
  return '运行中'
}

function taskStatusClass(status: string) {
  if (status === 'failed') return 'border-danger-border bg-danger-soft'
  if (status === 'completed' || status === 'stopped') return 'border-card-border bg-card'
  return 'border-primary/20 bg-secondary'
}

function taskStatusTitle(status: string) {
  if (status === 'completed' || status === 'stopped') return '最近任务状态'
  return '当前阶段'
}

function taskStopReasonLabel(reason?: string) {
  if (reason === 'stopped') return '任务已按你的要求停止，已入库岗位会保留。'
  return reason
}

function taskErrorFeedback(error: string) {
  const normalized = error.toLowerCase()
  if (
    normalized.includes('api key')
    || normalized.includes('authentication')
    || normalized.includes('unauthorized')
    || normalized.includes('401')
    || normalized.includes('403')
  ) {
    return {
      title: 'AI 接口认证失败',
      detail: '请到“配置 → AI 设置”检查 API Key、Base URL 和模型名称，保存后点击“测试连接”。',
    }
  }
  if (
    normalized.includes('chrome')
    || normalized.includes('cdp')
    || normalized.includes('websocket')
    || normalized.includes('browser runtime')
    || normalized.includes('not connected')
  ) {
    return {
      title: 'Google Chrome 连接中断',
      detail: '请确认 Chrome 已启动并开启远程调试，再点击上方“全流程预检”。',
    }
  }
  if (normalized.includes('zhipin') || normalized.includes('登录') || normalized.includes('login')) {
    return {
      title: '招聘平台页面或登录状态异常',
      detail: '请在已连接的 Google Chrome 中打开 BOSS 直聘并确认账号仍处于登录状态。',
    }
  }
  return {
    title: '任务运行失败',
    detail: '请查看原始错误；修复配置或连接问题后，重新运行启动检查。',
  }
}

interface DashboardPageProps {
  view?: DashboardView
}

interface PreflightCheck {
  id: string
  title: string
  status: 'pass' | 'warning' | 'error'
  message: string
  detail: string
  action?: 'config' | 'browser' | ''
}

const modes: Array<{ mode: WorkbenchMode; title: string; description: string }> = [
  {
    mode: 'full',
    title: '采集并评分',
    description: '搜索岗位并用 AI 打分，投递由你在 BOSS 直聘完成',
  },
  {
    mode: 'collect',
    title: '单独采集',
    description: '按平台和条件搜索岗位',
  },
]

const statItems = [
  { key: '采集总数', todayLabel: '今日新增岗位', totalLabel: '累计采集岗位' },
  { key: '初筛通过', todayLabel: '今日初筛通过', totalLabel: '累计初筛通过', highlight: true },
  { key: 'AI评分', todayLabel: '今日 AI 评分', totalLabel: '累计 AI 评分' },
  { key: 'pending', todayLabel: '当前待处理', totalLabel: '当前待处理', highlight: true, current: true },
  { key: '发送', todayLabel: '今日已投递', totalLabel: '累计已投递', highlight: true },
]

const taskMetricItems = [
  { key: 'collect_seen', label: '本轮扫描' },
  { key: 'collect_new', label: '本轮新增' },
  { key: 'collect_duplicate', label: '重复岗位' },
  { key: 'collect_filtered', label: '过滤' },
  { key: 'collect_parse_failed', label: '解析失败' },
  { key: 'collect_save_failed', label: '保存失败' },
  { key: 'ai_completed', label: 'AI已完成' },
  { key: 'ai_total', label: 'AI总数' },
  { key: 'ai_passed', label: 'AI通过' },
  { key: 'ai_filtered', label: 'AI过滤' },
  { key: 'ai_failed', label: 'AI失败' },
]

function jobSubtitle(job: Job) {
  return [job.score ? `匹配 ${job.score}` : '', job.salary, job.hr_active || '活跃度未知', getStatusLabel(job.status)].filter(Boolean).join(' · ')
}

async function parsePreflightResponse(res: Response) {
  const rawText = await res.text()
  let data: { ok?: boolean; messages?: unknown; checks?: unknown; error?: string } = {}
  try {
    data = rawText ? JSON.parse(rawText) : {}
  } catch {
    const message = `无法解析预检响应：预检接口返回 ${res.status}`
    return {
      ok: false,
      messages: [message],
      checks: [{ id: 'preflight_api', title: '启动检查', status: 'error', message, detail: '请重启猪猪求职助手后重试。' }] as PreflightCheck[],
    }
  }
  const messages = Array.isArray(data.messages) ? [...new Set(data.messages.map(String).filter(Boolean))] : []
  const checks = Array.isArray(data.checks)
    ? data.checks.filter((item): item is PreflightCheck => Boolean(
      item
      && typeof item === 'object'
      && 'id' in item
      && 'status' in item
      && 'message' in item
    ))
    : []
  if (data.error && !messages.includes(String(data.error))) messages.push(String(data.error))
  const hasActionableChecks = checks.some(check => check.status !== 'pass')
  if (!res.ok && !hasActionableChecks && messages.length === 0) messages.push(`预检接口返回 ${res.status}`)
  if (!data.ok && !hasActionableChecks && messages.length === 0) messages.push('后端未返回具体原因')
  if ((!res.ok || !data.ok) && !hasActionableChecks && messages.length > 0) {
    checks.push(...messages.map((message, index) => ({
      id: `legacy-${index}`,
      title: '启动检查',
      status: 'error' as const,
      message,
      detail: !res.ok ? `接口状态：HTTP ${res.status}。请修复后重新检查。` : '',
    })))
  }
  return { ok: Boolean(res.ok && data.ok), messages, checks }
}

function CompactNotice({ message, danger = false }: { message: string; danger?: boolean }) {
  return (
    <details className={cn('group mt-2 rounded-lg px-2 text-xs', danger ? 'bg-danger-soft text-danger' : 'bg-secondary text-primary')}>
      <summary className="flex h-8 cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden">
        <span role="status" className="min-w-0 flex-1 truncate" title={message}>{message}</span>
        <ChevronDown className="h-3 w-3 shrink-0 group-open:rotate-180" />
      </summary>
      <p className="break-words border-t border-current/10 py-2 leading-5">{message}</p>
    </details>
  )
}

function PreflightPanel({
  checks,
  checking,
  onRetry,
}: {
  checks: PreflightCheck[]
  checking: boolean
  onRetry: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const actionableChecks = checks.filter(check => check.status !== 'pass')
  if (actionableChecks.length === 0) return null

  const errors = actionableChecks.filter(check => check.status === 'error').length
  const needsConfig = actionableChecks.some(check => check.action === 'config')
  const heading = `${actionableChecks.length} 项${errors ? '待处理' : '提醒'}`
  const summary = `${heading} · ${actionableChecks[0].message}`

  return (
    <div role="region" aria-label="启动检查结果" className={`mt-2 rounded-lg border px-2 text-xs ${
      errors ? 'border-danger-border bg-danger-soft' : 'border-warning-border bg-warning-soft'
    }`}>
      <div className="flex h-8 min-w-0 items-center gap-2">
        {errors
          ? <XCircle className="h-3.5 w-3.5 shrink-0 text-danger" />
          : <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" />}
        <span role="status" title={summary} className="min-w-0 flex-1 truncate text-foreground">{summary}</span>
        <button type="button" aria-expanded={expanded} aria-controls="preflight-details" onClick={() => setExpanded(value => !value)} className="flex h-7 shrink-0 items-center gap-1 rounded px-1 text-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
          {expanded ? '收起' : '详情'}<ChevronDown className={cn('h-3 w-3', expanded && 'rotate-180')} />
        </button>
        {needsConfig && (
          <Button variant="ghost" size="sm" className="h-7 shrink-0 px-1" onClick={() => window.location.assign('/config')}>
            配置
          </Button>
        )}
        <Button variant="ghost" size="sm" className="h-7 shrink-0 px-1" onClick={onRetry} disabled={checking}>
          <RefreshCw className={`mr-1 h-3 w-3 ${checking ? 'animate-spin' : ''}`} />
          {checking ? '检查中' : '重新检查'}
        </Button>
      </div>
      <div id="preflight-details" hidden={!expanded} className="space-y-2 border-t border-current/10 py-2 text-xs leading-5">
        {actionableChecks.map(check => (
          <div key={`${check.id}-${check.title}`} className="break-words">
            <p className="text-foreground"><span className="font-semibold">{check.title}：</span>{check.message}</p>
            {check.detail && <p className="text-muted">{check.detail}</p>}
          </div>
        ))}
      </div>
    </div>
  )
}

function GettingStartedCard({
  onCheck,
  checking,
  notice,
  checks,
  onRetry,
}: {
  onCheck: () => void
  checking: boolean
  notice: string
  checks: PreflightCheck[]
  onRetry: () => void
}) {
  return (
    <section aria-label="开始使用" className="rounded-2xl border border-primary/30 bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <div className="mb-1 flex items-center gap-2">
            <span className="rounded-md bg-primary/15 px-2 py-0.5 text-[11px] font-bold text-primary">首次使用</span>
            <h2 className="text-lg font-black text-foreground">开始使用</h2>
          </div>
          <p className="text-sm text-muted">跟着页面提示，完成简历、搜索和 AI 配置，再检查运行环境。</p>
        </div>
        <Button size="sm" onClick={() => window.dispatchEvent(new Event(START_ONBOARDING_EVENT))}>开始交互引导</Button>
      </div>
      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-card-border pt-3">
        <p className="max-w-xl text-xs leading-5 text-muted">单独采集且关闭自动评分时，可先跳过简历和 AI 设置；运行全流程前请完成全部准备。</p>
        <div className="flex flex-wrap items-center gap-3">
          <a data-tour="chrome-guide" className="text-xs font-semibold text-primary underline-offset-2 hover:underline" href="https://github.com/czspig/czspig-work-hunter/blob/main/docs/QUICKSTART.md#3-开启-chrome-远程调试" target="_blank" rel="noreferrer">连接 Chrome 说明 ↗</a>
          <Button data-tour="check-preflight" variant="secondary" size="sm" onClick={onCheck} disabled={checking}>
            <ShieldCheck className="mr-1.5 h-3.5 w-3.5" />
            {checking ? '检查中' : '检查准备情况'}
          </Button>
        </div>
      </div>
      {notice && <CompactNotice message={notice} />}
      {checks.some(check => check.status !== 'pass') && (
        <PreflightPanel checks={checks} checking={checking} onRetry={onRetry} />
      )}
    </section>
  )
}

export default function DashboardPage({ view = 'workbench' }: DashboardPageProps) {
  const {
    workbench,
    loading,
    error,
    refreshing,
    lastRefreshedAt,
    refresh,
    updateJobStatus,
    startTask,
    stopTask,
  } = useDashboard(view)
  const [selected, setSelected] = useState<string[]>([])
  const [notice, setNotice] = useState('')

  // Dashboard visualization state
  const [activityData, setActivityData] = useState<ActivityData[]>([])
  const [topCompanies, setTopCompanies] = useState<TopCompany[]>([])
  const [recentActivity, setRecentActivity] = useState<HistoryItem[]>([])
  const [preflightChecks, setPreflightChecks] = useState<PreflightCheck[]>([])
  const [preflightMode, setPreflightMode] = useState<WorkbenchMode>('full')
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [modePending, setModePending] = useState<WorkbenchMode | null>(null)
  const [confirmedDeliveryIds, setConfirmedDeliveryIds] = useState<Set<string>>(new Set())
  const [todayFilters, setTodayFilters] = useState<JobFilters>({ ...EMPTY_JOB_FILTERS })
  const [statsScope, setStatsScope] = useState<StatsScope>('today')
  const [collectDialogOpen, setCollectDialogOpen] = useState(false)
  const [collectDialogMode, setCollectDialogMode] = useState<'collect' | 'full'>('collect')
  const [preflightRunning, setPreflightRunning] = useState(false)

  const todayJobs = useMemo(
    () => workbench.pending_confirmation.filter(job => !confirmedDeliveryIds.has(job.id)),
    [workbench.pending_confirmation, confirmedDeliveryIds]
  )
  const todayCityOptions = useMemo(
    () => [...new Set(todayJobs.map(job => jobCityBaseName(job.city)).filter(Boolean))].sort(),
    [todayJobs]
  )
  const debouncedTodayQuery = useDebouncedValue(todayFilters.query, 250)
  const activeTodayFilterCount = Object.values(todayFilters).filter(value => Array.isArray(value) ? value.length > 0 : value !== '').length
  const effectiveTodayFilters = useMemo(
    () => ({ ...todayFilters, query: debouncedTodayQuery }),
    [todayFilters, debouncedTodayQuery]
  )
  const filteredTodayJobs = useMemo(
    () => filterJobs(todayJobs, effectiveTodayFilters),
    [todayJobs, effectiveTodayFilters]
  )
  const visibleJobIds = useMemo(() => new Set(filteredTodayJobs.map(job => job.id)), [filteredTodayJobs])
  const actionableSelected = useMemo(() => selected.filter(id => visibleJobIds.has(id)), [selected, visibleJobIds])

  useEffect(() => {
    setSelected(previous => {
      const next = previous.filter(id => visibleJobIds.has(id))
      return next.length === previous.length ? previous : next
    })
  }, [visibleJobIds])

  useEffect(() => {
    const handleConfigSaved = () => { void refresh() }
    window.addEventListener('bosshunter-config-saved', handleConfigSaved)
    return () => window.removeEventListener('bosshunter-config-saved', handleConfigSaved)
  }, [refresh])

  // Keep the homepage overview separate from the monitor's conversation history.
  useEffect(() => {
    if (view !== 'workbench') return
    const controller = new AbortController()
    const fetchOverview = async <T,>(url: string, update: (data: T) => void) => {
      const response = await fetch(url, { cache: 'no-store', signal: controller.signal })
      if (!response.ok) return
      const data: T = await response.json()
      if (!controller.signal.aborted) update(data)
    }
    const fetchVisualizationData = async () => {
      // One unavailable chart must not prevent the other overview cards loading.
      await Promise.allSettled([
        fetchOverview('/api/activity?days=7', setActivityData),
        fetchOverview('/api/top-companies?limit=5', setTopCompanies),
        fetchOverview('/api/history?limit=3', setRecentActivity),
      ])
    }
    void fetchVisualizationData()
    const interval = window.setInterval(fetchVisualizationData, 60000)
    return () => {
      controller.abort()
      window.clearInterval(interval)
    }
  }, [view])

  const activeTask = workbench.task
  const visibleTask = activeTask || workbench.last_task
  const visibleTaskError = visibleTask?.error ? taskErrorFeedback(visibleTask.error) : null
  const fullStage = visibleTask ? currentTaskStage(visibleTask) : ''
  const hasStageDetails = fullStage.trim().includes('\n')
  const detailMetrics = taskMetricItems.filter(item => {
    const metrics = visibleTask?.metrics || {}
    if (!(item.key in metrics)) return false
    if (item.key.startsWith('collect_') && visibleTask?.progress?.platforms) return false
    if (['collect_seen', 'collect_new', 'ai_passed'].includes(item.key)) return false
    return !(item.key.endsWith('_failed') && Number(metrics[item.key]) > 0)
  })
  const taskSummary = visibleTask
    ? visibleTaskError?.title || taskStopReasonLabel(visibleTask.stop_reason) || currentTaskStage(visibleTask)
    : ''

  const toggleJob = (id: string) => {
    setSelected(prev => (prev.includes(id) ? prev.filter(item => item !== id) : [...prev, id]))
  }

  const runPreflight = async (mode: WorkbenchMode, options?: Record<string, unknown>) => {
    setPreflightMode(mode)
    const res = options
      ? await fetch('/api/workbench/preflight', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, options }),
      })
      : await fetch(`/api/workbench/preflight?mode=${mode}`)
    const data = await parsePreflightResponse(res)
    setPreflightChecks(data.checks)
    if (!data.ok) {
      setNotice('')
      return false
    }
    return true
  }

  const handleModeClick = async (mode: WorkbenchMode) => {
    try {
      if (activeTask?.mode === mode) {
        if (window.confirm(`是否停止当前${activeTask.label}任务？已入库岗位会保留。`)) {
          setModePending(mode)
          setNotice(`正在停止${activeTask.label}...`)
          await stopTask(activeTask.id)
          setNotice(`${activeTask.label}已请求停止。`)
        }
        return
      }
      if (modePending) return
      if (activeTask) {
        setNotice(
          activeTask.status === 'stopping'
            ? `当前${activeTask.label}正在停止，请等待后台完全结束后再启动其他模式。`
            : `当前正在运行${activeTask.label}，请先点击正在运行的卡片停止后再启动其他模式。`
        )
        return
      }
      if (mode === 'full') {
        setCollectDialogMode('full')
        setCollectDialogOpen(true)
        return
      }
      const target = modes.find(item => item.mode === mode)
      setModePending(mode)
      setNotice(`${target?.title || '任务'}启动前预检中...`)
      if (!(await runPreflight(mode))) return
      setNotice(`${target?.title || '任务'}启动中，请稍候...`)
      await startTask(mode)
      setNotice(`${target?.title || '任务'}已启动，日志会在下方更新。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '操作失败')
    } finally {
      setModePending(null)
    }
  }

  const retryPreflight = async () => {
    if (modePending) return
    try {
      setModePending(preflightMode)
      setNotice('正在重新检查运行环境...')
      await runPreflight(preflightMode)
      setNotice('')
    } catch {
      setNotice('重新检查失败，请确认猪猪求职助手后端仍在运行。')
    } finally {
      setModePending(null)
    }
  }

  const runStandalonePreflight = async () => {
    if (modePending || preflightRunning) return
    try {
      setPreflightRunning(true)
      setNotice('正在检查全流程运行环境...')
      const ok = await runPreflight('full')
      setNotice(ok ? '全流程预检通过，可以开始任务。' : '')
    } catch {
      setNotice('预检失败，请确认猪猪求职助手后端仍在运行。')
    } finally {
      setPreflightRunning(false)
    }
  }

  const startCollection = async (options: Record<string, unknown>): Promise<{ ok: boolean; error?: string }> => {
    const mode = collectDialogMode
    setModePending(mode)
    setNotice(mode === 'full' ? '全流程启动前预检中...' : '岗位采集启动前预检中...')
    try {
      if (!(await runPreflight(mode, options))) {
        setNotice('启动前预检未通过：请按下方检查提示修复后，再重新启动。')
        return { ok: false, error: '启动前预检未通过：请关闭弹窗后按检查提示修复，再重新启动。' }
      }
      setNotice('启动前预检通过，正在启动任务...')
      await startTask(mode, options)
      setNotice(mode === 'full' ? '全流程已启动，进度会在下方更新。' : '岗位采集已启动，进度会在下方更新。')
      return { ok: true }
    } catch (err) {
      const message = err instanceof Error ? err.message : '岗位采集启动失败'
      setNotice(message)
      return { ok: false, error: message }
    } finally {
      setModePending(null)
    }
  }

  const markApplied = async (ids: string[]) => {
    if (!ids.length) return
    if (!window.confirm(`确认已在 BOSS 直聘投递这 ${ids.length} 个岗位？只会更新本地状态。`)) return
    try {
      const res = await fetch('/api/jobs/manual-sent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_ids: ids, confirmed: true }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '标记失败')
      }
      setConfirmedDeliveryIds(prev => new Set([...prev, ...ids]))
      setSelected(prev => prev.filter(id => !ids.includes(id)))
      await refresh()
      setNotice(`已标记 ${ids.length} 个岗位为已投递。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '标记失败')
    }
  }

  const rejectSelectedJobs = async (ids: string[]) => {
    if (!ids.length) return
    const count = ids.length
    if (!window.confirm(`确定放弃这 ${count} 个岗位吗？可在岗位池中查看已拒绝状态。`)) return
    try {
      const res = await fetch('/api/workbench/reject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_ids: ids }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '放弃失败')
      }
      const rejectedIds = new Set(ids)
      setSelected(prev => prev.filter(id => !rejectedIds.has(id)))
      setConfirmedDeliveryIds(prev => new Set([...prev, ...ids]))
      await refresh()
      setNotice(`已放弃 ${count} 个岗位。`)
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '放弃失败')
    }
  }

  const openJobDetail = async (job: Job) => {
    try {
      const res = await fetch(`/api/jobs/${job.id}`)
      if (!res.ok) throw new Error('读取岗位详情失败')
      setSelectedJob(await res.json())
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '读取岗位详情失败')
    }
  }

  if (loading) {
    return <div className="flex h-full items-center justify-center text-sm text-muted">加载中...</div>
  }

  if (view === 'jobs') {
    return <JobsPoolView updateJobStatus={updateJobStatus} />
  }

  const showGettingStarted = !Object.values(workbench.funnel).some(count => count > 0)

  return (
    <div className="space-y-4">
      {showGettingStarted && (
        <GettingStartedCard
          onCheck={runStandalonePreflight}
          checking={preflightRunning || Boolean(modePending)}
          notice={notice}
          checks={preflightChecks}
          onRetry={retryPreflight}
        />
      )}
      <section id="today-workbench" className="scroll-mt-6 rounded-2xl border border-card-border bg-card p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <h2 className="text-lg font-bold tracking-tight">今日求职行动</h2>
          <div className="flex items-center gap-2 text-xs text-muted">
            <span className="mr-1 hidden sm:inline">{activeTask ? `${activeTask.label}中` : '当前空闲'}</span>
            <Button variant="ghost" size="sm" className="h-7 px-1" onClick={runStandalonePreflight} disabled={refreshing || Boolean(modePending) || preflightRunning}>
              <ShieldCheck className={cn('mr-1 h-3.5 w-3.5', preflightRunning && 'animate-spin')} />
              {preflightRunning ? '预检中' : '全流程预检'}
            </Button>
            <Button variant="ghost" size="sm" className="h-7 px-1" onClick={refresh} disabled={refreshing} title={lastRefreshedAt ? `最后刷新：${lastRefreshedAt.toLocaleTimeString('zh-CN', { hour12: false })}` : undefined}>
              <RefreshCw className={cn('mr-1 h-3.5 w-3.5', refreshing && 'animate-spin')} />
              {refreshing ? '刷新中' : '刷新'}
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3">
          {modes.map(item => {
            const isActive = activeTask?.mode === item.mode
            const disabled = Boolean(activeTask && !isActive)
            return (
              <button
                key={item.mode}
                onClick={() => {
                  if (isActive) {
                    void handleModeClick(item.mode)
                    return
                  }
                  if (disabled) {
                    setNotice(`当前正在运行${activeTask?.label || '其他任务'}，请先停止后再启动岗位采集。`)
                    return
                  }
                  if (item.mode === 'collect' || item.mode === 'full') {
                    setCollectDialogMode(item.mode)
                    setCollectDialogOpen(true)
                  }
                  else void handleModeClick(item.mode)
                }}
                aria-disabled={disabled}
                className={cn(
                  'min-w-0 rounded-xl border p-4 text-left transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary md:min-h-32 md:p-5',
                  item.mode === 'full' ? 'col-span-2 min-h-28 md:col-span-1' : 'min-h-24',
                  isActive
                    ? 'border-primary bg-primary text-primary-foreground'
                    : disabled
                      ? 'cursor-not-allowed border-card-border bg-card text-muted opacity-45'
                      : 'border-card-border bg-surface text-foreground hover:border-primary/60 hover:shadow-md'
                )}
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className={cn('font-bold tracking-tight', item.mode === 'full' ? 'text-2xl' : 'text-xl')}>
                    {modePending === item.mode
                      ? isActive ? '任务停止中' : '任务启动中'
                      : isActive ? `${item.title}中` : item.title}
                  </div>
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center">
                    {isActive ? <Square className="h-4 w-4 fill-current" /> : <Play className="h-5 w-5 fill-current" />}
                  </span>
                </div>
                <p className={`text-xs leading-5 ${isActive ? 'text-primary-foreground/85' : 'text-muted'}`}>{item.description}</p>
              </button>
            )
          })}
        </div>
        {!showGettingStarted && notice && <CompactNotice message={notice} />}
        {!showGettingStarted && preflightChecks.some(check => check.status !== 'pass') && (
          <PreflightPanel checks={preflightChecks} checking={Boolean(modePending) || preflightRunning} onRetry={retryPreflight} />
        )}
        {error && <CompactNotice message={error} danger />}
        {visibleTask && (
          <div className="mt-3 border-t border-card-border pt-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-sm font-semibold">{visibleTask.label}</div>
            <div className="flex flex-wrap items-center gap-2">
              {activeTask && (
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={activeTask.status !== 'running'}
                  onClick={() => {
                    if (!window.confirm(`是否停止当前${activeTask.label}任务？已入库数据会保留。`)) return
                    setNotice(`正在停止${activeTask.label}...`)
                    void stopTask(activeTask.id)
                      .then(() => setNotice(`${activeTask.label}已请求停止。`))
                      .catch(err => setNotice(
                        err instanceof Error
                          ? `${activeTask.label}停止失败：${err.message}`
                          : `${activeTask.label}停止失败，请稍后重试。`
                      ))
                  }}
                >
                  {activeTask.status === 'stopping' ? '正在停止...' : '停止任务'}
                </Button>
              )}
            </div>
            </div>
            <div className={`mt-3 rounded-xl border px-3 py-2.5 ${taskStatusClass(visibleTask.status)}`}>
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs" aria-label="任务状态摘要">
                <div className="flex shrink-0 items-center gap-2">
                  <span className="font-semibold text-foreground">{taskStatusTitle(visibleTask.status)}</span>
                  <span className="text-muted">{taskStatusText(visibleTask.status)}</span>
                </div>
                <p className="min-w-0 flex-[1_1_12rem] truncate leading-5 text-foreground" title={currentTaskStage(visibleTask)}>{currentTaskStage(visibleTask).split('\n')[0]}</p>
                {visibleTask.metrics && (
                  <div className="flex max-w-full flex-wrap items-center gap-x-4 gap-y-1" aria-label="任务关键统计">
                    {taskMetricItems.filter(item => item.key === 'ai_passed' && item.key in visibleTask.metrics!).map(item => (
                      <span key={item.key} className="whitespace-nowrap text-muted">{item.label} <strong className="font-semibold tabular-nums text-foreground">{visibleTask.metrics?.[item.key]}</strong></span>
                    ))}
                    {taskMetricItems.filter(item => !item.key.startsWith('collect_') && item.key.endsWith('_failed') && Number(visibleTask.metrics?.[item.key]) > 0).map(item => (
                      <span key={item.key} className="whitespace-nowrap text-danger">{item.label} {visibleTask.metrics?.[item.key]}</span>
                    ))}
                  </div>
                )}
                {visibleTask.deadline_at && <span className="text-muted">截止 {new Date(visibleTask.deadline_at).toLocaleString('zh-CN', { hour12: false })}</span>}
              </div>
              {(hasStageDetails || detailMetrics.length > 0) && (
                <details className="group mt-2 border-t border-card-border/60 pt-2 text-xs">
                  <summary className="flex w-fit cursor-pointer list-none items-center gap-1 text-muted hover:text-foreground [&::-webkit-details-marker]:hidden">
                    <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />任务详情
                  </summary>
                  {hasStageDetails && <p className="mt-2 whitespace-pre-line break-words leading-5 text-muted">{fullStage}</p>}
                  {detailMetrics.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-2" aria-label="任务详细统计">
                      {detailMetrics.map(item => (
                        <span key={item.key} className="text-muted">{item.label} <strong className="font-medium tabular-nums text-foreground">{visibleTask.metrics?.[item.key]}</strong></span>
                      ))}
                    </div>
                  )}
                </details>
              )}
            </div>
            {visibleTask.progress?.platforms && <CollectionProgressPanel progress={visibleTask.progress} />}
            {visibleTask.error && visibleTaskError && (
              <div className="mt-3 rounded-2xl border border-danger-border bg-danger-soft px-4 py-3 text-sm text-danger">
                <div className="font-black">{visibleTaskError.title}</div>
                <p className="mt-1 text-xs leading-5">{visibleTaskError.detail}</p>
                <details className="mt-2 text-xs text-muted">
                  <summary className="cursor-pointer font-bold">查看原始错误</summary>
                  <pre className="mt-2 whitespace-pre-wrap break-words rounded-lg bg-card p-2">{visibleTask.error}</pre>
                </details>
              </div>
            )}
            {visibleTask.stop_reason && (
              <div className="mt-3 rounded-2xl bg-secondary px-3 py-3 text-sm text-primary">
                <div className="font-black">任务说明</div>
                <div className="mt-1">{taskStopReasonLabel(visibleTask.stop_reason)}</div>
              </div>
            )}
          </div>
        )}
      </section>

      <section>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-black">求职数据</h3>
          </div>
          <div className="inline-flex rounded-full border border-card-border bg-card p-1">
            {([
              { value: 'today' as const, label: '今日数据' },
              { value: 'total' as const, label: '累计数据' },
            ]).map(option => (
              <button
                key={option.value}
                type="button"
                onClick={() => setStatsScope(option.value)}
                className={`rounded-full px-3 py-1.5 text-xs font-black transition ${
                  statsScope === option.value ? 'bg-primary text-primary-foreground shadow-sm' : 'text-muted hover:text-primary'
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          {statItems.map(item => {
            const currentValue = workbench.pending_confirmation.length
            const selectedFunnel = statsScope === 'today' ? workbench.funnel_today : workbench.funnel
            const alternateFunnel = statsScope === 'today' ? workbench.funnel : workbench.funnel_today
            const value = item.current ? currentValue : (selectedFunnel[item.key] || 0)
            const supportingText = item.current
              ? '实时待处理数量'
              : `${statsScope === 'today' ? '累计' : '今日'} ${alternateFunnel[item.key] || 0}`
            return (
              <div key={item.key} className="rounded-2xl border border-card-border bg-card p-4">
                <div className="text-xs text-muted">{statsScope === 'today' ? item.todayLabel : item.totalLabel}</div>
                <div className={`mt-1 text-2xl font-black ${item.highlight ? 'text-primary' : 'text-foreground'}`}>
                  {value}
                </div>
                <div className="mt-1 text-[10px] font-bold text-muted">{supportingText}</div>
              </div>
            )
          })}
        </div>
      </section>


      {/* 数据可视化概览：7日趋势 + 高分公司 + 最近活动 */}
      {(activityData.length > 0 || topCompanies.length > 0 || recentActivity.length > 0) ? (
        <section>
          <div className="mb-3">
            <h3 className="text-lg font-black">求职数据概览</h3>
          </div>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <TrendChart data={activityData} />
            </div>
            <TopCompanies data={topCompanies} />
          </div>
          <div className="mt-3">
            <RecentActivity data={recentActivity} />
          </div>
        </section>
      ) : (
        <section>
          <div className="mb-3">
            <h3 className="text-lg font-black">开始你的求职之旅</h3>
            <p className="mt-0.5 text-xs text-muted">配置好简历和 AI 接口后，点击上方按钮启动岗位采集</p>
          </div>
          <PipelineFlow />
        </section>
      )}
      <section className="rounded-2xl border border-card-border bg-card p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-black">今日待处理</h3>
            <p className="mt-0.5 text-xs text-muted">打开岗位链接，在 BOSS 直聘自行沟通投递，再回来标记。</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" disabled={!filteredTodayJobs.length} onClick={() => setSelected(filteredTodayJobs.map(job => job.id))}>全选</Button>
            <Button variant="secondary" size="sm" disabled={!selected.length} onClick={() => setSelected([])}>清空</Button>
            <Button variant="secondary" size="sm" disabled={!actionableSelected.length} onClick={() => rejectSelectedJobs(actionableSelected)}>放弃已选 {actionableSelected.length} 个</Button>
            <Button size="sm" disabled={!actionableSelected.length} onClick={() => markApplied(actionableSelected)}>标记已投递 {actionableSelected.length} 个</Button>
          </div>
        </div>
        <details className="group mb-3 rounded-xl border border-card-border bg-surface">
          <summary className="flex cursor-pointer list-none flex-wrap items-center justify-between gap-2 px-3 py-2 text-xs [&::-webkit-details-marker]:hidden">
            <span className="flex items-center gap-2 font-bold">
              <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
              筛选条件
              {activeTodayFilterCount > 0 && <span className="font-normal text-primary">已启用 {activeTodayFilterCount} 项</span>}
            </span>
            <span className={hasInvalidSalaryRange(todayFilters) ? 'text-danger' : 'text-muted'}>
              {hasInvalidSalaryRange(todayFilters) ? '薪资范围有误，请展开调整' : `${filteredTodayJobs.length} / ${todayJobs.length} 个岗位`}
            </span>
          </summary>
          <div className="px-2 pb-2">
            <JobFilterBar
              compact
              filters={todayFilters}
              onChange={setTodayFilters}
              onReset={() => setTodayFilters({ ...EMPTY_JOB_FILTERS })}
              resultCount={filteredTodayJobs.length}
              totalCount={todayJobs.length}
              invalidSalary={hasInvalidSalaryRange(todayFilters)}
              cityOptions={todayCityOptions}
            />
          </div>
        </details>
        {filteredTodayJobs.length ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {filteredTodayJobs.map(job => (
              <JobActionCard
                key={job.id}
                job={job}
                selected={selected.includes(job.id)}
                onToggle={() => toggleJob(job.id)}
                onDetail={() => openJobDetail(job)}
                onReject={() => rejectSelectedJobs([job.id])}
              />
            ))}
          </div>
        ) : todayJobs.length ? (
          <div className="rounded-2xl border border-dashed border-card-border bg-surface p-5 text-center text-sm text-muted">
            <p>没有符合当前条件的岗位</p>
            <Button className="mt-3" variant="secondary" size="sm" onClick={() => setTodayFilters({ ...EMPTY_JOB_FILTERS })}>重置筛选</Button>
          </div>
        ) : (
          <p className="py-2 text-xs text-muted">今天暂时没有待处理岗位。</p>
        )}
      </section>

      {selectedJob && <JobDetailModal job={selectedJob} onClose={() => setSelectedJob(null)} onChanged={() => void refresh()} />}
      <CollectJobsDialog
        open={collectDialogOpen}
        mode={collectDialogMode}
        activeTask={activeTask && (activeTask.mode === 'collect' || activeTask.mode === 'full') ? activeTask : null}
        onClose={() => setCollectDialogOpen(false)}
        onStart={startCollection}
      />
    </div>
  )
}

function collectionReasonLabel(reason = '') {
  if (reason.includes('daily_detail_page_limit')) return '今日详情页次数已用完'
  if (reason.includes('daily_platform_page_limit')) return '今日页面访问次数已用完'
  if (reason.includes('daily_search_page_limit')) return '今日搜索页次数已用完'
  return ''
}

function CollectionProgressPanel({ progress }: { progress: CollectionProgress }) {
  const outcome = ({ running: '采集中', scoring: '正在评分', completed: '已完成', completed_with_shortage: '采集已结束，数量不足', completed_with_errors: '采集结束，有异常', failed: '采集失败', stopped: '已停止', cancelled: '已取消' } as Record<string, string>)[progress.outcome || ''] || '状态待确认'
  const platforms = Object.entries(progress.platforms || {})
  return (
    <details className="group mt-2 rounded-xl border border-card-border bg-card px-3 py-2 text-xs">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 [&::-webkit-details-marker]:hidden">
        <span className="flex items-center gap-1.5 font-semibold"><ChevronDown className="h-3.5 w-3.5 text-muted transition-transform group-open:rotate-180" />采集详情</span>
        <span className="text-muted">{outcome}</span>
        {platforms.map(([platform, state]) => (
          <span key={platform} className="text-muted">{PLATFORM_LABELS[platform] || platform} · 新增 {state.new || 0} 条
            {collectionReasonLabel(`${state.reason_code || ''} ${state.message || ''}`) && <span className="ml-2 text-warning">{collectionReasonLabel(`${state.reason_code || ''} ${state.message || ''}`)}</span>}
          </span>
        ))}
      </summary>
      <div className="mt-2 divide-y divide-card-border border-t border-card-border">
        {platforms.map(([platform, state]) => (
          <div key={platform} className="py-2 leading-5">
            <div className="flex flex-wrap justify-between gap-x-3">
              <span className="font-medium">{PLATFORM_LABELS[platform] || platform}</span>
              <span className="text-muted">{state.status === 'queued' ? '等待前序平台完成' : `${state.city || '城市未开始'} · ${state.keyword || '关键词未开始'} · 第 ${state.page || 0}/${state.max_pages || 0} 页`}</span>
            </div>
            <p className="text-muted">扫描 {state.seen || 0} · 新增 {state.new} · 重复 {state.duplicate || 0} · 过滤 {state.filtered || 0} · 解析失败 {state.parse_failed || 0} · 保存失败 {state.save_failed || 0}</p>
            {Number(state.filtered) > 0 && (
              <details className="mt-2 rounded-lg border border-card-border bg-secondary/40 p-2" open>
                <summary className="cursor-pointer font-medium">过滤记录 · {state.filtered} 条</summary>
                <p className="mt-1 text-xs text-muted">每个平台保留本次采集最近 200 条记录；过滤岗位未进入岗位池。</p>
                {state.filter_records?.length ? (
                  <>
                    <p className="text-xs text-muted">已保留 {state.filter_records.length} 条{state.filter_records.length < Number(state.filtered) ? '，其余记录未保留或来自旧版采集' : ''}</p>
                    <ol tabIndex={0} aria-label={`${PLATFORM_LABELS[platform] || platform}过滤记录`} className="mt-2 max-h-60 space-y-2 overflow-y-auto overscroll-contain pr-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
                      {state.filter_records.map((record, index) => (
                        <li key={`${record.source_job_id}-${index}`} className="rounded-md bg-card p-2">
                          <p className="break-words font-medium">{record.title || '职位名称未提供'} · {record.company || '公司名称未提供'}</p>
                          <p className="break-words text-muted">{record.reason}</p>
                        </li>
                      ))}
                    </ol>
                  </>
                ) : <p className="mt-2 text-muted">本次采集未保存过滤明细，旧记录无法补回；新版本启动的采集会记录原因。</p>}
              </details>
            )}
            {(Number(state.parse_failed || 0) + Number(state.save_failed || 0)) > 0 && (
              <details open className="mt-2 rounded-lg border border-danger-border bg-danger-soft p-2">
                <summary className="cursor-pointer font-medium text-danger">采集异常记录 · {(state.parse_failed || 0) + (state.save_failed || 0)} 次</summary>
                <p className="mt-1 text-xs text-muted">每个平台保留本次采集最近 200 条异常；已保留 {state.error_records?.length || 0} 条。</p>
                {state.error_records?.length ? (
                  <ol tabIndex={0} aria-label={`${PLATFORM_LABELS[platform] || platform}采集异常记录`} className="mt-2 max-h-60 space-y-2 overflow-y-auto overscroll-contain pr-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
                    {state.error_records.map((record, index) => (
                      <li key={index} className="rounded-md bg-card p-2">
                        <p className="font-medium text-danger">{record.kind === 'save' ? '保存失败' : '解析失败'}</p>
                        {record.title && <p className="break-words">{record.title}{record.company ? ` · ${record.company}` : ''}</p>}
                        <p className="break-words text-muted">{[record.city, record.keyword, record.page ? `第 ${record.page} 页` : ''].filter(Boolean).join(' · ') || '未取得页面上下文'}</p>
                        <p className="break-words">{record.reason}</p>
                      </li>
                    ))}
                  </ol>
                ) : <p className="mt-2 text-muted">本次采集未保存异常明细，旧记录无法补回。</p>}
              </details>
            )}
            {(state.message || state.reason_code) && !state.filter_records?.some(record => record.reason === state.message) && !state.error_records?.some(record => record.reason === state.message) && <p className="mt-1 break-words text-muted">{state.message?.replace(/；本轮新增 \d+ 条，读取 \d+ 条，重复 \d+ 条，过滤 \d+ 条，解析失败 \d+ 条，保存失败 \d+ 条$/, '') || state.reason_code}</p>}
          </div>
        ))}
      </div>
    </details>
  )
}

function JobActionCard({ job, selected, onToggle, onDetail, onReject }: { job: Job; selected: boolean; onToggle: () => void; onDetail: () => void; onReject: () => void }) {
  return (
    <div className={`rounded-2xl border p-4 ${selected ? 'border-primary bg-surface' : 'border-card-border bg-surface'}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2 font-black"><span>{job.company}｜{job.title}</span><OutsourcingBadge job={job} /></div>
          <div className="mt-1 text-xs text-muted">{jobSubtitle(job)}</div>
        </div>
        <input type="checkbox" checked={selected} onChange={onToggle} className="mt-1 h-4 w-4 accent-primary" />
      </div>
      <p className="mt-3 line-clamp-2 text-sm leading-6 text-muted">{job.score_reason || '等待继续推进。'}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" onClick={onDetail}><Eye className="mr-2 h-4 w-4" />查看详情</Button>
        <Button variant="secondary" size="sm" disabled={!job.url} onClick={() => window.open(job.url, '_blank', 'noopener,noreferrer')}><ExternalLink className="mr-2 h-4 w-4" />跳转岗位链接</Button>
        <Button variant="secondary" size="sm" onClick={onReject}><XCircle className="mr-2 h-4 w-4" />放弃岗位</Button>
      </div>
    </div>
  )
}

function JobDetailModal({ job, onClose, onChanged }: { job: Job; onClose: () => void; onChanged?: () => void }) {
  const [resumePath, setResumePath] = useState(job.resume_path || '')
  const [generating, setGenerating] = useState(false)
  const [notice, setNotice] = useState('')

  const generateResume = async () => {
    setGenerating(true)
    setNotice('正在根据岗位 JD 生成定制简历，可能需要一两分钟...')
    try {
      const res = await fetch(`/api/jobs/${job.id}/tailored-resume`, { method: 'POST' })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.error || '定制简历生成失败')
      setResumePath(data.resume_path || '')
      setNotice('定制简历已生成，可以下载后手动发给 HR。')
      onChanged?.()
    } catch (err) {
      setNotice(err instanceof Error ? err.message : '定制简历生成失败')
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-6">
      <div className="max-h-[86vh] w-full max-w-3xl overflow-y-auto rounded-3xl border border-card-border bg-card p-6 shadow-2xl">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="text-xs font-black tracking-[0.18em] text-primary">岗位详情</div>
            <h3 className="mt-1 flex flex-wrap items-center gap-2 text-2xl font-black"><span>{job.company}｜{job.title}</span><OutsourcingBadge job={job} /></h3>
            <p className="mt-1 text-sm text-muted">{job.salary || '薪资未填'} · {job.city || '城市未填'} · {getStatusLabel(job.status)}</p>
          </div>
          <Button variant="secondary" size="sm" onClick={onClose}>关闭</Button>
        </div>
        <div className="grid gap-3 text-sm lg:grid-cols-2">
          <InfoBlock label="HR" value={[job.hr_name, job.hr_title].filter(Boolean).join(' · ') || '-'} />
          <InfoBlock label="招聘者活跃" value={job.hr_active || '活跃度未知'} />
          <InfoBlock label="公司" value={[job.company_size, job.company_industry].filter(Boolean).join(' · ') || '-'} />
          <InfoBlock label="来源平台" value="BOSS 直聘" />
          <InfoBlock label="匹配分" value={String(job.score || '-')} />
          <InfoBlock label="定制简历" value={resumePath ? '已生成' : '未生成'} />
        </div>
        <div className="mt-4 rounded-2xl border border-card-border bg-surface p-4">
          <div className="text-sm font-black">评分理由</div>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{job.score_reason || '-'}</p>
        </div>
        <div className="mt-4 rounded-2xl border border-card-border bg-surface p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm font-black">定制简历</div>
            <div className="flex gap-2">
              <Button size="sm" disabled={generating} onClick={generateResume}>
                {generating ? '生成中...' : resumePath ? 'AI 重新生成' : 'AI 生成定制简历'}
              </Button>
              <Button size="sm" variant="secondary" disabled={!resumePath} onClick={() => window.open(`/api/jobs/${job.id}/resume/download`, '_blank')}>
                <Download className="mr-1.5 h-3.5 w-3.5" />下载
              </Button>
            </div>
          </div>
          <p className="mt-2 text-xs text-muted">根据这个岗位的 JD 改写你的简历，不会编造经历；需要时下载后在 BOSS 直聘手动发送。</p>
          {notice && <div className="mt-2 text-xs font-bold text-primary">{notice}</div>}
        </div>
        <div className="mt-4 rounded-2xl border border-card-border bg-surface p-4">
          <div className="text-sm font-black">JD</div>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-muted">{job.jd || '-'}</p>
        </div>
      </div>
    </div>
  )
}

function InfoBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-card-border bg-surface p-4">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 font-bold text-foreground">{value}</div>
    </div>
  )
}

function JobsPoolView({ updateJobStatus }: { updateJobStatus: (jobId: string, status: string) => Promise<void> }) {
  const pageSize = 15
  const [page, setPage] = useState(0)
  const [filters, setFilters] = useState<JobFilters>({ ...EMPTY_JOB_FILTERS })
  const [selectedIds, setSelectedIds] = useState<string[]>([])
  const [notice, setNotice] = useState('')
  const [blockedCompanies, setBlockedCompanies] = useState<string[]>([])
  const [blockingJobId, setBlockingJobId] = useState<string | null>(null)
  const blockingCompanyRef = useRef(false)
  const [showRecycleBin, setShowRecycleBin] = useState(false)
  const [showScoreDialog, setShowScoreDialog] = useState(false)
  const [quickScoring, setQuickScoring] = useState(false)
  const [sortBy, setSortBy] = useState<JobSortKey>('created_at')
  const [sortOrder, setSortOrder] = useState<JobSortOrder>('desc')
  const [recycleJobs, setRecycleJobs] = useState<Job[]>([])
  const [recycleSelectedIds, setRecycleSelectedIds] = useState<string[]>([])
  const [recycleLoading, setRecycleLoading] = useState(false)
  const [permanentDeleteIds, setPermanentDeleteIds] = useState<string[]>([])
  const [permanentDeleteAcknowledged, setPermanentDeleteAcknowledged] = useState(false)
  const { items, total, allTotal, cityOptions, loading, error, refresh: refreshJobs } = useJobSearch(filters, page, pageSize, sortBy, sortOrder)

  useEffect(() => {
    const controller = new AbortController()
    const loadBlockedCompanies = () => {
      fetch('/api/agent/state', { cache: 'no-store', signal: controller.signal })
        .then(async res => {
          if (!res.ok) return
          const data = await res.json()
          if (!controller.signal.aborted && Array.isArray(data.preferences?.blocked_companies)) {
            setBlockedCompanies(data.preferences.blocked_companies)
          }
        })
        .catch(() => { /* 点击屏蔽时会重新读取，并显示读取错误。 */ })
    }
    loadBlockedCompanies()
    const handleConfigSaved = () => {
      loadBlockedCompanies()
      setSelectedIds([])
      setPage(0)
      refreshJobs()
    }
    window.addEventListener('bosshunter-config-saved', handleConfigSaved)
    return () => {
      controller.abort()
      window.removeEventListener('bosshunter-config-saved', handleConfigSaved)
    }
  }, [refreshJobs])

  useEffect(() => {
    setPage(0)
  }, [filters.query, filters.minScore, filters.salaryMin, filters.salaryMax, filters.status, filters.createdWithin, filters.sourcePlatform, filters.city, filters.education, filters.recruitmentType])

  const toggleSelected = (jobId: string) => {
    setSelectedIds(previous => previous.includes(jobId) ? previous.filter(id => id !== jobId) : [...previous, jobId])
  }

  const allPageSelected = items.length > 0 && items.every(job => selectedIds.includes(job.id))
  const toggleCurrentPage = () => {
    const pageIds = new Set(items.map(job => job.id))
    setSelectedIds(previous => allPageSelected
      ? previous.filter(id => !pageIds.has(id))
      : [...new Set([...previous, ...pageIds])])
  }

  const changeSort = (nextSortBy: JobSortKey) => {
    setPage(0)
    if (nextSortBy === sortBy) {
      setSortOrder(previous => previous === 'asc' ? 'desc' : 'asc')
      return
    }
    setSortBy(nextSortBy)
    setSortOrder(nextSortBy === 'score' || nextSortBy === 'created_at' ? 'desc' : 'asc')
  }

  const loadRecycleBin = async () => {
    setRecycleLoading(true)
    try {
      const collected: Job[] = []
      let offset = 0
      const limit = 200
      while (true) {
        const res = await fetch(`/api/jobs?deleted=only&limit=${limit}&offset=${offset}`, { cache: 'no-store' })
        if (!res.ok) throw new Error(`回收站接口返回 ${res.status}`)
        const pageItems = await res.json()
        if (!Array.isArray(pageItems)) throw new Error('回收站响应格式无效')
        collected.push(...pageItems)
        const totalCount = Number(res.headers.get('X-Total-Count'))
        if (!pageItems.length || pageItems.length < limit || (Number.isFinite(totalCount) && collected.length >= totalCount)) break
        offset += pageItems.length
      }
      const unique = new Map(collected.map(job => [String(job.id), job]))
      setRecycleJobs([...unique.values()])
      setRecycleSelectedIds(previous => previous.filter(id => unique.has(id)))
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '读取回收站失败')
    } finally {
      setRecycleLoading(false)
    }
  }

  useEffect(() => {
    void loadRecycleBin()
  }, [])

  const postJobAction = async (path: string, payload: Record<string, unknown>) => {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    if (!res.ok) {
      const data = await res.json().catch(() => ({}))
      const blocked = Array.isArray(data.blocked)
        ? data.blocked.map((item: { job_id?: string; reasons?: string[] }) => `${item.job_id || '岗位'}：${(item.reasons || []).join('、')}`).join('；')
        : ''
      throw new Error([data.error || '岗位操作失败', blocked].filter(Boolean).join('；'))
    }
    return res.json()
  }

  const softDelete = async (jobIds: string[]) => {
    if (!jobIds.length || !window.confirm(`确认将 ${jobIds.length} 个岗位移入回收站吗？岗位不会永久删除。`)) return
    try {
      const result = await postJobAction('/api/jobs/soft-delete', { job_ids: jobIds, confirmed: true })
      setSelectedIds(previous => previous.filter(id => !jobIds.includes(id)))
      refreshJobs()
      await loadRecycleBin()
      setNotice(`已移入回收站 ${result.affected_count || 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '移入回收站失败')
    }
  }

  const blockCompany = async (job: Job) => {
    const company = (job.company || '').trim()
    if (!company || blockingCompanyRef.current) return
    blockingCompanyRef.current = true
    setBlockingJobId(job.id)
    setNotice('正在读取屏蔽名单并准备配置预览…')
    try {
      const readRules = async (): Promise<string[]> => {
        const res = await fetch('/api/agent/state', { cache: 'no-store' })
        const data = await res.json()
        const rules = data.preferences?.blocked_companies
        if (!res.ok || !Array.isArray(rules) || !rules.every(rule => typeof rule === 'string')) {
          throw new Error(data.error || '读取屏蔽名单失败，请重试。')
        }
        return rules
      }
      const current = await readRules()
      setBlockedCompanies(current)
      const existing = current.find(rule => rule.trim() && company.toLowerCase().includes(rule.trim().toLowerCase()))
      if (existing) {
        setSelectedIds([])
        setPage(0)
        refreshJobs()
        setNotice(`“${company}”已被“${existing}”屏蔽。可在配置 → 个人信息 → 屏蔽公司中解除。`)
        return
      }
      const preferences = { blocked_companies: [...current, company] }
      await postJobAction('/api/agent/config/preview', { preferences })
      if (!window.confirm(`配置预览\n新增屏蔽公司：${company}\n已有屏蔽规则：${current.join('、') || '无'}\n\n保存后，公司名包含“${company}”的岗位将从列表隐藏，并跳过后续采集和评分。岗位记录和投递状态保留。\n可在“配置 → 个人信息 → 屏蔽公司”中解除，解除后恢复显示。\n\n确认保存？`)) {
        setNotice('已取消屏蔽，配置未修改。')
        return
      }
      // 确认期间可能在其他页面修改了名单，避免用旧预览覆盖新规则。
      const latest = await readRules()
      if (JSON.stringify(latest) !== JSON.stringify(current)) {
        setBlockedCompanies(latest)
        throw new Error('屏蔽名单已变化，请重新点击“屏蔽公司”查看最新预览。')
      }
      const result = await postJobAction('/api/agent/config/apply', { preferences, confirm: true })
      setBlockedCompanies(result.preferences.blocked_companies)
      window.dispatchEvent(new Event('bosshunter-config-saved'))
      setNotice(`已屏蔽“${company}”，匹配岗位已从列表隐藏，记录保留；解除屏蔽后恢复显示。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '屏蔽公司失败，请重试。')
    } finally {
      blockingCompanyRef.current = false
      setBlockingJobId(null)
    }
  }

  const markManuallySent = async (job: Job) => {
    if (!window.confirm('请确认：你已经在 BOSS 直聘完成了这个岗位的投递。此操作只更新本地记录，不会向平台发送任何内容。')) return
    try {
      const result = await postJobAction('/api/jobs/manual-sent', {
        job_ids: [job.id],
        confirmed: true,
      })
      refreshJobs()
      setNotice(
        result.affected_count
          ? '已将岗位标记为“已投递”。'
          : '该岗位此前已经标记为“已投递”。'
      )
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '标记已发送失败')
    }
  }

  const changeJobStatus = async (job: Job, status: string) => {
    if (!window.confirm(`确认将“${job.company} ${job.title}”状态改为“${getStatusLabel(status)}”吗？`)) return
    try {
      await updateJobStatus(job.id, status)
      refreshJobs()
      setNotice('岗位状态已更新。')
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '修改岗位状态失败')
    }
  }

  const restoreJobs = async (jobIds: string[]) => {
    if (!jobIds.length || !window.confirm(`确认恢复 ${jobIds.length} 个岗位吗？恢复后不会自动评分或投递。`)) return
    try {
      const result = await postJobAction('/api/jobs/restore', { job_ids: jobIds, confirmed: true })
      setRecycleSelectedIds(previous => previous.filter(id => !jobIds.includes(id)))
      refreshJobs()
      await loadRecycleBin()
      setNotice(`已恢复 ${result.affected_count || 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '恢复失败')
    }
  }

  const requestPermanentDelete = (jobIds: string[]) => {
    if (!jobIds.length) return
    setPermanentDeleteIds(jobIds)
    setPermanentDeleteAcknowledged(false)
  }

  const confirmPermanentDelete = async () => {
    if (!permanentDeleteIds.length || !permanentDeleteAcknowledged) return
    try {
      const result = await postJobAction('/api/jobs/permanent-delete', {
        job_ids: permanentDeleteIds,
        confirmed: true,
        confirmation: 'PERMANENT_DELETE',
      })
      setRecycleSelectedIds(previous => previous.filter(id => !permanentDeleteIds.includes(id)))
      setPermanentDeleteIds([])
      setPermanentDeleteAcknowledged(false)
      await loadRecycleBin()
      setNotice(`已永久删除 ${result.affected_count || 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '永久删除失败')
    }
  }

  const exportJobs = async (format: 'xlsx' | 'csv', scope: 'all' | 'filtered' | 'selected') => {
    try {
      const res = await fetch('/api/jobs/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          format,
          scope,
          job_ids: scope === 'selected' ? selectedIds : [],
          filters: scope === 'filtered' ? {
            q: filters.query.trim(),
            min_score: filters.minScore,
            salary_min: filters.salaryMin,
            salary_max: filters.salaryMax,
            status: filters.status,
            created_within: filters.createdWithin,
            source_platform: filters.sourcePlatform,
            city: filters.city,
            education: filters.education,
            recruitment_type: filters.recruitmentType,
          } : {},
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.error || '导出失败')
      }
      const blob = await res.blob()
      const disposition = res.headers.get('Content-Disposition') || ''
      const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || `bosshunter-jobs.${format}`
      const url = window.URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      window.URL.revokeObjectURL(url)
      const exportedCount = Number(res.headers.get('X-Exported-Count'))
      setNotice(`已导出 ${Number.isFinite(exportedCount) ? exportedCount : 0} 条岗位。`)
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '导出失败')
    }
  }

  const startScoring = async (options: {
    scope: 'pending' | 'failed' | 'selected' | 'all_scored'
    limit: number | null
    job_ids: string[]
    force_rescore: boolean
    force?: boolean
  }): Promise<{ ok: boolean; error?: string }> => {
    const res = await fetch('/api/scoring/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ options, force: options.force ?? false }),
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      if (res.status === 409 && data.code === 'scoring_run_paused' && !options.force) {
        const confirmed = window.confirm(
          `已有等待恢复的评分任务：${data.error || ''}\n是否结束该任务并强制开始新评分任务？（已完成的评分结果会保留）`,
        )
        if (confirmed) {
          return await startScoring({ ...options, force: true })
        }
      }
      const checks = Array.isArray(data.messages) ? data.messages.join('；') : ''
      return { ok: false, error: [data.error || '启动评分失败', checks].filter(Boolean).join('：') }
    }
    setNotice(`独立评分已启动，共 ${data.run?.remaining_job_ids?.length || 0} 个岗位。`)
    return { ok: true }
  }

  const startQuickScoring = async () => {
    if (!window.confirm('将对岗位池中所有未评分或评分失败的岗位启动 AI 评分，可能产生模型费用，是否继续？')) return
    setQuickScoring(true)
    try {
      const result = await startScoring({ scope: 'pending', limit: null, job_ids: [], force_rescore: false })
      if (!result.ok) setNotice(result.error || '启动 AI 评分失败')
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : '启动 AI 评分失败')
    } finally {
      setQuickScoring(false)
    }
  }

  if (showRecycleBin) {
    return (
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={() => setShowRecycleBin(false)}>返回岗位池</Button>
          <Button variant="secondary" size="sm" onClick={() => void loadRecycleBin()} disabled={recycleLoading}>刷新回收站</Button>
        </div>
        {notice && <div className="rounded-xl bg-secondary px-4 py-3 text-sm text-primary">{notice}</div>}
        <RecycleBinPanel
          jobs={recycleJobs}
          selectedIds={recycleSelectedIds}
          loading={recycleLoading}
          onToggleSelected={id => setRecycleSelectedIds(previous => previous.includes(id) ? previous.filter(item => item !== id) : [...previous, id])}
          onSelectAll={setRecycleSelectedIds}
          onRestore={ids => void restoreJobs(ids)}
          onPermanentDelete={requestPermanentDelete}
        />
        {permanentDeleteIds.length > 0 && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4" role="dialog" aria-modal="true">
            <div className="w-full max-w-lg rounded-3xl border border-danger-border bg-card p-6 shadow-2xl">
              <div className="flex items-start gap-3"><AlertTriangle className="mt-0.5 h-6 w-6 shrink-0 text-danger" /><div><h3 className="text-xl font-black">确认永久删除</h3><p className="mt-2 text-sm leading-6 text-muted">将永久删除 {permanentDeleteIds.length} 条岗位及其历史，无法恢复。存在发送或回复证据的岗位会被后端拒绝删除。</p></div></div>
              <label className="mt-5 flex cursor-pointer items-start gap-3 rounded-2xl border border-danger-border bg-danger-soft p-3 text-sm font-bold"><input type="checkbox" checked={permanentDeleteAcknowledged} onChange={event => setPermanentDeleteAcknowledged(event.target.checked)} className="mt-0.5 h-4 w-4 accent-danger" /><span>我确认永久删除，并了解此操作无法撤销。</span></label>
              <div className="mt-6 flex justify-end gap-3"><Button variant="secondary" size="sm" onClick={() => setPermanentDeleteIds([])}>取消</Button><Button variant="destructive" size="sm" disabled={!permanentDeleteAcknowledged} onClick={() => void confirmPermanentDelete()}>永久删除</Button></div>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-3xl border border-card-border bg-card p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-black">岗位池</h2>
          <p className="mt-1 text-sm text-muted">集中查看已采集岗位、AI 分数、状态和详情入口。</p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={() => { setShowRecycleBin(true); void loadRecycleBin() }}><Trash2 className="mr-1 h-4 w-4" />回收站 ({recycleJobs.length})</Button>
          <BriefcaseBusiness className="h-6 w-6 text-primary" />
        </div>
      </div>
      <JobFilterBar
        filters={filters}
        onChange={setFilters}
        onReset={() => setFilters({ ...EMPTY_JOB_FILTERS })}
        resultCount={total}
        totalCount={allTotal}
        invalidSalary={hasInvalidSalaryRange(filters)}
        showStatus
        cityOptions={cityOptions}
      />
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs">
        <Button variant="secondary" size="sm" disabled={!items.length} onClick={toggleCurrentPage}>
          {allPageSelected ? '取消选择本页' : '选择本页'}
        </Button>
        <span className="rounded-full bg-secondary px-3 py-2 font-bold text-primary">已选择 {selectedIds.length} 条</span>
        {selectedIds.length > 0 && <Button variant="ghost" size="sm" onClick={() => setSelectedIds([])}>清空选择</Button>}
        <Button variant="destructive" size="sm" disabled={!selectedIds.length} onClick={() => void softDelete(selectedIds)}>移入回收站</Button>
        <Button size="sm" onClick={() => void startQuickScoring()} disabled={quickScoring || !total}>
          {quickScoring ? '启动评分中…' : '一键 AI 评分'}
        </Button>
        <Button variant="secondary" size="sm" onClick={() => setShowScoreDialog(true)}>评分选项</Button>
        <ExportMenu onExport={exportJobs} hasSelection={selectedIds.length > 0} hasFiltered={total > 0} />
      </div>
      {notice && <div className="mb-4 rounded-xl bg-secondary px-4 py-3 text-sm text-primary">{notice}</div>}
      {error && <div className="mb-4 rounded-xl border border-danger-border bg-danger-soft px-4 py-3 text-sm text-danger">{error}</div>}
      <JobsTable
        jobs={items}
        page={page}
        pageSize={pageSize}
        total={total}
        onPageChange={setPage}
        selectedIds={selectedIds}
        onToggleSelected={toggleSelected}
        onSoftDelete={job => void softDelete([job.id])}
        onMarkManuallySent={job => void markManuallySent(job)}
        onStatusChange={changeJobStatus}
        onBlockCompany={job => void blockCompany(job)}
        blockedCompanies={blockedCompanies}
        blockingJobId={blockingJobId}
        loading={loading}
        sortBy={sortBy}
        sortOrder={sortOrder}
        onSortChange={changeSort}
      />
      <ScoreJobsDialog
        open={showScoreDialog}
        selectedJobIds={selectedIds}
        onClose={() => setShowScoreDialog(false)}
        onStart={startScoring}
      />
    </div>
  )
}

function ExportMenu({
  onExport,
  hasSelection,
  hasFiltered,
}: {
  onExport: (format: 'xlsx' | 'csv', scope: 'all' | 'filtered' | 'selected') => void
  hasSelection: boolean
  hasFiltered: boolean
}) {
  const [format, setFormat] = useState<'xlsx' | 'csv'>('xlsx')
  return (
    <div className="ml-auto flex flex-wrap items-center gap-2">
      <select
        value={format}
        onChange={event => setFormat(event.target.value as 'xlsx' | 'csv')}
        className="rounded-xl border border-card-border bg-card px-2 py-2 text-xs outline-none focus:border-primary"
      >
        <option value="xlsx">XLSX</option>
        <option value="csv">CSV</option>
      </select>
      <Button variant="secondary" size="sm" disabled={!hasFiltered} onClick={() => onExport(format, 'filtered')}>导出筛选结果</Button>
      <Button variant="secondary" size="sm" disabled={!hasSelection} onClick={() => onExport(format, 'selected')}>导出所选岗位</Button>
      <Button variant="secondary" size="sm" onClick={() => onExport(format, 'all')}>导出全部岗位</Button>
    </div>
  )
}
