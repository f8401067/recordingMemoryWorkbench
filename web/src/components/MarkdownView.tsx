import React, { useEffect, useMemo } from 'react';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

marked.setOptions({ gfm: true, breaks: true });

export default function MarkdownView({
  markdown,
  className = '',
}: {
  markdown: string;
  className?: string;
}) {
  const html = useMemo(() => {
    if (!markdown) return '';
    let raw = marked.parse(markdown, { async: false }) as string;
    // 把 GFM 任务列表的 <li><input type=checkbox> 调整为更紧凑的样式
    raw = raw.replace(/<li>\s*(<input[^>]*type="checkbox"[^>]*>)/g, '<li class="task-item">$1');
    return DOMPurify.sanitize(raw, {
      ADD_ATTR: ['target', 'rel', 'checked', 'disabled', 'type'],
    });
  }, [markdown]);

  return (
    <div
      className={`prose-recall ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
