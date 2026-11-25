import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertCircle, ArrowUpRight, CalendarDays, Check, Compass, Loader2, Send, Sparkles, Wrench,
} from 'lucide-react';
import {
  findInRecordings, listChatTools, pushFindAnswer,
  type FindResult, type McpPushResult,
} from '../lib/api';
import { useAuth } from '../lib/auth';
import { useToast } from '../lib/toast';
import { cn, fmtDurationCn } from '../lib/utils';

type ChatTool = {
  key: string;
  name: string;
  title: string;
  description: string;
  params: string[];
  serverId: string;
  serverName: string;
};

type Msg = {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  result?: FindResult;
  pending?: boolean;
  /** 这句回答对应的问题，手动推送时要一起发出去 */
  question?: string;
  pushed?: McpPushResult[];
};

/** 切换底部 tab 会卸载页面，用 sessionStorage 把对话留在当前标签页里。 */
function restoreMessages(key: string): Msg[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(key) || '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (m) =>
          m &&
          (m.role === 'user' || m.role === 'assistant') &&
          typeof m.content === 'string' &&
          !m.pending,
      )
      .map((m, i) => ({
        id: typeof m.id === 'string' ? m.id : `restored-${i}`,
        role: m.role,
        content: m.content,
        result: m.result,
      }));
  } catch {
    return [];
  }
}

const TOOL_LABEL: Record<string, string> = {
  search_keyword: '精确搜索',
  search_vector: '语义匹配',
  list_recordings: '日期定位',
  get_recording: '读取录音',
  final: '收敛',
};

const SUGGESTIONS = [
  '昨天我聊了些什么？',
  '帮我找找关于「旅行计划」的录音',
  '上次提到的那个人是什么时候聊的？',
];

function Matches({ result }: { result: FindResult }) {
  if (!result.matches.length) return null;
  return (
    <div className="mt-3 space-y-2">
      {result.matches.map((m, i) => {
        // 有录音时优先直达那条录音；带 startMs 就直接定位到命中的那句原话
        const directUrl = m.recordingUrl
          ? `${m.recordingUrl}${m.startMs != null ? `?t=${m.startMs}` : ''}`
          : null;
        return (
          <div
            key={`${m.recordingId ?? m.date}-${i}`}
            className="rounded-xl border border-line bg-panel2/60 p-3"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <Link
                  to={directUrl ?? m.jumpUrl}
                  className="text-[13.5px] font-medium hover:text-brand transition-colors line-clamp-1"
                >
                  {m.title || (directUrl ? '未命名录音' : '当天回忆')}
                </Link>
                <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted flex-wrap">
                  <span>{m.date}</span>
                  {m.time && <><span className="opacity-40">·</span><span>{m.time}</span></>}
                  {m.durationMs > 0 && (
                    <><span className="opacity-40">·</span><span>{fmtDurationCn(m.durationMs)}</span></>
                  )}
                  {m.matchedBy && (
                    <span className="chip bg-soft text-brand !px-1.5 !py-0">{m.matchedBy}</span>
                  )}
                </div>
              </div>
              {m.cited && (
                <span className="chip bg-brand/10 text-brand shrink-0 !px-1.5 !py-0">
                  <Sparkles size={10} />
                  依据
                </span>
              )}
            </div>

            <p className="mt-2 text-[12.5px] leading-[1.7] text-muted line-clamp-3">
              {m.snippet || '这段录音暂时没有可展示的文字'}
            </p>

            <div className="mt-2.5 flex items-center gap-2">
              {directUrl ? (
                <>
                  <Link
                    to={directUrl}
                    className="chip bg-soft text-brand hover:bg-brand/15 transition-colors"
                  >
                    <ArrowUpRight size={12} />
                    {m.startMs != null ? '直接跳到这段原话' : '打开这条录音'}
                  </Link>
                  <Link
                    to={m.jumpUrl}
                    className="chip bg-panel2 text-muted hover:text-ink transition-colors"
                  >
                    <CalendarDays size={12} />
                    查看当天
                  </Link>
                </>
              ) : (
                <Link
                  to={m.jumpUrl}
                  className="chip bg-soft text-brand hover:bg-brand/15 transition-colors"
                >
                  <CalendarDays size={12} />
                  查看当天录音
                </Link>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function AssistantBody({
  msg,
  onPush,
  pushing,
}: {
  msg: Msg;
  onPush: (msg: Msg, serverId: string) => void;
  pushing: boolean;
}) {
  if (msg.pending) {
    return (
      <div className="flex items-center gap-2 text-[13px] text-muted">
        <Loader2 size={14} className="animate-spin text-brand" />
        正在理解问题并检索录音…
      </div>
    );
  }
  const r = msg.result;
  const pushes = [...(r?.mcp?.pushes ?? []), ...(msg.pushed ?? [])];
  return (
    <>
      <p className="text-[14px] leading-[1.75] whitespace-pre-wrap">{msg.content}</p>

      {r && r.trace.length > 0 && (
        <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
          {r.trace
            .filter((t) => t.tool !== 'final')
            .map((t, i) => (
              <span
                key={i}
                className={cn(
                  'chip bg-panel2 text-muted !px-2 !py-0.5 !text-[10.5px]',
                  t.kind === 'mcp' && 'bg-brand/10 text-brand',
                )}
              >
                {t.kind === 'mcp' && <Wrench size={9} />}
                {t.label ?? TOOL_LABEL[t.tool] ?? t.tool} {t.hits > 0 ? `${t.hits} 条` : ''}
              </span>
            ))}
        </div>
      )}

      {r && <Matches result={r} />}

      {/* 推送结果（自动 + 手动） */}
      {!!pushes.length && (
        <div className="mt-2.5 space-y-1">
          {pushes.map((p, i) => (
            <div
              key={`${p.serverId}-${i}`}
              className={cn(
                'flex items-start gap-1.5 text-[11.5px]',
                p.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400',
              )}
            >
              {p.ok ? <Check size={12} className="mt-0.5 shrink-0" /> : <AlertCircle size={12} className="mt-0.5 shrink-0" />}
              <span>
                {p.ok ? '已发送到' : '发送失败'}「{p.serverName}」· {p.tool}
                {p.message ? `：${p.message}` : ''}
                {p.reason ? `（${p.reason}）` : ''}
              </span>
            </div>
          ))}
        </div>
      )}

      {r?.mcp?.pushNote && (
        <p className="mt-2 text-[11px] text-muted/80">{r.mcp.pushNote}</p>
      )}

      {/* 手动推送按钮 */}
      {!!r?.mcp?.manualTargets?.length && (
        <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
          <span className="text-[11px] text-muted">发送总结到：</span>
          {r.mcp.manualTargets.map((t) => (
            <button
              key={t.serverId}
              onClick={() => onPush(msg, t.serverId)}
              disabled={pushing}
              className="chip bg-soft text-brand hover:bg-brand/15 transition-colors disabled:opacity-50"
              title={t.tool ? `由模型决定工具（建议 ${t.tool}）` : '由模型决定调用哪个工具'}
            >
              {pushing ? <Loader2 size={11} className="animate-spin" /> : <Send size={11} />}
              {t.serverName}
            </button>
          ))}
        </div>
      )}

      {r?.note && (
        <p className="mt-2 text-[11px] text-muted/80">{r.note}</p>
      )}
    </>
  );
}

export default function FindPage() {
  const toast = useToast();
  const { user } = useAuth();
  const msgKey = `recall.find.messages.${user?.id ?? 'anon'}`;
  const inputKey = `recall.find.input.${user?.id ?? 'anon'}`;
  const [messages, setMessages] = useState<Msg[]>(() => restoreMessages(msgKey));
  const [input, setInput] = useState(() => sessionStorage.getItem(inputKey) || '');
  const [busy, setBusy] = useState(false);
  const [pushing, setPushing] = useState(false);
  const [chatTools, setChatTools] = useState<ChatTool[]>([]);
  const [showAllTools, setShowAllTools] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  // 可用的外部工具（设置页配置的 MCP）；只影响展示，取不到就静默
  useEffect(() => {
    listChatTools()
      .then((r) => setChatTools(r.tools ?? []))
      .catch(() => setChatTools([]));
  }, []);

  // 对话与输入草稿落到 sessionStorage，返回本页时原样恢复
  useEffect(() => {
    try {
      sessionStorage.setItem(msgKey, JSON.stringify(messages.filter((m) => !m.pending)));
    } catch {
      /* 存储空间不足时忽略，保持当前会话可用 */
    }
  }, [messages, msgKey]);

  useEffect(() => {
    try {
      sessionStorage.setItem(inputKey, input);
    } catch {
      /* 同上 */
    }
  }, [input, inputKey]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages]);

  const ask = useCallback(
    async (text: string) => {
      const question = text.trim();
      if (!question || busy) return;

      // 只把此前的问答带给服务端，避免重复提交当前问题
      const history = messages
        .filter((m) => !m.pending && m.content.trim())
        .map((m) => ({ role: m.role, content: m.content }));

      const pendingId = `pending-${Date.now()}`;
      const userMsg: Msg = { id: `u-${Date.now()}`, role: 'user', content: question };
      setMessages((cur) => [
        ...cur,
        userMsg,
        { id: pendingId, role: 'assistant', content: '', pending: true },
      ]);
      setInput('');
      setBusy(true);
      try {
        const res = await findInRecordings(question, history);
        setMessages((cur) =>
          cur.map((m) =>
            m.id === pendingId
              ? { id: pendingId, role: 'assistant', content: res.answer, result: res, question }
              : m,
          ),
        );
      } catch (e: any) {
        setMessages((cur) => cur.filter((m) => m.id !== pendingId));
        toast.push(e?.message ?? '找寻失败', 'error');
      } finally {
        setBusy(false);
      }
    },
    [busy, messages, toast],
  );

  const pushToMcp = useCallback(
    async (msg: Msg, serverId: string) => {
      if (pushing) return;
      setPushing(true);
      try {
        const matches = (msg.result?.matches ?? [])
          .map((m) => `${m.date}《${m.title || '未命名录音'}》`)
          .join('；');
        const res = await pushFindAnswer(msg.question || '', msg.content, {
          serverIds: [serverId],
          matches,
        });
        setMessages((cur) =>
          cur.map((m) =>
            m.id === msg.id ? { ...m, pushed: [...(m.pushed ?? []), ...res.results] } : m,
          ),
        );
        if (!res.results.length) toast.push(res.message || '没有可用的推送目标', 'error');
        else toast.push(res.ok ? '已发送' : '发送失败，详情见消息下方', res.ok ? 'success' : 'error');
      } catch (e: any) {
        toast.push(e?.message ?? '推送失败', 'error');
      } finally {
        setPushing(false);
      }
    },
    [pushing, toast],
  );

  const empty = messages.length === 0;
  // 工具条跟随最近一次回答：模型按那次问答挑出来的工具，随消息一起存进 sessionStorage
  const lastResult = [...messages].reverse().find((m) => m.result && !m.pending)?.result;
  const recommended = lastResult?.mcp?.recommendedTools ?? [];
  const recommendNote = lastResult?.mcp?.recommendNote || '';

  return (
    <div className="max-w-3xl mx-auto px-4 md:px-8 py-6 md:py-10">
      <div className="flex items-start gap-3">
        <div className="h-10 w-10 rounded-[14px] bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] grid place-items-center shadow-[0_8px_20px_-10px_var(--brand)] shrink-0">
          <Compass size={19} className="text-white" strokeWidth={2.3} />
        </div>
        <div>
          <h1 className="text-[22px] md:text-[24px] font-semibold tracking-tight leading-tight">找寻</h1>
          <p className="text-[12.5px] text-muted mt-1">
            直接问一句，AI 会在你的录音里精确检索、语义匹配，找到最相关的那一段。
          </p>
        </div>
      </div>

      {empty && (
        <div className="mt-8 card p-6 md:p-8 bg-gradient-to-br from-panel to-panel2/40">
          <div className="text-[13px] text-muted">试试这样问：</div>
          <div className="mt-3 flex flex-col gap-2">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                onClick={() => void ask(s)}
                className="text-left rounded-xl border border-line bg-panel px-3.5 py-3 text-[13.5px] hover:border-brand/40 hover:bg-soft/40 transition-colors flex items-center gap-2"
              >
                <Sparkles size={14} className="text-brand shrink-0" />
                {s}
              </button>
            ))}
          </div>
          <p className="mt-4 text-[11px] text-muted/80 leading-relaxed">
            支持人名、地名、原话等精确搜索，也支持「最近聊过什么」这类模糊描述；找到录音后可以直接打开那条记录，并定位到命中的原话。
          </p>
        </div>
      )}

      <div className="mt-5 space-y-5">
        {messages.map((m) => (
          <div key={m.id}>
            {m.role === 'user' ? (
              <div className="flex justify-end">
                <div className="max-w-[85%] rounded-2xl rounded-tr-md bg-gradient-to-br from-[var(--brand)] to-[var(--brand-2)] text-white px-4 py-2.5 text-[14px] leading-relaxed shadow-[0_6px_16px_-10px_var(--brand)]">
                  {m.content}
                </div>
              </div>
            ) : (
              <div className="flex items-start gap-2.5">
                <div className="h-7 w-7 rounded-lg bg-soft text-brand grid place-items-center shrink-0 mt-0.5">
                  <Sparkles size={14} />
                </div>
                <div className="flex-1 min-w-0 card px-4 py-3.5">
                  <AssistantBody msg={m} onPush={pushToMcp} pushing={pushing} />
                </div>
              </div>
            )}
          </div>
        ))}
      </div>

      <div ref={bottomRef} className="h-1" />

      <div className="sticky bottom-[62px] md:bottom-0 z-20 mt-5 -mx-4 md:-mx-8 px-4 md:px-8 py-3 bg-bg/90 backdrop-blur-xl border-t border-line/70">
        {(chatTools.length > 0 || lastResult) && (
          <div className="mb-2 flex items-center gap-1.5 flex-wrap text-[10.5px] text-muted">
            <Wrench size={11} className="text-brand shrink-0" />
            {showAllTools ? (
              <>
                <span>全部工具（{chatTools.length}）：</span>
                <span className="chip bg-panel2 !px-1.5 !py-0">内置检索 4</span>
                {chatTools.map((t) => (
                  <span
                    key={t.key}
                    title={t.description || t.title}
                    className="chip bg-panel2 !px-1.5 !py-0 max-w-[220px] truncate"
                  >
                    {t.serverName} · {t.name}
                  </span>
                ))}
                <button
                  type="button"
                  onClick={() => setShowAllTools(false)}
                  className="underline underline-offset-2 hover:text-brand"
                >
                  只看推荐
                </button>
              </>
            ) : (
              <>
                <span title={lastResult?.mcp?.recommendReason || undefined}>本次推荐工具：</span>
                <span className="chip bg-panel2 !px-1.5 !py-0">内置检索 4</span>
                {busy ? (
                  <span className="inline-flex items-center gap-1">
                    <Loader2 size={10} className="animate-spin" /> 模型判断中…
                  </span>
                ) : (
                  recommended.map((t) => (
                    <span
                      key={t.key}
                      title={[t.reason, t.description].filter(Boolean).join('\n')}
                      className="chip bg-brand/10 text-brand !px-1.5 !py-0 max-w-[220px] truncate"
                    >
                      {t.serverName} · {t.name}
                    </span>
                  ))
                )}
                {!busy && !!recommended.length && recommendNote && (
                  <span className="text-muted/70">（{recommendNote}）</span>
                )}
                {!busy && !recommended.length && (
                  <span>
                    {!chatTools.length
                      ? '还没有开启可用于对话的 MCP 工具'
                      : lastResult
                        ? recommendNote || '模型判断本次无需外部工具'
                        : '问一句，模型会挑出用得上的工具'}
                  </span>
                )}
                {chatTools.length > 0 && (
                  <button
                    type="button"
                    onClick={() => setShowAllTools(true)}
                    className="underline underline-offset-2 hover:text-brand"
                  >
                    全部 {chatTools.length} 个
                  </button>
                )}
              </>
            )}
          </div>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void ask(input);
          }}
          className={cn(
            'card flex items-center gap-2 p-2 pl-4 transition-colors',
            busy && 'opacity-80',
          )}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="想找什么，直接问…"
            disabled={busy}
            className="flex-1 min-w-0 h-9 bg-transparent outline-none text-[14px] placeholder:text-muted/70"
          />
          <button
            type="submit"
            disabled={busy || !input.trim()}
            className="btn-primary h-9 px-3.5 shrink-0 !rounded-xl"
            title="发送"
          >
            {busy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
            <span className="hidden sm:inline">发送</span>
          </button>
        </form>
      </div>
    </div>
  );
}
