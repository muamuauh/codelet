"""Intent router: make "just asking" turns read-only.

Enable in settings.json:
    {"plugins": {"enabled": ["router"], "config": {"router": {"llm": false}}}}

Each user turn is classified as question / plan / unclear / edit / command.
The first three make the turn read-only: tools that do not declare themselves
read-only are refused, with a message telling the model to describe the change
and ask. edit and command leave the turn exactly as it would be without the
router. A short confirmation after a restricted turn ("好的", "go ahead") turns
changes back on.

Why restrict, and why only then:
  - A question is not a request to edit. Models asked "why does divide() raise?"
    often also fix it, unasked. On a question turn that is now impossible.
  - The router only restricts when the rules are confident. Anything it cannot
    place falls through to the unrouted behaviour, so a miss costs at most one
    confirmation round-trip and never makes the agent less capable than before.
  - Rules first, and no model call at all by default: they are free, instant and
    testable. `"llm": true` adds a compact-model call for turns the rules leave
    undecided; evals/intent/ measures whether that is worth it.

The value is safety and predictability, not "understanding the request better"
-- the main model already does that. See evals/intent/ for how often it helps.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from ...plugins.base import PluginContext, TurnPolicy

LABELS = ("question", "plan", "unclear", "edit", "command")
RESTRICTED = frozenset({"question", "plan", "unclear"})

_I = re.IGNORECASE
# Order of the checks is the policy; see classify().
_AFFIRM = re.compile(
    r"^\s*(好|好的|可以|行|嗯|改吧|改吧改吧|就这么改|就这样|按这个改|按你说的(来|改)|确认|是的|对|没问题|"
    r"yes|yep|ok|okay|sure|go ahead|do it|please do|proceed|sounds good|lgtm)\b[\s。.!！,，]*",
    _I)
_VAGUE = re.compile(
    r"优化|完善|整理|弄|搞|处理|修|改|调|看看|看一下|看着办|有点问题|有问题|不对|不行|老样子|那个|"
    r"\b(improve|optimi[sz]e|fix|update|refactor|clean|tidy|polish|sort|make|handle|help|look|"
    r"doesn'?t work|broken|something'?s off|the thing)\b", _I)
# A named target makes a short request concrete: a file, an identifier, code.
# Case-sensitive on purpose: with IGNORECASE the CamelCase branch matched any
# word of three letters or more, so "help" and "fix it" counted as named targets.
_TARGET = re.compile(r"\w+\.[a-zA-Z]{1,4}\b|`|\w+_\w+|\w+\(\)|\b[A-Z][a-z]+[A-Z]\w*|(?i:\breadme\b)")
_EDIT_SHAPE = re.compile(r"把|改成|改为|换成|删掉|删除|加上|添加")


def _vague(t: str) -> bool:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", t))
    words = len(re.findall(r"[A-Za-z']+", t))
    short = cjk <= 8 and words <= 4
    return (short and bool(_VAGUE.search(t)) and not _TARGET.search(t)
            and not _EDIT_SHAPE.search(t))
_PLAN = re.compile(
    r"方案|计划|规划|思路|步骤|先别改|先不要改|不要改代码|别改代码|别动代码|先别动|先不要动|先不要实现|先别实现|设计一下|设计下|怎么设计|"
    r"不用改|不需要改|不写代码|先不写|利弊|优缺点|权衡|评估一下|想想怎么|想一想怎么|下一步|"
    r"\btradeoffs?\b|pros and cons|options for|\boutline\b|\bsketch\b|"
    r"\bplan\b|\bdesign\b|\bapproach\b|\bproposal\b|\bstrategy\b|how should (we|i)\b|"
    r"don'?t (change|implement|edit|touch|write)|before (changing|implementing|you change)|no code (yet|changes)",
    _I)
_RUN = re.compile(
    r"跑一下|跑下|跑跑|跑一遍|运行|执行|安装依赖|安装一下|装一下|启动|部署|编译|构建|打包|报错|挂了|失败|崩溃|崩了|"
    r"\bgit\s+[a-z]+|\bpytest\b|\bruff\b|\blint\b|^\s*trigger\b|\btests? (pass|passing)\b|"
    r"\brun\b|\bexecute\b|\binstall\b|\bstart\b|\blaunch\b|\bdeploy\b|\bbuild\b|\bcompile\b|\bdebug\b|"
    r"\bfail(s|ing|ed)?\b|\berrors?\b|\btraceback\b|\bcrash", _I)
_EDIT = re.compile(
    r"改成|改为|改一下|修改|修复|修一下|修掉|加上|加个|加一个|添加|增加|删除|删掉|去掉|移除|重命名|改名|实现|创建|新建|"
    r"写一个|写个|写一段|重构|替换|更新|升级|补上|补充|迁移|提交|格式化|"
    r"调大|调小|调成|改大|改小|设成|设为|设置成|改得|改掉|改进|拆一下|拆成|拆分|拆开|"
    r"把.{0,30}(改|换|删|加|拆|移|调)|让.{0,20}支持|"
    r"\badd\b|\bfix\b|\bchange\b|\bupdate\b|\brename\b|\bremove\b|\bdelete\b|\bimplement\b|\bcreate\b|"
    r"\bwrite\b|\brefactor\b|\breplace\b|\brewrite\b|\bmove\b|\bconvert\b|\bbump\b|\bupgrade\b|"
    r"\bformat\b|\bcommit\b|\bgenerate\b", _I)
_QUESTION = re.compile(
    r"[?？]\s*$|^\s*(为什么|为啥|怎么|如何|什么|哪|是否|有没有|是不是|解释|说明|介绍|讲讲|讲一下|说说)|"
    r"是做什么的|是干什么的|什么意思|什么作用|有什么区别|的区别|原理|是多少|指什么|说一下|理解一下|是什么|原因|"
    r"^\s*(what|why|how|where|when|which|who|is|are|does|explain|describe|tell me)\b", _I)
# A question that opens with a wh-word is a question even if it names an edit
# ("what files did we change"), unless it is a polite request ("could you ...").
_WH_LEAD = re.compile(r"^\s*(what|why|where|which|who|when)\b|^\s*(为什么|为啥|哪些|哪里|什么)", _I)
_POLITE = re.compile(r"\b(can|could|would) you\b|\bplease\b|能不能|能否|可不可以|麻烦", _I)


def classify(text: str, after_restricted: bool = False) -> tuple[str | None, str]:
    """(label, why). None = no confident call; the turn runs unrouted.

    Order matters: a confirmation after a restricted turn wins; then short,
    vague requests with no named target ("修一下", "make it better"); then "plan,
    don't change yet" (it beats edit verbs: "先别改，给个方案"); then running /
    debugging (it needs bash); then questions that open with a wh-word; then edit
    verbs (which also catch polite requests phrased as questions: "能帮我把 X 改成
    Y 吗？"); and only then other questions.
    """
    t = text.strip()
    if after_restricted and _AFFIRM.match(t) and len(t) <= 40:
        return "edit", "confirmation after a restricted turn"
    if _vague(t):
        return "unclear", "short, vague, no target"
    for label, rx in (("plan", _PLAN), ("command", _RUN)):
        m = rx.search(t)
        if m:
            return label, f"matched {m.group(0).strip()!r}"
    if _WH_LEAD.search(t) and not _POLITE.search(t):
        return "question", "opens with a wh-word"
    for label, rx in (("edit", _EDIT), ("question", _QUESTION)):
        m = rx.search(t)
        if m:
            return label, f"matched {m.group(0).strip()!r}"
    return None, "no signal"


NOTES = {
    "question": "Routed as a question: answer it. Tools that change files are off for this "
                "turn; if a proper answer needs a change, describe it and ask the user to confirm.",
    "plan": "Routed as a planning request: lay out the plan. Tools that change files are off "
            "for this turn.",
    "unclear": "Routed as unclear: ask one short clarifying question before doing anything. "
               "Tools that change files are off for this turn.",
}

_LLM_PROMPT = (
    "Classify this request to a coding agent as exactly one word: question (wants "
    "information only), plan (wants a plan or design, no changes yet), unclear (too vague "
    "to act on), edit (wants files created or changed), command (wants something run).\n\n"
    "Request: {text}\n\nOne word:")


def llm_classify(client: Any, model: str, text: str) -> str | None:
    response = client.chat(messages=[{"role": "user", "content": _LLM_PROMPT.format(text=text)}],
                           system="", tools=[], model=model, max_tokens=20)
    words = re.findall(r"[a-z]+", " ".join(response.text_blocks).lower())
    return next((w for w in words if w in LABELS), None)


def policy_for(label: str | None) -> TurnPolicy | None:
    if label is None:
        return None
    return TurnPolicy(label=label, read_only=label in RESTRICTED, note=NOTES.get(label, ""))


class RouterPlugin:
    name = "router"

    def setup(self, ctx: PluginContext) -> None:
        use_llm = bool(ctx.config.get("llm", False))
        host = ctx.host
        last: dict[str, str | None] = {"label": None}

        async def decide(text: str) -> TurnPolicy | None:
            label, _ = classify(text, after_restricted=last["label"] in RESTRICTED)
            if label is None and use_llm and host is not None:
                try:
                    label = await asyncio.to_thread(
                        llm_classify, host.client, host.config.compact_model, text)
                except Exception:
                    label = None        # a failed classifier leaves the turn unrouted
            last["label"] = label
            return policy_for(label)

        ctx.on_turn(decide)


PLUGIN = RouterPlugin()
