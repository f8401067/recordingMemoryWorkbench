import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowUpRight, Check, CircleDashed, Inbox, Loader2, Plus, RefreshCw, Sparkles,
  UserRound, CalendarClock,
} from 'lucide-react';
import {
  api, acceptAction, setActionStatus,
  type ActionsPayload, type ActionItem, type ActionSuggestion,
} from '../lib/api';
import { useToast } from '../lib/toast';
import { cn, friendlyDay, todayStr, weekdayCn } from '../lib/utils';

const TAG_STYLE: Record<string, { badge: string; dot: string }> = {
  工作: { badge: 'bg-sky-500/10 text-sky-600 dark:text-sky-400', dot: 'bg-sky-500' },
  生活: { badge: 'bg-amber-500/10 text-amber-600 dark:text-amber-400', dot: 'bg-amber-500' },
  学习: { badge: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400', dot: 'bg-emerald-500' },
  灵感: { badge: 'bg-fuchsia-500/10 text-fuchsia-600 dark:text-fuchsia-400', dot: 'bg-fuchsia-500' },
  未分类: { badge: 'bg-panel2 text-muted', dot: 'bg-muted/45' },
};

const tagStyle = (tag: string) => TAG_STYLE[tag] ?? TAG_STYLE['未分类'];

/** 未完成的排前面，同组内保持接口顺序（新的在上） */
function groupByDate(items: ActionItem[]) {
  const map = new Map<string, ActionItem[]>();
  for (const item of items) {
    const arr = map.get(item.date) ?? [];
    arr.push(item);
    map.set(item.date, arr);
  }
  for (const arr of map.values()) {
    arr.sort((a, b) => Number(a.status === 'done') - Number(b.status === 'done'));
  }
  return [...map.entries()].sort((a, b) => b[0].localeCompare(a[0]));
}

type Tab = 'suggest' | 'todo';

export default function ActionsPage() {
  const toast = useToast();
  const [payload, setPayload] = useState<ActionsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [tag, setTag] = useState('全部');
  const [tab, setTab] = useState<Tab>('suggest');
  const [accepting, setAccepting] = useState<string | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);
  // 用户手动切过 Tab 之后，就不再用「有没有建议」自动纠正
  const tabTouched = useRef(false);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const res = await api<ActionsPayload>('/api/actions');
      setPayload(res);
      if (!tabTouched.current) setTab(res.suggestions.length > 0 ? 'suggest' : 'todo');
    } catch (e: any) {
      if (!silent) toast.push(e?.message ?? '加载失败', 'error');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const counts = payload?.counts ?? {};
  const suggestions = payload?.suggestions ?? [];
  const allItems = payload?.items ?? [];
  const todoCount = counts['全部'] ?? 0;
  const pendingCount = allItems.filter((x) => x.status !== 'done').length;

  const filters = useMemo(() => {
    const list = ['全部', ...(payload?.tags ?? [])];
    if ((counts['未分类'] ?? 0) > 0) list.push('未分类');
    return list;
  }, [payload, counts]);

  const items = useMemo(
    () => allItems.filter((x) => tag === '全部' || x.tag === tag),
    [allItems, tag],
  );

  const groups = useMemo(() => groupByDate(items), [items]);
  const doneCount = items.filter((x) => x.status === 'done').length;

  /** 点 + ：候选 → 正式待办 */
  const accept = async (s: ActionSuggestion) => {
    if (accepting) return;
    setAccepting(s.originKey);
    // 先把它从「猜你想做」里拿掉，点下去就有反应
    setPayload((p) => (p ? { ...p, suggestions: p.suggestions.filter((x) => x.originKey !== s.originKey) } : p));
    try {
      await acceptAction(s);
      await load(true);
    } catch (e: any) {
      toast.push(e?.message ?? '加入待办失败', 'error');
      await load(true);
    } finally {
      setAccepting(null);
    }
  };

  /** 勾选框：切换完成状态 */
  const toggle = async (item: ActionItem) => {
    if (toggling) return;
    const next = item.status === 'done' ? 'pending' : 'done';
    setToggling(item.id);
    setPayload((p) =>
      p
        ? { ...p, items: p.items.map((x) => (x.id === item.id ? { ...x, status: next } : x)) }
        : p,
    );
    try {
      await setActionStatus(item.id, next);
    } catch (e: any) {
      toast.push(e?.message ?? '更新失败', 'error');
      await load(true);
    } finally {
      setToggling(null);
    }
  };

  const switchTab = (next: Tab) => {
    tabTouched.current = true;
    setTab(next);
  };

  const tabs: { key: Tab; label: string; count: number; icon: React.ReactNode }[] = [
    { key: 'suggest', label: '猜你想做', count: suggestions.length, icon: <Sparkles size={13} /> },
    { key: 'todo', label: '待办', count: todoCount, icon: <Check size={13} /> },
  ];

  return (
    <div className="max-w-3xl mx-auto px-4 md:px-8 py-4 md:py-10">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <div className="h-10 w-10 rounded-[14px] bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center shadow-[0_8px_20px_-10px_var(--brand)] shrink-0">
            <Inbox size={19} className="text-white" strokeWidth={2.3} />
          </div>
          <div>
            <h1 className="text-[22px] md:text-[24px] font-semibold tracking-tight leading-tight">行动</h1>
            {/* 手机上省掉这行，顶栏只留标题 */}
            <p className="hidden md:block text-[12.5px] text-muted mt-1">
              从每天的回忆里抽出来的待办，按标签挑出下一步要做的事。
            </p>
          </div>
        </div>
        <button
          onClick={() => void load()}
          disabled={loading}
          className="h-9 w-9 grid place-items-center rounded-xl bg-panel2 text-muted hover:text-ink active:scale-95 transition disabled:opacity-40 shrink-0"
          title="刷新"
        >
          <RefreshCw size={15} className={cn(loading && 'animate-spin')} />
        </button>
      </div>

      {/* Tab 切换 */}
      <div className="mt-5 flex items-center gap-1 rounded-full border border-line bg-panel2 p-1">
        {tabs.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              onClick={() => switchTab(t.key)}
              className={cn(
                'flex-1 h-9 rounded-full inline-flex items-center justify-center gap-1.5 text-[13px] font-medium transition-colors',
                active ? 'bg-panel text-ink shadow-soft' : 'text-muted hover:text-ink',
              )}
            >
              <span className={cn(active ? 'text-brand' : 'text-muted/70')}>{t.icon}</span>
              {t.label}
              <span
                className={cn(
                  'tabular-nums text-[11px] rounded-full px-1.5 py-0.5 leading-none',
                  active ? 'bg-soft text-brand' : 'bg-panel text-muted/70',
                )}
              >
                {t.count}
              </span>
            </button>
          );
        })}
      </div>

      {loading && (
        <div className="py-16 grid place-items-center">
          <Loader2 size={20} className="animate-spin text-muted" />
        </div>
      )}

      {/* ---------- 猜你想做 ---------- */}
      {!loading && tab === 'suggest' && (
        <div className="mt-4">
          {suggestions.length === 0 ? (
            <div className="card p-8 md:p-10 flex flex-col items-center text-center">
              <div className="h-12 w-12 rounded-2xl bg-soft grid place-items-center">
                <Sparkles size={20} className="text-brand" />
              </div>
              <div className="mt-3 text-[14px] font-medium">今天还没有新的建议</div>
              <p className="mt-1.5 text-[12px] text-muted leading-relaxed max-w-[320px]">
                整理完今天的回忆后，AI 会把其中的待办列在这里，点左边的 + 就变成正式待办。
              </p>
            </div>
          ) : (
            <>
              <div className="flex items-center gap-2 mb-2">
                <h2 className="text-[12px] font-medium text-muted">
                  今天
                  <span className="ml-1.5 text-muted/60">{weekdayCn(payload?.today ?? todayStr())}</span>
                </h2>
                <div className="flex-1 h-px bg-line" />
                <span className="text-[11px] text-muted/70 shrink-0 tabular-nums">{suggestions.length} 条</span>
              </div>

              <div className="space-y-2">
                {suggestions.map((s) => {
                  const style = tagStyle(s.tag);
                  const busy = accepting === s.originKey;
                  return (
                    <article
                      key={s.originKey}
                      className="card p-3 md:p-3.5 hover:border-brand/25 transition-colors"
                    >
                      <div className="flex items-start gap-3">
                        <button
                          onClick={() => void accept(s)}
                          disabled={busy}
                          aria-label="加入待办"
                          className={cn(
                            'mt-0.5 h-[18px] w-[18px] shrink-0 rounded-full grid place-items-center transition',
                            'bg-soft text-brand hover:bg-brand hover:text-white active:scale-95 disabled:opacity-50',
                          )}
                        >
                          {busy ? <Loader2 size={11} className="animate-spin" /> : <Plus size={12} strokeWidth={3} />}
                        </button>
                        <div className="min-w-0 flex-1">
                          <div className="flex items-start justify-between gap-3">
                            <p className="text-[14px] leading-relaxed break-words">{s.text}</p>
                            <span className={cn('chip shrink-0 !py-0.5 !text-[10.5px]', style.badge)}>
                              {s.tag}
                            </span>
                          </div>

                          <div className="mt-1.5 flex items-center gap-x-3 gap-y-1 text-[11px] text-muted flex-wrap">
                            {s.summaryId && (
                              <Link
                                to={`/summaries/${s.summaryId}`}
                                className="inline-flex items-center gap-1 hover:text-brand transition-colors min-w-0"
                              >
                                <span className="truncate max-w-[220px]">
                                  {s.source} · {s.summaryTitle}
                                </span>
                                <ArrowUpRight size={11} className="shrink-0" />
                              </Link>
                            )}
                            {s.owner && (
                              <span className="inline-flex items-center gap-1">
                                <UserRound size={11} />
                                {s.owner}
                              </span>
                            )}
                            {s.due && (
                              <span className="inline-flex items-center gap-1">
                                <CalendarClock size={11} />
                                {s.due}
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    </article>
                  );
                })}
              </div>
            </>
          )}
        </div>
      )}

      {/* ---------- 待办 ---------- */}
      {!loading && tab === 'todo' && (
        <div className="mt-4">
          {/* 标签筛选 */}
          <div className="-mx-4 md:-mx-8 px-4 md:px-8 overflow-x-auto no-scrollbar">
            <div className="flex items-center gap-1.5 w-max md:w-auto">
              {filters.map((t) => {
                const active = t === tag;
                return (
                  <button
                    key={t}
                    onClick={() => setTag(t)}
                    className={cn(
                      'h-8 shrink-0 rounded-full border px-3 text-[12.5px] font-medium transition-colors',
                      active
                        ? 'border-brand bg-soft text-brand'
                        : 'border-line bg-panel text-muted hover:border-brand/35 hover:text-ink',
                    )}
                  >
                    {t}
                    <span className={cn('ml-1.5 tabular-nums text-[11px]', active ? 'text-brand/70' : 'text-muted/60')}>
                      {counts[t] ?? 0}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          {(counts['未分类'] ?? 0) > 0 && (
            <p className="mt-2.5 text-[11px] text-muted/75 leading-relaxed">
              有 {counts['未分类']} 条旧待办还没有标签，重新整理对应的回忆后会自动归类。
            </p>
          )}

          {groups.length === 0 && (
            <div className="mt-4 card p-8 md:p-10 flex flex-col items-center text-center">
              <div className="h-12 w-12 rounded-2xl bg-soft grid place-items-center">
                <CircleDashed size={22} className="text-brand" />
              </div>
              <div className="mt-3 text-[14px] font-medium">
                {tag === '全部' ? '还没有待办' : `「${tag}」下还没有待办`}
              </div>
              <p className="mt-1.5 text-[12px] text-muted leading-relaxed max-w-[320px]">
                {tag === '全部'
                  ? suggestions.length > 0
                    ? '去「猜你想做」里点 + ，挑中的会变成这里的待办。'
                    : '打开「回忆」生成一份纪要，AI 会把其中的待办整理到这里。'
                  : '换一个标签看看，或者等下一次整理出新的待办。'}
              </p>
            </div>
          )}

          {groups.length > 0 && (
            <div className="mt-4">
              {groups.map(([date, rows]) => (
                <section key={date} className="mb-5 last:mb-0">
                  <div className="flex items-center gap-2 mb-2">
                    <h2 className="text-[12px] font-medium text-muted">
                      {friendlyDay(date)}
                      <span className="ml-1.5 text-muted/60">{weekdayCn(date)}</span>
                      {date.slice(0, 4) !== todayStr().slice(0, 4) && (
                        <span className="ml-1.5 text-muted/60">{date.slice(0, 4)} 年</span>
                      )}
                    </h2>
                    <div className="flex-1 h-px bg-line" />
                    <span className="text-[11px] text-muted/70 shrink-0 tabular-nums">{rows.length} 项</span>
                  </div>

                  <div className="space-y-2">
                    {rows.map((item) => {
                      const style = tagStyle(item.tag);
                      const done = item.status === 'done';
                      return (
                        <article
                          key={item.id}
                          className={cn(
                            'card p-3 md:p-3.5 transition-colors',
                            done ? 'opacity-60' : 'hover:border-brand/25',
                          )}
                        >
                          <div className="flex items-start gap-3">
                            <button
                              onClick={() => void toggle(item)}
                              aria-label={done ? '标记未完成' : '标记完成'}
                              className={cn(
                                'mt-0.5 h-[18px] w-[18px] shrink-0 rounded-full border grid place-items-center transition',
                                done
                                  ? 'bg-brand border-brand text-white'
                                  : 'border-line bg-panel2 hover:border-brand',
                              )}
                            >
                              {done && <Check size={11} strokeWidth={3.5} />}
                            </button>
                            <div className="min-w-0 flex-1">
                              <div className="flex items-start justify-between gap-3">
                                <p
                                  className={cn(
                                    'text-[14px] leading-relaxed break-words',
                                    done && 'line-through text-muted',
                                  )}
                                >
                                  {item.text}
                                </p>
                                <span className={cn('chip shrink-0 !py-0.5 !text-[10.5px]', style.badge)}>
                                  {item.tag}
                                </span>
                              </div>

                              <div className="mt-1.5 flex items-center gap-x-3 gap-y-1 text-[11px] text-muted flex-wrap">
                                {item.summaryId && (
                                  <Link
                                    to={`/summaries/${item.summaryId}`}
                                    className="inline-flex items-center gap-1 hover:text-brand transition-colors min-w-0"
                                  >
                                    <span className="truncate max-w-[220px]">
                                      {item.source} · {item.summaryTitle}
                                    </span>
                                    <ArrowUpRight size={11} className="shrink-0" />
                                  </Link>
                                )}
                                {item.owner && (
                                  <span className="inline-flex items-center gap-1">
                                    <UserRound size={11} />
                                    {item.owner}
                                  </span>
                                )}
                                {item.due && (
                                  <span className="inline-flex items-center gap-1">
                                    <CalendarClock size={11} />
                                    {item.due}
                                  </span>
                                )}
                              </div>
                            </div>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                </section>
              ))}

              <p className="mt-1 text-[11px] text-muted/60 tabular-nums">
                {doneCount > 0 ? `已完成 ${doneCount} / ${items.length}` : `共 ${pendingCount} 项待办`}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
