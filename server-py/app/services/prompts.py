"""纪要整理提示词。

五种单条录音风格 + 四种回忆周期（日 / 周 / 月 / 年）+ 任意时段；
用户还可以在设置里为每个周期分别写「自定义要求」，它会替换掉风格的描述部分，
但硬性规则与 JSON 输出契约始终保留（否则前端解析不出 markdown / 思维导图）。
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date

TEMPLATE_LABEL: dict[str, str] = {
    "meeting": "会议纪要",
    "lecture": "课程/讲座笔记",
    "interview": "访谈记录",
    "idea": "灵感与想法",
    "general": "内容纪要",
    "daily": "每日回忆",
    "week": "每周回忆",
    "month": "每月回忆",
    "year": "年度回忆",
    "period": "时段回忆",
}

# 回忆类（日报 / 周报 / 月报 / 年报 / 任意时段）：都不许写成会议纪要
RECALL_TEMPLATES = {"daily", "week", "month", "year", "period"}

# 待办标签：AI 整理时从中选一个，行动页按这些标签筛选
TODO_TAGS: tuple[str, ...] = ("工作", "生活", "学习", "灵感")

# 每个 scope 的默认风格：日报/时段汇总走「回忆」，单条录音沿用会议纪要
SCOPE_DEFAULT_TEMPLATE: dict[str, str] = {
    "recording": "meeting",
    "day": "daily",
    "range": "period",
}

# 区间类型 → 默认风格。周/月/年各自有独立结构与提示词
PERIOD_TEMPLATE: dict[str, str] = {
    "recording": "meeting",
    "day": "daily",
    "week": "week",
    "month": "month",
    "year": "year",
    "range": "period",
}

# 风格 → 设置里的提示词字段。单条录音的几套风格共用一个字段（沿用老的 summaryPrompt）
PROMPT_FIELDS: dict[str, str] = {
    "meeting": "summaryPrompt",
    "lecture": "summaryPrompt",
    "interview": "summaryPrompt",
    "idea": "summaryPrompt",
    "general": "summaryPrompt",
    "daily": "promptDay",
    "week": "promptWeek",
    "month": "promptMonth",
    "year": "promptYear",
    "period": "promptRange",
}

PERIOD_LABEL: dict[str, str] = {
    "recording": "单条录音",
    "day": "日报",
    "week": "周报",
    "month": "月报",
    "year": "年报",
    "range": "任意时段",
}


def _month_end(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def period_of(scope: str, date_from: str, date_to: str) -> str:
    """从区间判断这是日报 / 周报 / 月报 / 年报 / 任意时段。

    日历页传上来的区间就是这几种：单日、周一到周日、整月 1 号到月末、整年 1/1 到 12/31。
    判断不出来的一律按「任意时段」处理。
    """
    if scope == "recording":
        return "recording"
    if scope == "day" or date_from == date_to:
        return "day"
    try:
        d1 = date.fromisoformat(date_from)
        d2 = date.fromisoformat(date_to)
    except ValueError:
        return "range"

    if d1.year == d2.year and d1.month == 1 and d1.day == 1 and d2.month == 12 and d2.day == 31:
        return "year"
    if d1.year == d2.year and d1.month == d2.month and d1.day == 1 and d2 == _month_end(d1):
        return "month"
    if d1.weekday() == 0 and (d2 - d1).days == 6:
        return "week"
    return "range"


def default_template_for(scope: str, date_from: str, date_to: str) -> str:
    return PERIOD_TEMPLATE.get(period_of(scope, date_from, date_to), "period")


def prompt_field(template: str | None) -> str:
    return PROMPT_FIELDS.get(template or "meeting", "summaryPrompt")

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
    "daily": {
        "label": "每日回忆",
        "focus": (
            "这是同一天里多段录音的整体回顾，可能是闲聊、工作沟通、电话、碎碎念，"
            "不是一场会议。请像写一天的日记那样整理：这一天主要聊了什么、发生了哪些事、"
            "冒出过什么念头、哪些事还没办、相关的人心情怎么样。"
            "保留生活感和细节，不要套用「议题 / 与会者 / 决议」这类会议措辞。"
        ),
        "outline": "## 今天都聊了什么\n## 冒出来的想法\n## 待办事项\n## 人物与心情",
        "tips": (
            "「今天都聊了什么」按话题或时间顺序分段，每段写清：跟谁、聊了什么事、结果如何；"
            "「人物与心情」只写原文明确出现的人名或称呼，心情必须能从语气、用词或原话看出来，判断不了就写「未表露」，禁止臆测；"
            "标题写成一句有画面感的话（如「跟老张聊到半夜，决定先缓缓」），不要写成「X月X日日报」。"
        ),
    },
    "week": {
        "label": "每周回忆",
        "focus": (
            "这是同一周里多段录音的回顾，不是一场会议。像写周记那样整理："
            "这周主要在忙什么、几天里反复出现的人和事、哪些念头值得留下、还有哪些事没做完。"
            "保留生活节奏感，不要套用「议题 / 与会者 / 决议」这类会议措辞。"
        ),
        "outline": "## 这周在忙什么\n## 值得记住的片段\n## 待办事项\n## 人物与心情",
        "tips": (
            "按「天」或「话题」推进，不要逐条罗列录音；同一件事跨几天就合起来写，写清变化；"
            "「人物与心情」只写原文明确出现的人名或称呼，心情必须能从语气、用词或原话看出来，判断不了就写「未表露」，禁止臆测；"
            "标题写成一句能概括这周状态的话（如「赶完方案，周末补了一觉」），不要写成「X月X日-X月X日周报」。"
        ),
    },
    "month": {
        "label": "每月回忆",
        "focus": (
            "这是一个月里多段录音的回顾，不是一场会议。像写月记那样整理："
            "这个月的主线是什么、有哪些阶段性的事、反复出现的人和情绪、哪些待办一直挂着。"
            "允许压缩细节，但不要写成流水账或工作汇报。"
        ),
        "outline": "## 这个月在忙什么\n## 几件重要的事\n## 一直挂着的待办\n## 人物与心情",
        "tips": (
            "先用一段话概括这个月的状态，再按主线分点；同一主题跨多天就合并，只保留有辨识度的细节；"
            "「人物与心情」只写原文有依据的，判断不了就写「未表露」，禁止臆测；"
            "标题概括这个月的关键词（如「搬完家，开始适应新节奏」），不要写成「X月月报」。"
        ),
    },
    "year": {
        "label": "年度回忆",
        "focus": (
            "这是一整年多段录音的回顾，不是一场会议。像写年终总结里最私人的那一部分："
            "这一年的大主线、几个重要阶段、反复出现的人、拖了很久的事、心态的变化。"
            "不要罗列每个月做了什么，要抓主干和转折。"
        ),
        "outline": "## 这一年的主线\n## 几个重要阶段\n## 还没做完的事\n## 人物与心情",
        "tips": (
            "「几个重要阶段」按时间顺序写 3-6 段，每段用一两句话说清发生了什么、带来了什么变化；"
            "细节只留能代表这一年的，其余概括；"
            "「人物与心情」只写原文有依据的，判断不了就写「未表露」，禁止臆测；"
            "标题写成一句年度侧写（如「换了两座城市，慢慢安顿下来」），不要写成「2026 年度报告」。"
        ),
    },
    "period": {
        "label": "时段回忆",
        "focus": (
            "这是跨越多天的录音回顾，不是一场会议。请按天或按主题串联，"
            "讲清这段时间里持续在聊的事、出现的人、情绪起伏、累积下来的待办。"
        ),
        "outline": "## 这段时间在忙什么\n## 值得记住的片段\n## 待办事项\n## 人物与心情",
        "tips": (
            "多天内容按「天 / 主题」分组，不要逐条罗列录音；"
            "人物心情只写原文有依据的，判断不了就写「未表露」；"
            "标题概括这段时间的状态，不要写成「X月X日至X月X日汇总」。"
        ),
    },
}

RULES = """硬性要求：
1. 只使用原文真实出现的信息，绝不编造人名、数字、时间、结论。原文没有的内容就不写。
2. 修正明显的同音错别字，删除口语重复（"就是说""那个"），但保留原意和说话人立场。
3. 待办事项必须有原文依据；每条待办必须带一个 tag，取值为「工作 / 生活 / 学习 / 灵感」之一：工作=工作事务、项目、客户、协作，生活=家务、购物、健康、家人、出行、缴费，学习=课程、读书、练习、复习、研究，灵感=想法、创意、选题、产品点子；拿不准就选最贴近的一类，不要留空。有明确负责人时写入 owner，没有则留空字符串；没有待办就给空数组。
4. 整理类型为「每日 / 每周 / 每月 / 年度 / 时段回忆」时，禁止写成会议纪要形式：不要出现「会议信息」「议题」「决议」「与会者」这类小节和措辞。
5. moods 里的人物必须是原文明确出现的人名或称呼；心情必须由原文语气、用词或原话支撑，判断不了就写「未表露」，绝不臆测。
6. 只输出 JSON，不要代码围栏，不要任何解释性文字。
7. 原文为空、只有噪音，或只有零碎文字（语气词、重复音节、无法串联的词句）而拼不出实际话题时，contentful 写 false，title 写「无有效内容」，summary 说明原因，markdown 保持简短，highlights、todos、moods 给空数组；其余情况 contentful 写 true。"""

JSON_CONTRACT = """JSON 结构：
{
  "contentful": true,
  "title": "不超过 20 字的标题",
  "summary": "80-180 字的核心概要，一句话说清这段内容在讲什么、得出了什么",
  "markdown": "完整纪要（Markdown）。用 ## 二级标题分节，章节按【纪要结构】执行；要点用 - 列表，待办用 - [ ]，关键数字和结论加粗。",
  "mindmap": "思维导图大纲。第一行 # 主题；第二行起用 ## 表示 3-7 个主分支；每个主分支下用 ### 列 2-5 个要点。只使用 # 标题层级，禁止使用 - 或 * 列表符号。层级不超过三级。",
  "highlights": ["3-6 条最值得记住的信息，每条不超过 30 字"],
  "todos": [{"text": "待办内容", "tag": "工作", "owner": "", "due": ""}],
  "moods": [{"person": "人物名或称呼", "mood": "心情概括，如「焦虑但释然」", "evidence": "支撑该判断的原话或行为，不超过 30 字"}]
}
moods 只给有明显情绪线索的人，最多 6 条；没有就给空数组。
todos 的 tag 只能是「工作」「生活」「学习」「灵感」之一，每条都要带。
contentful 只填布尔值 true / false，表示原文里有没有可整理的实际话题。"""


def _style(template: str | None) -> dict[str, str]:
    return STYLES.get(template or "meeting", STYLES["meeting"])


def _structure_block(style: dict[str, str]) -> str:
    return (
        "【纪要结构】markdown 字段按这些二级标题组织（原文确实没有内容的小节可以省略，顺序保持一致）：\n"
        f"{style['outline']}\n"
        f"【写作提醒】{style['tips']}"
    )


def build_system_prompt(
    template: str | None = None, custom: str | None = None, hierarchical: bool = False
) -> str:
    """拼出一次整理用的 system prompt。

    custom 非空时作为「用户自定义要求」，替换掉风格描述部分；规则与 JSON 契约照旧。
    hierarchical 表示输入是下级回忆而非口语转写原文（层级式汇总）。
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
        lead = (
            "你是「忆录」的资深内容整理助手。输入是已经整理好的下级回忆"
            "（由语音转写逐层汇总而来），请在此基础上做更高一层的归纳，不要退回逐条复述。"
            if hierarchical
            else "你是「忆录」的资深内容整理助手。输入是语音识别得到的口语文本，"
            "可能包含错别字、口头禅、重复和断句错误。"
        )
        if template in RECALL_TEMPLATES:
            unit = {"daily": "天", "week": "周", "month": "个月", "year": "年"}.get(template or "", "段")
            lead += f"请把这一{unit}的录音整理成一份有温度的回忆式纪要，并同时输出思维导图大纲。"
        else:
            lead += "请整理成结构化纪要，并同时输出思维导图大纲。"
        parts.append(lead)
        parts.append(f"【本次整理类型】{style['label']}")
        parts.append(f"【侧重点】{style['focus']}")

    parts.append(_structure_block(style))
    parts.append(RULES)
    parts.append(JSON_CONTRACT)
    return "\n\n".join(parts)


def build_chunk_prompt(template: str | None = None, hierarchical: bool = False) -> str:
    """长文本分段提炼用的提示词（map 阶段）。"""
    style = _style(template)
    extra = ""
    if template in RECALL_TEMPLATES:
        extra = "另外保留情绪线索：谁在什么话题上表现出什么心情，以及支撑它的原话或行为。"
    material = "下级回忆材料" if hierarchical else "口语转写"
    return (
        f"你是内容提炼助手。请把这段{material}浓缩成条目化要点，"
        "保留所有人名、数字、决定、待办和时间点，不要编造。"
        f"这段材料最终要整理成「{style['label']}」，提炼时优先保留与之相关的信息。{extra}"
        "只输出要点列表。"
    )


REVISE_RULES = """硬性要求：
1. 只使用「当前纪要」和「依据材料」里出现过的信息，绝不编造人名、数字、时间或结论。材料里没有的内容不要新增。
2. 按用户的修改要求改；要求里没有提到的部分保持原样，不要顺手重写整篇。
3. 保留纪要原有的章节结构和写作风格，只调整与要求相关的部分。
4. 待办事项必须带 tag，取值为「工作 / 生活 / 学习 / 灵感」之一；没有待办就给空数组。
5. moods 只保留当前纪要里已有、且原文有依据的人物；不要借修改之名新增情绪判断。
6. 只输出 JSON，不要代码围栏，不要任何解释性文字。"""


def build_revise_system_prompt() -> str:
    """「按用户要求修改这一篇纪要」的 system prompt（与生成时的风格无关，只改不改）。

    不走 STYLES：修改必须保留原纪要自己的结构，风格提示词反而会诱导模型重写。
    """
    return (
        "你是「忆录」的纪要修订助手。用户会对一份已经生成好的纪要提出修改要求，"
        "请在当前纪要的基础上按要求修订，输出与生成时完全相同的 JSON 结构。\n\n"
        + REVISE_RULES
        + "\n\n" + JSON_CONTRACT)
