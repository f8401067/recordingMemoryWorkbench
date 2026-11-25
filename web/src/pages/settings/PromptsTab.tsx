import React, { useEffect, useState } from 'react';
import { Check, Info, Loader2, RotateCcw } from 'lucide-react';
import { useAuth } from '../../lib/auth';
import { useToast } from '../../lib/toast';
import type { UserSettings } from '../../lib/api';
import { cn } from '../../lib/utils';

type PromptField = 'summaryPrompt' | 'promptDay' | 'promptWeek' | 'promptMonth' | 'promptYear' | 'promptRange';

type CardMeta = {
  field: PromptField;
  title: string;
  tag: string;
  desc: string;
  outline: string;
  placeholder: string;
};

const CARDS: CardMeta[] = [
  {
    field: 'summaryPrompt',
    title: '单条录音',
    tag: '会议 / 课程 / 访谈 / 灵感',
    desc: '一条录音转写完成后自动生成的那份纪要，风格由上方的「纪要风格」决定。',
    outline: '会议：讨论要点 / 结论与决定 / 待办事项',
    placeholder: '例如：每条结论后面标注是谁说的；技术术语保留英文原文；不要输出待办小节。',
  },
  {
    field: 'promptDay',
    title: '日报 · 每日回忆',
    tag: 'day',
    desc: '同一天里所有录音合成一份回忆，偏日记口吻。',
    outline: '今天都聊了什么 / 冒出来的想法 / 待办事项 / 人物与心情',
    placeholder: '例如：只写跟家人有关的部分；待办按紧急程度排序；语气轻松一点。',
  },
  {
    field: 'promptWeek',
    title: '周报 · 每周回忆',
    tag: 'week',
    desc: '周一到周日这七天的回顾，按天或按话题串联。',
    outline: '这周在忙什么 / 值得记住的片段 / 待办事项 / 人物与心情',
    placeholder: '例如：按项目分块写；标出这周推进不下去的事；周末的部分可以简略。',
  },
  {
    field: 'promptMonth',
    title: '月报 · 每月回忆',
    tag: 'month',
    desc: '整月的回顾，压缩细节、抓主线。',
    outline: '这个月在忙什么 / 几件重要的事 / 一直挂着的待办 / 人物与心情',
    placeholder: '例如：开头先用一句话概括这个月的状态；和上个月相比的变化单独写一段。',
  },
  {
    field: 'promptYear',
    title: '年报 · 年度回忆',
    tag: 'year',
    desc: '一整年的回顾，抓主干与转折，不逐月罗列。',
    outline: '这一年的主线 / 几个重要阶段 / 还没做完的事 / 人物与心情',
    placeholder: '例如：按「城市 / 工作 / 家庭」三条线写；只保留能代表这一年的细节。',
  },
  {
    field: 'promptRange',
    title: '任意时段',
    tag: '自定义区间',
    desc: '不是整周 / 整月 / 整年的自定义起止区间，比如出差那几天。',
    outline: '这段时间在忙什么 / 值得记住的片段 / 待办事项 / 人物与心情',
    placeholder: '例如：按天写，每天两三句；只关注跟这次出差有关的内容。',
  },
];

const MAX = 2000;

function PromptCard({
  meta,
  value,
  onSave,
}: {
  meta: CardMeta;
  value: string;
  onSave: (field: PromptField, next: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const [justSaved, setJustSaved] = useState(false);

  // 服务端值变化（切换账号、别处保存）时跟上；本地有未保存修改就不覆盖
  useEffect(() => {
    if (draft.trim() === value.trim()) return;
    if (!draft) setDraft(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  const dirty = draft.trim() !== value.trim();

  const save = async () => {
    setSaving(true);
    try {
      await onSave(meta.field, draft.trim());
      setJustSaved(true);
      window.setTimeout(() => setJustSaved(false), 1600);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="card p-4 md:p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-[13.5px] font-semibold">{meta.title}</h3>
            <span className="chip bg-panel2 text-muted !py-0 !px-1.5 font-mono text-[10px]">{meta.tag}</span>
            {value.trim() && (
              <span className="chip bg-soft text-brand !py-0 !px-1.5 text-[10px]">
                <Check size={9} /> 已设置
              </span>
            )}
          </div>
          <p className="text-[11.5px] text-muted mt-1 leading-relaxed">{meta.desc}</p>
          <p className="text-[10.5px] text-muted/80 mt-1 leading-relaxed">内置结构：{meta.outline}</p>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {draft.trim() && (
            <button
              onClick={() => setDraft('')}
              className="text-[11.5px] h-8 px-2 rounded-lg text-muted hover:text-ink hover:bg-panel2 inline-flex items-center gap-1"
              title="清空并恢复内置提示词"
            >
              <RotateCcw size={11} /> 清空
            </button>
          )}
          <button
            onClick={() => void save()}
            disabled={!dirty || saving}
            className={cn(
              'h-8 px-3 rounded-lg text-[12px] transition-colors inline-flex items-center gap-1',
              dirty ? 'btn-primary !h-8 !px-3 !text-[12px]' : 'btn-ghost !h-8 !px-3 !text-[12px]',
              justSaved && '!bg-emerald-500/15 !text-emerald-600 dark:!text-emerald-400',
            )}
          >
            {saving ? <Loader2 size={12} className="animate-spin" /> : justSaved ? <Check size={12} /> : null}
            {saving ? '保存中' : justSaved ? '已保存' : '保存'}
          </button>
        </div>
      </div>

      <textarea
        value={draft}
        onChange={(e) => setDraft(e.target.value.slice(0, MAX))}
        placeholder={meta.placeholder}
        className="field w-full mt-3 min-h-[84px] text-[12px] leading-relaxed resize-y"
      />
      <div className="mt-1 flex items-center justify-between text-[10.5px] text-muted/70">
        <span>留空 = 用内置提示词；填写后会替换掉风格描述，JSON 输出格式仍由系统保证</span>
        <span className="tabular-nums">{draft.length}/{MAX}</span>
      </div>
    </div>
  );
}

export default function PromptsTab() {
  const { settings, patchSettings } = useAuth();
  const toast = useToast();

  const save = async (field: PromptField, next: string) => {
    try {
      await patchSettings({ [field]: next } as Partial<UserSettings>);
      toast.push(next ? '已保存，之后生成的这类回忆会带上这些要求' : '已清空，改回内置提示词', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '保存失败', 'error');
      throw e;
    }
  };

  return (
    <div className="space-y-3">
      <div className="card p-4 flex items-start gap-2.5">
        <div className="h-8 w-8 rounded-lg bg-panel2 grid place-items-center shrink-0">
          <Info size={15} className="text-muted" />
        </div>
        <div className="text-[11.5px] leading-relaxed text-muted">
          <div className="text-[13px] font-medium text-ink">每种回忆可以有不同的要求</div>
          <p className="mt-1">
            单条录音、日报、周报、月报、年报各自读自己的提示词，互不影响。
            填写的内容作为「用户自定义要求」交给模型，会            <strong className="text-ink/80">替换</strong>掉该风格的描述；
            不写人名的硬性规则和 JSON 输出契约始终保留。已经生成过的回忆需要点「重新整理」才会用上新提示词。
          </p>
        </div>
      </div>

      {CARDS.map((meta) => (
        <PromptCard
          key={meta.field}
          meta={meta}
          value={(settings?.[meta.field] as string) ?? ''}
          onSave={save}
        />
      ))}
    </div>
  );
}
