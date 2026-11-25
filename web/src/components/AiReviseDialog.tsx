import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Loader2, Sparkles, X } from 'lucide-react';
import { reviseSummary, type Summary } from '../lib/api';
import { useToast } from '../lib/toast';

/** 常用修改意图的快捷选项，点一下填进输入框，用户可继续改 */
const QUICK_PROMPTS = [
  '把标题改得更口语化一点',
  '压缩到一半篇幅，保留结论和待办',
  '把待办事项按优先级排个序',
  '补充日期、人名等细节，让纪要更具体',
  '语气改得更像给家人看的日记',
];

export default function AiReviseDialog({
  summary,
  onClose,
  onRevised,
}: {
  summary: Summary;
  onClose: () => void;
  /** 服务端返回修订后的纪要时回调，页面用它刷新本地状态 */
  onRevised: (s: Summary) => void;
}) {
  const toast = useToast();
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose();
    };
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [busy, onClose]);

  const submit = async () => {
    const text = instruction.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const next = await reviseSummary(summary.id, text);
      onRevised(next);
      onClose();
      toast.push('已提交，AI 正在按要求修改这份纪要', 'success');
    } catch (e: any) {
      toast.push(e?.message ?? '提交修改要求失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[90] bg-black/45 backdrop-blur-[2px] flex items-end md:items-center justify-center p-0 md:p-4"
      onClick={() => { if (!busy) onClose(); }}
    >
      <div
        className="w-full md:max-w-lg rounded-t-3xl md:rounded-2xl bg-panel border border-line shadow-lift animate-fade-up max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 h-12 border-b border-line sticky top-0 bg-panel z-10">
          <div className="flex items-center gap-2 text-[14px] font-semibold">
            <Sparkles size={15} className="text-brand" />
            AI 修改这份纪要
          </div>
          <button
            onClick={onClose}
            disabled={busy}
            className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2 disabled:opacity-40"
            aria-label="关闭"
          >
            <X size={16} />
          </button>
        </div>

        <div className="p-4 space-y-3">
          <div className="text-[12.5px] text-muted leading-relaxed">
            用一句话说明想怎么改（如「把待办删到只剩今天能做的」「标题换成问句」）。
            AI 会基于这份纪要和原始录音材料修订，不会改没提到的部分；提交后自动刷新结果。
          </div>

          <div className="flex flex-wrap gap-1.5">
            {QUICK_PROMPTS.map((q) => (
              <button
                key={q}
                onClick={() => setInstruction(q)}
                disabled={busy}
                className="chip bg-soft text-brand hover:bg-brand/15 transition-colors disabled:opacity-40"
              >
                {q}
              </button>
            ))}
          </div>

          <textarea
            ref={inputRef}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
                e.preventDefault();
                void submit();
              }
            }}
            rows={4}
            maxLength={2000}
            spellCheck={false}
            placeholder="例如：把「明天要做的」单独列成待办，其余内容压缩一点"
            className="field h-auto min-h-[96px] w-full resize-y py-2 text-[13.5px] leading-relaxed"
          />
          <div className="flex items-center justify-between text-[11px] text-muted">
            <span>⌘S 提交 · Esc 取消</span>
            <span className="tabular-nums">{instruction.length}/2000</span>
          </div>
        </div>

        <div className="px-4 pb-4 flex items-center gap-2">
          <button
            onClick={onClose}
            disabled={busy}
            className="btn-ghost flex-1 h-9 text-[13px]"
          >
            取消
          </button>
          <button
            onClick={() => void submit()}
            disabled={busy || !instruction.trim()}
            className="btn-primary flex-1 h-9 text-[13px]"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
            {busy ? '已提交，等 AI 修改…' : '让 AI 按要求修改'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
