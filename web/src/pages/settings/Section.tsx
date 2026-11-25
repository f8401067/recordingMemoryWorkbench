import React from 'react';
import type { LucideIcon } from 'lucide-react';

/** 设置页里的一块卡片：图标 + 标题 + 说明 + 右上角操作 */
export default function Section({
  title,
  desc,
  icon: Icon,
  children,
  action,
}: {
  title: string;
  desc?: React.ReactNode;
  icon: LucideIcon;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="card p-4 md:p-5">
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-2.5">
          <div className="h-8 w-8 rounded-lg bg-panel2 grid place-items-center shrink-0">
            <Icon size={15} className="text-muted" />
          </div>
          <div>
            <h2 className="text-[14px] font-semibold">{title}</h2>
            {desc && <p className="text-[11.5px] text-muted mt-0.5 leading-relaxed">{desc}</p>}
          </div>
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

/** 开关：设置页里用到很多次 */
export function Switch({
  checked,
  onChange,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 rounded-full transition-colors shrink-0 disabled:opacity-40 ${
        checked ? 'bg-brand' : 'bg-line'
      }`}
    >
      <span
        className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
          checked ? 'left-[22px]' : 'left-0.5'
        }`}
      />
    </button>
  );
}
