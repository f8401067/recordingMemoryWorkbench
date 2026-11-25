import html2canvas from 'html2canvas';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import type { Summary } from './api';

marked.setOptions({ gfm: true, breaks: true });

const BRAND = '#5b5bd6';
const FONT = '-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Segoe UI",sans-serif';

/** 长图宽度（CSS px）；导出时按 2 倍图渲染 */
const POSTER_WIDTH = 720;

/**
 * 长图样式必须挂在 document 上：html2canvas 是从文档样式表里取规则的，
 * 塞在待渲染节点内部的 <style> 它读不到。
 */
const STYLE = `
.rp{width:${POSTER_WIDTH}px;background:#ffffff;color:#14161f;font-family:${FONT};}
.rp,.rp *{box-sizing:border-box;}
.rp .rp-hd{padding:40px 44px 0;}
.rp .rp-tag{font-size:12px;color:${BRAND};font-weight:600;letter-spacing:.02em;}
.rp .rp-title{margin:10px 0 0;font-size:29px;line-height:1.34;font-weight:700;color:#14161f;}
.rp .rp-meta{margin-top:12px;font-size:12.5px;color:#8a8f9c;}
.rp .rp-meta span{margin:0 6px;color:#d3d6e0;}
.rp .rp-rule{height:1px;background:#eceef4;margin:22px 44px 0;}
.rp .rp-body{padding:20px 44px 0;}
.rp .rp-box{background:#f5f5ff;border-radius:14px;padding:16px 18px;margin-bottom:12px;}
.rp .rp-box-t{font-size:11.5px;font-weight:600;color:${BRAND};letter-spacing:.06em;margin-bottom:8px;}
.rp .rp-box ul{margin:0;padding:0;list-style:none;}
.rp .rp-box li{font-size:13.5px;line-height:1.7;color:#2a2e3a;}
.rp .rp-box li>.rp-li{display:block;margin-left:14px;}
.rp .rp-box li>.rp-li>.rp-dot{float:left;width:4px;height:4px;border-radius:2px;
  background:#c3c3f2;margin:9px 10px 0 -14px;}
.rp .rp-md>h1{font-size:20px;line-height:1.4;font-weight:700;margin:24px 0 10px;color:#14161f;}
.rp .rp-md>h2{font-size:17px;line-height:1.45;font-weight:700;margin:24px 0 9px;padding-left:10px;border-left:3px solid ${BRAND};color:#14161f;}
.rp .rp-md>h3{font-size:15px;line-height:1.5;font-weight:600;margin:18px 0 4px;color:#14161f;}
.rp .rp-md>h4{font-size:13.5px;line-height:1.5;font-weight:600;margin:14px 0 4px;color:#2a2e3a;}
.rp .rp-md p{font-size:14px;line-height:1.78;color:#2a2e3a;margin:0 0 10px;}
.rp .rp-md ul,.rp .rp-md ol{margin:0 0 10px;padding-left:0;list-style:none;}
.rp .rp-md ul ul,.rp .rp-md ul ol,.rp .rp-md ol ul,.rp .rp-md ol ol{margin:6px 0 6px;padding-left:16px;}
.rp .rp-md li{font-size:14px;line-height:1.78;color:#2a2e3a;margin-bottom:4px;}
.rp .rp-md li>.rp-li{display:block;margin-left:16px;}
.rp .rp-md li>.rp-li>.rp-dot{float:left;width:4px;height:4px;border-radius:2px;
  background:#c3c3f2;margin:10px 12px 0 -16px;}
.rp .rp-md li>.rp-li>.rp-num{float:left;width:16px;margin-left:-16px;color:#8a8f9c;}
.rp .rp-md li>ul,.rp .rp-md li>ol{margin-left:16px;}
.rp .rp-md strong{font-weight:600;color:#14161f;}
.rp .rp-md a{color:${BRAND};text-decoration:none;}
.rp .rp-md code{background:#f3f4f8;border-radius:4px;padding:1px 5px;font-size:12.5px;font-family:Menlo,monospace;color:#454a63;}
.rp .rp-md pre{background:#f7f7fb;border-radius:12px;padding:14px 16px;margin:0 0 12px;white-space:pre-wrap;word-break:break-word;font-size:12.5px;line-height:1.7;font-family:Menlo,monospace;color:#454a63;}
.rp .rp-md pre code{background:none;padding:0;font-size:inherit;}
.rp .rp-md blockquote{margin:0 0 12px;padding:2px 0 2px 12px;border-left:3px solid #e2e2f6;color:#5d6272;font-size:13.5px;line-height:1.7;}
.rp .rp-md hr{border:none;height:1px;background:#eceef4;margin:20px 0;}
.rp .rp-md table{width:100%;border-collapse:collapse;margin:0 0 12px;font-size:13px;}
.rp .rp-md th,.rp .rp-md td{border:1px solid #eceef4;padding:7px 10px;text-align:left;line-height:1.6;}
.rp .rp-md th{background:#f7f7fb;font-weight:600;}
.rp .rp-md img{max-width:100%;border-radius:10px;}
.rp .rp-ft{padding:26px 44px 34px;font-size:11.5px;color:#a3a8b4;}
.rp .rp-ft b{color:${BRAND};font-weight:600;}
.rp .rp-ft-r{float:right;}
`;

let styleInjected = false;
function ensureStyles() {
  if (styleInjected) return;
  const el = document.createElement('style');
  el.id = 'recall-poster-style';
  el.textContent = STYLE;
  document.head.appendChild(el);
  styleInjected = true;
}

const escapeHtml = (text: string) =>
  text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');

/**
 * html2canvas 不画 list-style 的圆点/序号，所以把标记换成真实元素塞进 <li>。
 * 有序列表按各自层级的顺序重新编号。
 */
function decorateLists(html: string): string {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const root = tpl.content;

  const markItem = (li: Element, mark: HTMLElement) => {
    // 正文包一层 block 并用 margin-left 缩进；圆点靠负 margin 缩回去。
    // html2canvas 不认 padding/text-indent/绝对定位的偏移，只有这种写法能保证
    // 折行后的第二行和首行文字左对齐。
    const body = document.createElement('span');
    body.className = 'rp-li';
    Array.from(li.childNodes).forEach((node) => {
      if (node.nodeType === 1 && ['UL', 'OL'].includes((node as Element).tagName)) return;
      body.appendChild(node);
    });
    body.insertBefore(mark, body.firstChild);
    li.insertBefore(body, li.firstChild);
  };

  root.querySelectorAll('ol').forEach((ol) => {
    let index = 1;
    Array.from(ol.children).forEach((li) => {
      if (li.tagName !== 'LI') return;
      const mark = document.createElement('i');
      mark.className = 'rp-num';
      mark.textContent = `${index++}.`;
      markItem(li, mark);
    });
  });

  root.querySelectorAll('ul').forEach((ul) => {
    Array.from(ul.children).forEach((li) => {
      if (li.tagName !== 'LI') return;
      const mark = document.createElement('i');
      mark.className = 'rp-dot';
      markItem(li, mark);
    });
  });

  const box = document.createElement('div');
  box.appendChild(root);
  return box.innerHTML;
}

function span(s: Summary) {
  return s.dateTo && s.dateTo !== s.dateFrom ? `${s.dateFrom} - ${s.dateTo}` : s.dateFrom;
}

function metaLine(s: Summary) {
  const parts: string[] = [];
  const st = s.stats;
  if (st?.recordings !== undefined) parts.push(`${st.recordings} 段录音`);
  if (st?.durationMs) {
    const m = Math.round(st.durationMs / 60000);
    parts.push(m >= 60 ? `${Math.floor(m / 60)} 小时 ${m % 60} 分` : `${m} 分钟`);
  }
  if (st?.chars) parts.push(`${(st.chars / 1000).toFixed(1)} 千字`);
  return parts.map((p) => `<span>·</span>${escapeHtml(p)}`).join('');
}

/** 把一份纪要拼成适合截图分享的长图 HTML */
export function summaryToPosterHtml(s: Summary): string {
  const scopeLabel = s.scope === 'day' ? '每日回忆' : s.scope === 'range' ? '时段回忆' : '录音纪要';

  const raw = marked.parse(s.markdown ?? '', { async: false }) as string;
  const clean = decorateLists(
    DOMPurify.sanitize(raw, { ADD_ATTR: ['target', 'rel', 'checked', 'disabled', 'type'] }),
  );

  const highlights = (s.stats?.highlights ?? []).slice(0, 8);
  const moods = (s.stats?.moods ?? []).slice(0, 8);

  const li = (inner: string) =>
    `<li><span class="rp-li"><i class="rp-dot"></i>${inner}</span></li>`;

  const boxes = [
    highlights.length
      ? `<div class="rp-box"><div class="rp-box-t">关键信息</div><ul>${highlights
          .map((h) => li(escapeHtml(h)))
          .join('')}</ul></div>`
      : '',
    moods.length
      ? `<div class="rp-box"><div class="rp-box-t">人物与心情</div><ul>${moods
          .map((m) =>
            li(
              `<b>${escapeHtml(m.person)}</b> — ${escapeHtml(m.mood)}` +
                (m.evidence ? `<span style="color:#8a8f9c">（${escapeHtml(m.evidence)}）</span>` : ''),
            ),
          )
          .join('')}</ul></div>`
      : '',
  ].join('');

  return `<div class="rp">
<div class="rp-hd">
  <div class="rp-tag">${escapeHtml(scopeLabel)} · ${escapeHtml(span(s))}</div>
  <h1 class="rp-title">${escapeHtml(s.title || '未命名纪要')}</h1>
  <div class="rp-meta">${metaLine(s)}</div>
</div>
<div class="rp-rule"></div>
<div class="rp-body">
  ${boxes}
  <div class="rp-md">${clean}</div>
</div>
<div class="rp-ft">
  <span class="rp-ft-r">${escapeHtml(span(s))}</span>
  <span><b>忆录</b> · AI 整理的回忆</span>
</div>
</div>`;
}

export type PosterResult = { blob: Blob; width: number; height: number; url: string };

/**
 * 把长图 HTML 渲染成 PNG。
 *
 * 走 html2canvas：它自己遍历 DOM 画 canvas，不碰 SVG foreignObject ——
 * 后者会被 Chromium 判定为跨域污染，`canvas.toBlob` 直接抛 Tainted。
 * 超长内容按画布像素上限自动降采样（iOS Safari 大约 1600 万像素就画不出来了）。
 */
export async function htmlToPng(html: string, width = POSTER_WIDTH): Promise<PosterResult> {
  ensureStyles();

  const holder = document.createElement('div');
  holder.style.cssText =
    `position:fixed;left:0;top:0;width:${width}px;z-index:-1;pointer-events:none;` +
    `background:#ffffff;overflow:hidden;`;
  holder.innerHTML = html;
  document.body.appendChild(holder);

  try {
    const node = holder.firstElementChild as HTMLElement | null;
    if (!node) throw new Error('长图内容为空');

    const height = Math.ceil(node.getBoundingClientRect().height);
    if (!height) throw new Error('长图内容为空');

    const MAX_PIXELS = 16_000_000;
    const scale = Math.max(0.6, Math.min(2, Math.sqrt(MAX_PIXELS / (width * height))));

    const canvas = await html2canvas(node, {
      backgroundColor: '#ffffff',
      width,
      height,
      windowWidth: width,
      windowHeight: height,
      scale,
      logging: false,
      useCORS: false,
    });

    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob((b) => resolve(b), 'image/png'),
    );
    if (!blob) throw new Error('长图生成失败，请重试');
    return { blob, width: canvas.width, height: canvas.height, url: URL.createObjectURL(blob) };
  } finally {
    holder.remove();
  }
}

/** 分享用的 Markdown 全文（含标题、日期与思维导图大纲） */
export function summaryToMarkdown(s: Summary) {
  const mindmap = (s.mindmap ?? '').trim();
  return [
    `# ${s.title}`,
    '',
    `> ${span(s)}`,
    '',
    (s.markdown ?? '').trim(),
    ...(mindmap ? ['', '---', '', '## 思维导图大纲', '', '```markdown', mindmap, '```'] : []),
    '',
  ].join('\n');
}