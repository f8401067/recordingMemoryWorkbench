import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  Check, Clipboard, ClipboardCheck, Download, Image as ImageIcon, Loader2, Share2, X,
} from 'lucide-react';
import type { Summary } from '../lib/api';
import { htmlToPng, summaryToMarkdown, summaryToPosterHtml, type PosterResult } from '../lib/poster';
import { useToast } from '../lib/toast';
import { cn } from '../lib/utils';

const safeName = (title: string) =>
  (title || '纪要').replace(/[\\/:*?"<>|\n\r\t]/g, '_').slice(0, 80);

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export default function ShareSummary({
  summary,
  onClose,
}: {
  summary: Summary;
  onClose: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [poster, setPoster] = useState<PosterResult | null>(null);
  const [copied, setCopied] = useState<'md' | 'img' | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  useEffect(() => () => {
    if (poster) URL.revokeObjectURL(poster.url);
  }, [poster]);

  const copyMarkdown = async () => {
    const md = summaryToMarkdown(summary);
    try {
      await navigator.clipboard.writeText(md);
      setCopied('md');
      toast.push('已复制 Markdown', 'success');
      setTimeout(() => setCopied(null), 1600);
    } catch {
      toast.push('复制失败，请手动选择文本', 'error');
    }
  };

  const downloadMarkdown = () => {
    saveBlob(
      new Blob([summaryToMarkdown(summary)], { type: 'text/markdown;charset=utf-8' }),
      `${safeName(summary.title)}.md`,
    );
    toast.push('已下载 Markdown', 'success');
  };

  const makePoster = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const res = await htmlToPng(summaryToPosterHtml(summary));
      setPoster(res);
    } catch (e: any) {
      toast.push(e?.message ?? '生成长图失败', 'error');
    } finally {
      setBusy(false);
    }
  };

  const downloadPoster = () => {
    if (!poster) return;
    saveBlob(poster.blob, `${safeName(summary.title)}.png`);
    toast.push('已保存长图', 'success');
  };

  const sharePoster = async () => {
    if (!poster) return;
    const file = new File([poster.blob], `${safeName(summary.title)}.png`, { type: 'image/png' });
    if (!navigator.canShare?.({ files: [file] })) {
      toast.push('当前浏览器不支持直接分享，请保存图片后发送', 'info');
      return;
    }
    try {
      await navigator.share({ files: [file], title: summary.title });
    } catch {
      /* 用户取消，不打扰 */
    }
  };

  const copyPoster = async () => {
    if (!poster) return;
    try {
      if (!('ClipboardItem' in window)) throw new Error('unsupported');
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': poster.blob })]);
      setCopied('img');
      toast.push('图片已复制', 'success');
      setTimeout(() => setCopied(null), 1600);
    } catch {
      toast.push('当前浏览器不支持复制图片，请保存后发送', 'error');
    }
  };

  const canShareFiles =
    poster && typeof navigator.canShare === 'function' &&
    navigator.canShare({ files: [new File([poster.blob], 'x.png', { type: 'image/png' })] });

  return createPortal(
    <div
      className="fixed inset-0 z-[90] bg-black/45 backdrop-blur-[2px] flex items-end md:items-center justify-center p-0 md:p-4"
      onClick={onClose}
    >
      <div
        className="w-full md:max-w-md rounded-t-3xl md:rounded-2xl bg-panel border border-line shadow-lift animate-fade-up max-h-[92vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 h-12 border-b border-line sticky top-0 bg-panel z-10">
          <div className="flex items-center gap-2 text-[14px] font-semibold">
            <Share2 size={15} className="text-brand" />
            分享这份纪要
          </div>
          <button
            onClick={onClose}
            className="h-8 w-8 grid place-items-center rounded-lg text-muted hover:bg-panel2"
            aria-label="关闭"
          >
            <X size={16} />
          </button>
        </div>

        {!poster && (
          <div className="p-2">
            <button
              onClick={() => void copyMarkdown()}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-panel2 text-left transition-colors"
            >
              <span className="h-9 w-9 shrink-0 rounded-xl bg-soft text-brand grid place-items-center">
                {copied === 'md' ? <ClipboardCheck size={16} /> : <Clipboard size={16} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-medium">复制 Markdown</span>
                <span className="block text-[11px] text-muted mt-0.5">粘到飞书、Notion、Obsidian 都保留格式</span>
              </span>
            </button>

            <button
              onClick={downloadMarkdown}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-panel2 text-left transition-colors"
            >
              <span className="h-9 w-9 shrink-0 rounded-xl bg-soft text-brand grid place-items-center">
                <Download size={16} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-medium">下载 .md 文件</span>
                <span className="block text-[11px] text-muted mt-0.5">含标题、日期与思维导图大纲</span>
              </span>
            </button>

            <button
              onClick={() => void makePoster()}
              disabled={busy}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-panel2 text-left transition-colors disabled:opacity-60"
            >
              <span className="h-9 w-9 shrink-0 rounded-xl bg-soft text-brand grid place-items-center">
                {busy ? <Loader2 size={16} className="animate-spin" /> : <ImageIcon size={16} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13.5px] font-medium">{busy ? '正在生成长图…' : '生成长图'}</span>
                <span className="block text-[11px] text-muted mt-0.5">整篇排成一张图片，适合发群和朋友圈</span>
              </span>
            </button>
          </div>
        )}

        {poster && (
          <div className="p-3">
            <div className="rounded-xl border border-line bg-panel2 overflow-y-auto max-h-[54vh]">
              <img src={poster.url} alt="纪要长图预览" className="w-full block" />
            </div>
            <p className="mt-2 text-[11px] text-muted/70 tabular-nums text-center">
              {poster.width} × {poster.height} px
            </p>

            <div className="mt-3 flex items-center gap-2">
              <button
                onClick={downloadPoster}
                className="flex-1 h-10 rounded-xl bg-brand text-white text-[13px] font-medium inline-flex items-center justify-center gap-1.5 active:scale-[0.98] transition"
              >
                <Download size={15} />
                保存图片
              </button>
              {canShareFiles && (
                <button
                  onClick={() => void sharePoster()}
                  className="flex-1 h-10 rounded-xl border border-line text-[13px] font-medium inline-flex items-center justify-center gap-1.5 hover:border-brand/40 active:scale-[0.98] transition"
                >
                  <Share2 size={15} />
                  分享
                </button>
              )}
            </div>

            <div className="mt-2 flex items-center justify-between gap-2">
              <button
                onClick={() => void copyPoster()}
                className="h-8 px-2.5 rounded-lg text-[12px] text-muted hover:bg-panel2 inline-flex items-center gap-1.5"
              >
                {copied === 'img' ? <Check size={13} className="text-emerald-500" /> : <Clipboard size={13} />}
                复制图片
              </button>
              <button
                onClick={() => {
                  URL.revokeObjectURL(poster.url);
                  setPoster(null);
                }}
                className={cn('h-8 px-2.5 rounded-lg text-[12px] text-muted hover:bg-panel2')}
              >
                换一种分享方式
              </button>
            </div>

            <p className="mt-1 text-[11px] text-muted/60 leading-relaxed text-center">
              手机长按图片也能直接保存或发送
            </p>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
