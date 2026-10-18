import { NavLink } from 'react-router-dom'
import { BriefcaseBusiness, LayoutDashboard, Settings } from 'lucide-react'

const navItems = [
  { to: '/', icon: LayoutDashboard, label: '工作台' },
  { to: '/jobs', icon: BriefcaseBusiness, label: '岗位池' },
  { to: '/config', icon: Settings, label: '配置' },
]

export function Sidebar() {
  return (
    <aside className="flex w-16 shrink-0 flex-col border-r border-card-border bg-card md:w-60">
      <div className="flex h-16 shrink-0 items-center justify-center border-b border-card-border md:justify-start md:px-5">
        <div className="flex items-center gap-3">
          <img src="/logo.webp" alt="猪猪求职助手" className="h-10 w-10" />
          <div className="hidden md:block">
            <div className="font-black text-sm tracking-tight text-foreground">猪猪求职助手</div>
            <div className="text-[11px] text-muted">v0.1.0 · 本地 AI 岗位筛选</div>
          </div>
        </div>
      </div>

      <nav className="flex-1 space-y-1 px-1.5 py-4 md:px-3">
        {navItems.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            data-tour={item.to === '/config' ? 'nav-config' : item.to === '/' ? 'nav-workbench' : undefined}
            className={({ isActive }) =>
              `relative flex items-center justify-center gap-3 rounded-xl px-1 py-3 text-[10px] transition-colors md:justify-between md:px-3 md:text-sm ${
                isActive
                  ? 'bg-secondary text-primary font-black'
                  : 'text-muted hover:text-foreground hover:bg-surface'
              }`
            }
          >
            <span className="flex flex-col items-center gap-1 md:flex-row md:gap-3">
              <item.icon className="w-4 h-4" />
              {item.label}
            </span>
          </NavLink>
        ))}
      </nav>

      <div className="hidden border-t border-card-border px-4 py-4 md:block">
        <img src="/mascot.webp" alt="" className="mx-auto w-28" />
        <p className="mt-2 text-center text-[11px] leading-5 text-muted">挑岗位交给我，投递你说了算</p>
      </div>
    </aside>
  )
}
