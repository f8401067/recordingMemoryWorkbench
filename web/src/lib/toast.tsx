import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { CheckCircle2, AlertCircle, Info, X } from 'lucide-react';
import { cn } from './utils';

type Toast = { id: number; text: string; tone: 'success' | 'error' | 'info' };
type Ctx = { push: (text: string, tone?: Toast['tone']) => void };

const ToastCtx = createContext<Ctx>({ push: () => {} });

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);

  const push = useCallback((text: string, tone: Toast['tone'] = 'info') => {
    const now = Date.now();
    setItems((prev) => {
      // 2 秒内相同内容只保留一条，避免接口连续失败刷屏
      if (prev.some((t) => t.text === text && now - t.id < 2000)) return prev;
      const id = now + Math.random();
      setTimeout(() => setItems((p) => p.filter((x) => x.id !== id)), 4200);
      return [...prev.slice(-3), { id, text, tone }];
    });
  }, []);

  const value = useMemo(() => ({ push }), [push]);

  return (
    <ToastCtx.Provider value={value}>
      {children}
      <div className="fixed inset-x-0 top-3 z-[100] flex flex-col items-center gap-2 px-3 pointer-events-none">
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              'pointer-events-auto flex items-center gap-2.5 rounded-xl px-3.5 py-2.5 text-sm shadow-lift animate-fade-up max-w-sm',
              'border backdrop-blur-md',
              t.tone === 'success' && 'bg-emerald-500/95 text-white border-emerald-400',
              t.tone === 'error' && 'bg-red-500/95 text-white border-red-400',
              t.tone === 'info' && 'bg-panel/95 text-ink border-line',
            )}
          >
            {t.tone === 'success' && <CheckCircle2 size={16} className="shrink-0" />}
            {t.tone === 'error' && <AlertCircle size={16} className="shrink-0" />}
            {t.tone === 'info' && <Info size={16} className="shrink-0 text-brand" />}
            <span className="leading-snug">{t.text}</span>
            <button
              className="ml-1 opacity-60 hover:opacity-100"
              onClick={() => setItems((p) => p.filter((x) => x.id !== t.id))}
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);
