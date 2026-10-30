import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Maximize2, Minus, Plus, RotateCcw } from 'lucide-react';

type TreeNode = {
  id: number;
  text: string;
  depth: number;
  children: TreeNode[];
  collapsed: boolean;
};

type LaidNode = TreeNode & {
  w: number;
  h: number;
  x: number;
  y: number;
  parent: LaidNode | null;
  color: string;
  descendants: number;
};

const PALETTE = ['#5b5bd6', '#0ea5e9', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6', '#14b8a6', '#f43f5e'];

/* ---------------- markdown 大纲解析 ---------------- */
function parseOutline(markdown: string): TreeNode {
  const lines = markdown.split('\n');
  const flat: { text: string; depth: number }[] = [];

  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const head = line.match(/^(#{1,6})\s+(.*)$/);
    if (head) {
      flat.push({ text: clean(head[2]), depth: head[1].length });
      continue;
    }
    const item = line.match(/^([\s]*)[-*+]\s+(?:\[.\]\s*)?(.*)$/);
    if (item) {
      const indent = Math.floor(item[1].replace(/\t/g, '  ').length / 2);
      flat.push({ text: clean(item[2]), depth: 99 + indent }); // 列表项放在最后，稍后归一化
    }
  }

  // 把 99+ 的列表层级压到标题层级之后
  const maxHead = Math.max(1, ...flat.filter((f) => f.depth < 99).map((f) => f.depth));
  for (const f of flat) if (f.depth >= 99) f.depth = maxHead + (f.depth - 99) + 1;

  if (flat.length === 0) {
    return { id: 0, text: '（空）', depth: 1, children: [], collapsed: false };
  }

  // 若顶层为多个节点，用一个虚拟根包起来
  const baseDepth = Math.min(...flat.map((f) => f.depth));
  const roots = flat.filter((f) => f.depth === baseDepth);
  let root: TreeNode;
  let seq = 1;

  if (roots.length === 1) {
    root = { id: seq++, text: roots[0].text, depth: 1, children: [], collapsed: false };
  } else {
    root = { id: seq++, text: '思维导图', depth: 1, children: [], collapsed: false };
  }

  const stack: TreeNode[] = [root];
  for (const item of flat) {
    if (item === roots[0] && roots.length === 1) continue;
    const depth = item.depth - baseDepth + 1;
    while (stack.length >= depth && stack.length > 1) stack.pop();
    const parent = stack[stack.length - 1] ?? root;
    const node: TreeNode = { id: seq++, text: item.text, depth: parent.depth + 1, children: [], collapsed: false };
    parent.children.push(node);
    stack.push(node);
  }
  return root;
}

function clean(s: string) {
  return s
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/\[(.*?)\]\(.*?\)/g, '$1')
    .trim()
    .slice(0, 60);
}

/* ---------------- 文本测量 ---------------- */
let measureCtx: CanvasRenderingContext2D | null = null;
function measureText(text: string, fontSize: number): number {
  if (!measureCtx) {
    measureCtx = document.createElement('canvas').getContext('2d');
  }
  if (!measureCtx) return text.length * fontSize * 0.9;
  measureCtx.font = `${fontSize}px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif`;
  return measureCtx.measureText(text).width;
}

const H_GAP = 46;
const V_GAP = 12;
const NODE_H = 28;
const ROOT_H = 36;

/* ---------------- 布局 ---------------- */
function layoutTree(root: TreeNode, collapsedIds: Set<number>): { nodes: LaidNode[]; links: [LaidNode, LaidNode][]; bbox: any; root: LaidNode } {
  const nodes: LaidNode[] = [];
  const links: [LaidNode, LaidNode][] = [];
  let seq = 0;
  let cursorY = 0;

  const build = (node: TreeNode, depth: number, parent: LaidNode | null, color: string): LaidNode => {
    const isRoot = depth === 0;
    const fontSize = isRoot ? 15 : depth === 1 ? 13.5 : 13;
    const w = measureText(node.text, fontSize) + (isRoot ? 34 : 20);
    const h = isRoot ? ROOT_H : NODE_H;
    const laid: LaidNode = {
      ...node,
      w,
      h,
      x: 0,
      y: 0,
      parent,
      color,
      descendants: 0,
    };
    laid.id = seq++;
    nodes.push(laid);

    const kids = node.collapsed || collapsedIds.has(node.id) ? [] : node.children;
    if (kids.length === 0) {
      laid.y = cursorY + h / 2;
      cursorY += h + V_GAP;
    } else {
      const kidsLaid = kids.map((c, i) => build(c, depth + 1, laid, depth === 0 ? PALETTE[i % PALETTE.length] : color));
      laid.y = (kidsLaid[0].y + kidsLaid[kidsLaid.length - 1].y) / 2;
      laid.descendants = kidsLaid.length + kidsLaid.reduce((a, k) => a + k.descendants, 0);
    }
    return laid;
  };

  const rootLaid = build(root, 0, null, PALETTE[0]);

  // 按层计算 x
  const maxW = new Map<number, number>();
  const depthOf = (n: LaidNode) => {
    let d = 0;
    let p = n.parent;
    while (p) { d++; p = p.parent; }
    return d;
  };
  for (const n of nodes) {
    const d = depthOf(n);
    maxW.set(d, Math.max(maxW.get(d) ?? 0, n.w));
  }
  const xAt = new Map<number, number>();
  let acc = 0;
  const depthCount = maxW.size;
  for (let d = 0; d < depthCount; d++) {
    xAt.set(d, acc);
    acc += (maxW.get(d) ?? 0) + H_GAP;
  }
  for (const n of nodes) {
    const d = depthOf(n);
    n.x = xAt.get(d) ?? 0;
    if (n.parent) links.push([n.parent, n]);
  }

  const minX = 0;
  const minY = 0;
  const maxX = Math.max(...nodes.map((n) => n.x + n.w));
  const maxY = Math.max(...nodes.map((n) => n.y + n.h / 2)) + 8;

  return { nodes, links, bbox: { minX, minY, maxX, maxY }, root: rootLaid };
}

/* ---------------- 组件 ---------------- */
export default function MindMap({ markdown }: { markdown: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [viewBox, setViewBox] = useState({ x: 0, y: 0, w: 1000, h: 600 });
  const fittedRef = useRef(false);
  const dragRef = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);

  const outline = useMemo(() => {
    try {
      return parseOutline(markdown || '');
    } catch {
      return parseOutline('');
    }
  }, [markdown]);

  const { nodes, links, bbox } = useMemo(() => layoutTree(outline, collapsed), [outline, collapsed]);

  const fit = () => {
    const el = containerRef.current;
    if (!el) return;
    const cw = el.clientWidth;
    const ch = el.clientHeight;
    const pad = 28;
    const w = bbox.maxX + pad * 2;
    const h = bbox.maxY + pad * 2;
    const aspect = cw / ch;
    const contentAspect = w / h;
    let vw = w;
    let vh = h;
    if (contentAspect > aspect) {
      vh = w / aspect;
    } else {
      vw = h * aspect;
    }
    setViewBox({
      x: (w - vw) / 2 - pad * 0,
      y: (h - vh) / 2,
      w: vw,
      h: vh,
    });
  };

  useLayoutEffect(() => {
    fittedRef.current = false;
    const id = requestAnimationFrame(() => {
      fit();
      fittedRef.current = true;
    });
    return () => cancelAnimationFrame(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outline]);

  // 折叠后保持视图
  useEffect(() => {
    if (!fittedRef.current) return;
    const t = setTimeout(fit, 30);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collapsed]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => fit());
    ro.observe(el);
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bbox.maxX, bbox.maxY]);

  const zoom = (factor: number) => {
    setViewBox((v) => {
      const nw = v.w * factor;
      const nh = v.h * factor;
      return { x: v.x + (v.w - nw) / 2, y: v.y + (v.h - nh) / 2, w: nw, h: nh };
    });
  };

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    const factor = e.deltaY > 0 ? 1.1 : 0.9;
    zoom(factor);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as Element).setPointerCapture?.(e.pointerId);
    dragRef.current = { x: e.clientX, y: e.clientY, vx: viewBox.x, vy: viewBox.y };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    const el = containerRef.current;
    if (!d || !el) return;
    const rect = el.getBoundingClientRect();
    const dx = ((e.clientX - d.x) / rect.width) * viewBox.w;
    const dy = ((e.clientY - d.y) / rect.height) * viewBox.h;
    setViewBox((v) => ({ ...v, x: d.vx - dx, y: d.vy - dy }));
  };
  const onPointerUp = () => { dragRef.current = null; };

  const toggle = (n: LaidNode) => {
    if (n.children.length === 0) return;
    setCollapsed((prev) => {
      const next = new Set(prev);
      const isCollapsed = prev.has(n.id) || n.collapsed;
      if (isCollapsed) next.delete(n.id);
      else next.add(n.id);
      return next;
    });
  };

  const depthOf = (n: LaidNode) => {
    let d = 0;
    let p = n.parent;
    while (p) { d++; p = p.parent; }
    return d;
  };

  return (
    <div className="relative">
      <div
        ref={containerRef}
        className="h-[62vh] min-h-[400px] rounded-2xl border border-line bg-panel overflow-hidden select-none touch-none"
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
      >
        <svg
          className="w-full h-full block"
          viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`}
          preserveAspectRatio="xMidYMid meet"
        >
          <defs>
            <linearGradient id="mm-root" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="var(--brand)" />
              <stop offset="100%" stopColor="var(--brand-2)" />
            </linearGradient>
          </defs>

          {/* 连线 */}
          {links.map(([p, c], i) => {
            const x1 = p.x + p.w;
            const y1 = p.y;
            const x2 = c.x;
            const y2 = c.y;
            const mx = x1 + (x2 - x1) * 0.5;
            return (
              <path
                key={`l${i}`}
                d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                fill="none"
                stroke={c.color}
                strokeWidth={1.6}
                strokeOpacity={0.42}
                strokeLinecap="round"
              />
            );
          })}

          {/* 节点 */}
          {nodes.map((n) => {
            const d = depthOf(n);
            const isRoot = d === 0;
            const isBranch = d === 1;
            const hasKids = n.children.length > 0;
            const isCollapsed = collapsed.has(n.id);
            const fs = isRoot ? 15 : isBranch ? 13.5 : 13;

            return (
              <g
                key={n.id}
                transform={`translate(${n.x}, ${n.y - n.h / 2})`}
                className={hasKids ? 'cursor-pointer' : 'cursor-default'}
                onClick={(e) => { e.stopPropagation(); toggle(n); }}
              >
                {isRoot && (
                  <rect x={0} y={0} width={n.w} height={n.h} rx={12} fill="url(#mm-root)" />
                )}

                <text
                  x={isRoot ? n.w / 2 : 10}
                  y={n.h / 2}
                  textAnchor={isRoot ? 'middle' : 'start'}
                  dominantBaseline="central"
                  fontSize={fs}
                  fontWeight={isRoot || isBranch ? 600 : 400}
                  fill={isRoot ? '#ffffff' : isBranch ? n.color : 'var(--text)'}
                  style={{ fontFamily: 'inherit' }}
                >
                  {n.text}
                </text>

                {/* 折叠标记 */}
                {hasKids && (
                  <g transform={`translate(${n.w + (isRoot ? 8 : 6)}, ${n.h / 2})`}>
                    <circle r={isCollapsed ? 5 : 3.6} fill={isRoot ? n.color : n.color} fillOpacity={isCollapsed ? 1 : 0.75} />
                    {isCollapsed && (
                      <text
                        x={11}
                        y={0}
                        dominantBaseline="central"
                        fontSize={10}
                        fill="var(--muted)"
                      >
                        +{n.descendants || n.children.length}
                      </text>
                    )}
                  </g>
                )}
              </g>
            );
          })}
        </svg>
      </div>

      <div className="absolute right-3 bottom-3 flex flex-col gap-1.5">
        <button
          onClick={() => zoom(0.8)}
          className="h-8 w-8 grid place-items-center rounded-lg bg-panel/90 border border-line backdrop-blur text-muted hover:text-ink shadow-soft"
          title="放大"
        >
          <Plus size={14} />
        </button>
        <button
          onClick={() => zoom(1.25)}
          className="h-8 w-8 grid place-items-center rounded-lg bg-panel/90 border border-line backdrop-blur text-muted hover:text-ink shadow-soft"
          title="缩小"
        >
          <Minus size={14} />
        </button>
        <button
          onClick={fit}
          className="h-8 w-8 grid place-items-center rounded-lg bg-panel/90 border border-line backdrop-blur text-muted hover:text-ink shadow-soft"
          title="适应窗口"
        >
          <Maximize2 size={13} />
        </button>
        <button
          onClick={() => setCollapsed(new Set())}
          className="h-8 w-8 grid place-items-center rounded-lg bg-panel/90 border border-line backdrop-blur text-muted hover:text-ink shadow-soft"
          title="全部展开"
        >
          <RotateCcw size={13} />
        </button>
      </div>

      <div className="absolute left-3 bottom-3 text-[10.5px] text-muted/60">
        滚轮缩放 · 拖拽平移 · 点击分支折叠
      </div>
    </div>
  );
}
