import type { Provider } from '../db/index.js';

export type ChatMessage = { role: 'system' | 'user' | 'assistant'; content: string };
export type ChatOptions = { json?: boolean; temperature?: number; maxTokens?: number };

export interface LlmEngine {
  chat(messages: ChatMessage[], opts?: ChatOptions): Promise<string>;
}

function params(p: Provider): Record<string, any> {
  try {
    return p.params ? JSON.parse(p.params) : {};
  } catch {
    return {};
  }
}

function normalizeRoot(baseUrl: string | null, fallback: string) {
  const base = (baseUrl || fallback).replace(/\/+$/, '');
  return base.endsWith('/v1') ? base : `${base}/v1`;
}

class OpenAiCompatibleLlm implements LlmEngine {
  constructor(private p: Provider) {}

  async chat(messages: ChatMessage[], opts: ChatOptions = {}): Promise<string> {
    if (!this.p.api_key) throw new Error('缺少 API Key');
    const root = normalizeRoot(this.p.base_url, 'https://api.openai.com/v1');
    const cfg = params(this.p);
    const body: any = {
      model: this.p.model || 'gpt-4o-mini',
      messages,
      temperature: opts.temperature ?? cfg.temperature ?? 0.3,
      stream: false,
    };
    if (opts.maxTokens ?? cfg.maxTokens) body.max_tokens = opts.maxTokens ?? cfg.maxTokens;
    if (opts.json && cfg.supportsJsonMode !== false) body.response_format = { type: 'json_object' };

    const res = await fetch(`${root}/chat/completions`, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.p.api_key}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    });
    const raw = await res.text();
    if (!res.ok) throw new Error(`LLM 请求失败 ${res.status}: ${raw.slice(0, 400)}`);
    const json: any = JSON.parse(raw);
    const content = json?.choices?.[0]?.message?.content;
    if (typeof content !== 'string') throw new Error('LLM 返回格式异常：缺少 choices[0].message.content');
    return content;
  }
}

class AnthropicLlm implements LlmEngine {
  constructor(private p: Provider) {}

  async chat(messages: ChatMessage[], opts: ChatOptions = {}): Promise<string> {
    if (!this.p.api_key) throw new Error('缺少 API Key');
    const base = (this.p.base_url || 'https://api.anthropic.com').replace(/\/+$/, '');
    const system = messages.filter((m) => m.role === 'system').map((m) => m.content).join('\n\n');
    const rest = messages
      .filter((m) => m.role !== 'system')
      .map((m) => ({ role: m.role, content: m.content }));

    const res = await fetch(`${base}/v1/messages`, {
      method: 'POST',
      headers: {
        'x-api-key': this.p.api_key,
        'anthropic-version': '2023-06-01',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: this.p.model || 'claude-sonnet-4-20250514',
        max_tokens: opts.maxTokens ?? params(this.p).maxTokens ?? 4096,
        temperature: opts.temperature ?? 0.3,
        system: system || undefined,
        messages: rest,
      }),
    });
    const raw = await res.text();
    if (!res.ok) throw new Error(`LLM 请求失败 ${res.status}: ${raw.slice(0, 400)}`);
    const json: any = JSON.parse(raw);
    const text = Array.isArray(json.content)
      ? json.content.map((c: any) => (c.type === 'text' ? c.text : '')).join('')
      : '';
    return text;
  }
}

class MockLlm implements LlmEngine {
  async chat(messages: ChatMessage[], opts: ChatOptions = {}): Promise<string> {
    const userText = messages.filter((m) => m.role === 'user').map((m) => m.content).join('\n');
    const snippet = userText.replace(/\s+/g, ' ').slice(0, 120);
    if (opts.json) {
      return JSON.stringify({
        title: '演示纪要',
        markdown: [
          '## 核心概要',
          '这是一份由演示引擎生成的示例纪要。配置真实的语言模型后，这里会基于录音转写内容自动总结。',
          '',
          '## 关键议题',
          '- 上传与分片转写链路已验证可用',
          '- 纪要生成与思维导图渲染链路已验证可用',
          '- 完成服务商配置后即可切换到真实模型',
          '',
          '## 待办事项',
          '- [ ] 在「设置 → AI 服务商」中填入真实 API Key',
          '- [ ] 用一段真实录音做端到端验证',
          '',
          `> 原始内容片段：${snippet}`,
        ].join('\n'),
        mindmap: [
          '# 演示纪要',
          '## 录音转写',
          '### 分片上传',
          '### 自动转写',
          '## AI 整理',
          '### 纪要生成',
          '### 思维导图',
          '## 后续动作',
          '### 配置真实服务商',
        ].join('\n'),
        highlights: ['链路已打通', '等待真实服务商'],
      });
    }
    return `演示回复：${snippet}`;
  }
}

export function createLlmEngine(p: Provider): LlmEngine {
  switch (p.kind) {
    case 'openai-compat':
      return new OpenAiCompatibleLlm(p);
    case 'anthropic':
      return new AnthropicLlm(p);
    case 'mock':
      return new MockLlm();
    default:
      throw new Error(`未知的 LLM 引擎类型：${p.kind}`);
  }
}

export const LLM_KINDS = [
  { kind: 'openai-compat', label: 'OpenAI 兼容接口', hint: 'DeepSeek / 通义千问 / Moonshot / 硅基流动 / 各类中转站，填 Base URL + 模型名即可。' },
  { kind: 'anthropic', label: 'Anthropic Claude', hint: '官方 API 或兼容网关，使用 /v1/messages 协议。' },
  { kind: 'mock', label: '演示引擎（模拟数据）', hint: '不调用外部服务，用于验证整体流程。' },
];
