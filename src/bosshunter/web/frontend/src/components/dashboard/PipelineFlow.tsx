import { Search, Bot, CheckCircle, FileText } from 'lucide-react'

const steps = [
  { icon: Search, label: '采集', desc: '搜索岗位' },
  { icon: Bot, label: 'AI评分', desc: '匹配打分' },
  { icon: CheckCircle, label: '挑选', desc: '你来决定' },
  { icon: FileText, label: '定制简历', desc: '按 JD 生成' },
]

export function PipelineFlow() {
  return (
    <div className="rounded-2xl border border-card-border bg-surface p-6">
      <h3 className="text-sm font-black text-foreground mb-4">猪猪求职助手工作流程</h3>
      <div className="grid grid-cols-2 gap-x-3 gap-y-5 lg:grid-cols-4">
        {steps.map((step, i) => (
          <div key={step.label} className="relative min-w-0">
            <div className="flex flex-col items-center">
              <div className="w-12 h-12 rounded-xl bg-card border border-card-border flex items-center justify-center mb-2 hover:border-primary/50 hover:shadow-md transition-all shadow-sm">
                <step.icon className="w-5 h-5 text-primary" />
              </div>
              <span className="text-xs font-black text-foreground">{step.label}</span>
              <span className="text-[10px] text-muted mt-0.5">{step.desc}</span>
            </div>
            {i < steps.length - 1 && (
              <div className="absolute left-[calc(50%+28px)] right-[calc(-50%+16px)] top-6 hidden h-px bg-card-border lg:block" />
            )}
          </div>
        ))}
      </div>
      <p className="text-xs text-muted mt-4">
        可从上方选择「采集并评分」或「单独采集」；投递和沟通由你在 BOSS 直聘完成。
      </p>
    </div>
  )
}
