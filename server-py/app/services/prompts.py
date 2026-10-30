"""纪要整理提示词。

五种风格各有一套结构与侧重点；用户还可以在设置里写「自定义整理要求」，
它会替换掉风格的描述部分，但硬性规则与 JSON 输出契约始终保留
（否则前端解析不出 markdown / 思维导图）。
"""

from __future__ import annotations

TEMPLATE_LABEL: dict[str, str] = {
    "meeting": "会议纪要",
    "lecture": "课程/讲座笔记",
    "interview": "访谈记录",
    "idea": "灵感与想法",
    "general": "内容纪要",
}

STYLES: dict[str, dict[str, str]] = {
    "meeting": {
        "label": "会议纪要",
        "focus": "这是一段会议或讨论录音：重点是议题、各方观点、达成的结论，以及谁在什么时候做什么。",
        "outline": "## 会议信息\n## 讨论要点\n## 结论与决定\n## 待办事项",
        "tips": "参与人只写原文明确出现的名字；没有形成结论时写「未形成结论」，不要替与会者下判断。",
    },
    "lecture": {
        "label": "课程/讲座笔记",
        "focus": "这是一段课程或讲座录音：重点是把知识讲清楚，定义、原理、例子和易错点比「谁说了什么」更重要。",
        "outline": "## 本节主题\n## 核心概念\n## 详细知识点\n## 案例与例题\n## 复习要点",
        "tips": "核心概念用「术语：一句话解释」的形式；推导过程按步骤保留；听不懂或有争议的地方归到「复习要点」里标出来。",
    },
    "interview": {
        "label": "访谈记录",
        "focus": "这是一段访谈或对话录音：重点是还原受访者的观点和原话，而不是把对话压成结论。",
        "outline": "## 访谈对象与背景\n## 核心观点\n## 原话摘录\n## 事实与数据\n## 可追问的方向",
        "tips": "尽量区分提问者与受访者；「原话摘录」用引号保留受访者的原表述，不要改写成书面语；观点与事实分开写。",
    },
    "idea": {
        "label": "灵感与想法",
        "focus": "这是一段灵感或头脑风暴录音：重点是把零散的想法完整捞出来，不要急着归纳成会议决议。",
        "outline": "## 想法清单\n## 想法从哪来\n## 可行性与风险\n## 下一步",
        "tips": "一个想法一条，保留原始表述和情绪色彩；半成品、玩笑话里如果有价值也留着，标注「未成形」。",
    },
    "general": {
        "label": "内容纪要",
        "focus": "不预设场景，按内容本身的逻辑整理，抓住主线、要点和结论。",
        "outline": "## 核心概要\n## 关键内容\n## 结论\n## 待办事项",
        "tips": "内容更偏叙述或闲聊时，可以按话题重组小节，不必硬套待办。",
    },
}

RULES = """硬性要求：
1. 只使用原文真实出现的信息，绝不编造人名、数字、时间、结论。原文没有的内容就不写。
2. 修正明显的同音错别字，删除口语重复（"就是说""那个"），但保留原意和说话人立场。
3. 待办事项必须有原文依据；有明确负责人时写入 owner，没有则留空字符串；没有待办就给空数组。
4. 只输出 JSON，不要代码围栏，不要任何解释性文字。
5. 原文为空或只有噪音时，title 写「无有效内容」，summary 说明原因，markdown 保持简短，highlights 与 todos 给空数组。"""

JSON_CONTRACT = """JSON 结构：
{
  "title": "不超过 20 字的标题",
  "summary": "80-180 字的核心概要，一句话说清这段内容在讲什么、得出了什么",
  "markdown": "完整纪要（Markdown）。用 ## 二级标题分节，章节按【纪要结构】执行；要点用 - 列表，待办用 - [ ]，关键数字和结论加粗。",
  "mindmap": "思维导图大纲。第一行 # 主题；第二行起用 ## 表示 3-7 个主分支；每个主分支下用 ### 列 2-5 个要点。只使用 # 标题层级，禁止使用 - 或 * 列表符号。层级不超过三级。",
  "highlights": ["3-6 条最值得记住的信息，每条不超过 30 字"],
  "todos": [{"text": "待办内容", "owner": "", "due": ""}]
}"""


def _style(template: str | None) -> dict[str, str]:
    return STYLES.get(template or "meeting", STYLES["meeting"])


def _structure_block(style: dict[str, str]) -> str:
    return (
        "【纪要结构】markdown 字段按这些二级标题组织（原文确实没有内容的小节可以省略，顺序保持一致）：\n"
        f"{style['outline']}\n"
        f"【写作提醒】{style['tips']}"
    )


def build_system_prompt(template: str | None = None, custom: str | None = None) -> str:
    """拼出一次整理用的 system prompt。

    custom 非空时作为「用户自定义要求」，替换掉风格描述部分；规则与 JSON 契约照旧。
    """
    style = _style(template)
    parts: list[str] = []

    if custom and custom.strip():
        parts.append(
            "你是「忆录」的内容整理助手。用户给出了自定义整理要求，请优先遵守；"
            "与下面的硬性要求冲突时，以硬性要求为准。"
        )
        parts.append("【用户自定义要求】\n" + custom.strip()[:2000])
        parts.append(f"【本次整理类型】{style['label']}（仅作背景参考，用户要求优先）")
    else:
        parts.append(
            "你是「忆录」的资深内容整理助手。输入是语音识别得到的口语文本，"
            "可能包含错别字、口头禅、重复和断句错误。请整理成结构化纪要，并同时输出思维导图大纲。"
        )
        parts.append(f"【本次整理类型】{style['label']}")
        parts.append(f"【侧重点】{style['focus']}")

    parts.append(_structure_block(style))
    parts.append(RULES)
    parts.append(JSON_CONTRACT)
    return "\n\n".join(parts)


def build_chunk_prompt(template: str | None = None) -> str:
    """长文本分段提炼用的提示词（map 阶段）。"""
    style = _style(template)
    return (
        "你是内容提炼助手。请把这段口语转写浓缩成条目化要点，"
        "保留所有人名、数字、决定、待办和时间点，不要编造。"
        f"这段材料最终要整理成「{style['label']}」，提炼时优先保留与之相关的信息。"
        "只输出要点列表。"
    )
