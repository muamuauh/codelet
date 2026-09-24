"""Labelled requests for the intent router. Synthetic and labelled by one person.

There were no real prompts to sample: ~/.codelet/sessions held nine distinct
user messages, all test runs. So these were written to sound like requests to a
coding agent, in Chinese and English, deliberately including hard cases the
rules were not written for: polite edit requests phrased as questions, planning
requests without the word "plan", vague requests with an edit verb in them.

Gold labels, by what the user wants from THIS turn:
  question  information about the code, answerable by reading it
  plan      a plan, design or options before any change
  unclear   too underspecified to act on without asking (which bug? what "better"?)
  edit      files created, changed or deleted -- whatever the phrasing
  command   something run: tests, builds, installs, or debugging a failure
            that needs running things
question / plan / unclear are the turns where read-only is right.

Within each label, even positions are the dev split (for tuning rules) and odd
positions the test split (reported). The rules were written before this file.
"""
from __future__ import annotations

QUESTION = [
    "calc.py 里的 divide 是做什么的？",
    "这个项目的测试是怎么组织的",
    "Explain what `compact_if_needed` does.",
    "What does the --profile flag do?",
    "为什么这里用 asyncio.gather 而不是 TaskGroup？",
    "Where is the settings file loaded?",
    "How does the plugin loader discover plugins?",
    "这两个函数有什么区别：load 和 parse",
    "utils.py 第 40 行那个正则是什么意思",
    "Is the session file written atomically?",
    "哪些地方调用了 build_client？",
    "What's the difference between AUTO and ASK mode here",
    "介绍一下这个仓库的目录结构",
    "Does this function handle empty input?",
    "Can you explain the retry logic in http_client.py?",
    "讲讲 AgentSink 是怎么解耦 UI 的",
    "what files did we change in the last commit",
    "Why is max_turns set to 30?",
    "这个函数的时间复杂度是多少",
    "How many tools are registered by default?",
    "这段代码有没有线程安全问题？",
    "Which Python version does this project target?",
    "说一下 memory 插件的数据格式",
    "What would happen if the summarizer fails?",
    "README 里说的 P9 是指什么",
    "Who calls `_dispatch_one`?",
    "是不是所有工具都会经过权限检查",
    "How is the token budget estimated?",
    "这个 bug 的原因是什么，不用改",
    "Summarize what this module does.",
    "帮我理解一下 context.py 的压缩逻辑",
    "Any idea why test_evals imports yaml?",
]

EDIT = [
    "把 timeout 改成 30 秒",
    "能帮我把 timeout 改成 30 秒吗？",
    "Add a --verbose flag to the CLI.",
    "Can you add tests for utils.py?",
    "给 parse() 加上类型注解",
    "Rename parse_cfg to load_config everywhere.",
    "删掉没用的 import",
    "Fix the typo in README.",
    "修一下 README 里的错别字",
    "Implement a --dry-run option for the sync command.",
    "Create a new module mathutil.py with a clamp function.",
    "把这个类拆成两个文件",
    "Refactor the dispatch loop to use a dict instead of if/else.",
    "在 config.py 里新增一个 max_retries 字段，默认 3",
    "Please update the docstring of compact_if_needed.",
    "写一个脚本把 csv 转成 json",
    "Replace print with logging in cleanup.py.",
    "Could you make load_settings return a dataclass?",
    "帮我把这个函数改得更易读一点",
    "Change the default model to claude-haiku-4-5.",
    "把错误信息改成中文",
    "Move the helper functions into utils.py.",
    "能不能把 max_turns 调大到 50？",
    "Bump the version to 0.4.0.",
    "给 README 加一节安装说明",
    "Convert these tabs to spaces.",
    "让 bash 工具支持超时参数",
    "Remove the deprecated --legacy option.",
    "把 test_calc.py 改名成 check_calc.py",
    "Add error handling for missing files in read_file.",
    "这个函数太长了，拆一下",
    "Write a unit test for subtract().",
]

COMMAND = [
    "跑一下测试",
    "Run the test suite.",
    "执行 pytest tests/test_router.py",
    "Install the dev dependencies.",
    "帮我启动 web 服务",
    "Run ruff and show me the output.",
    "git status 看一下",
    "构建一下 docker 镜像",
    "Why is the build failing?",
    "测试挂了，看看怎么回事",
    "Check whether the server starts without errors.",
    "运行一下 evals/runner.py 看看结果",
    "Deploy this to the staging box.",
    "Execute the migration script.",
    "pip install -e . 然后跑一下测试",
    "Start the dev server on port 8000.",
    "编译一下前端",
    "Can you run the benchmarks and report the numbers?",
    "这个脚本报错了：ModuleNotFoundError，帮我看看",
    "Debug why test_parallel_dispatch is flaky.",
    "Lint the codebase.",
    "用 python -m codelet \"hi\" 试一下能不能跑通",
    "Show me the output of pytest -q.",
    "打包成 wheel",
    "Trigger the eval with the haiku model.",
    "看看 CI 为什么失败",
    "Reproduce the crash from issue #12.",
    "跑一遍 SWE-bench 前 5 题",
    "Check if all tests pass after the last commit.",
    "执行一下 git pull",
    "Profile the startup time.",
    "run it",
]

PLAN = [
    "先别改代码，给我一个重构方案",
    "How should we split this module?",
    "设计一下缓存层",
    "Propose an approach for adding authentication — don't implement yet.",
    "列一下迁移到 TaskGroup 的步骤",
    "What's the best way to add plugin versioning? Just the plan.",
    "给个思路：怎么让压缩支持多模型",
    "Outline how we'd add a memory eviction policy.",
    "我想加一个意图识别，你觉得应该怎么设计？",
    "Before changing anything, tell me how you'd restructure the tests.",
    "规划一下 v1.0 要做的功能",
    "Write up a design for streaming tool results, no code yet.",
    "有哪些方案可以减少 token 消耗？",
    "Sketch the architecture for a multi-agent version.",
    "先别动代码，说说你打算怎么修这个 bug",
    "How would you approach migrating to pydantic?",
    "评估一下把 SQLite 换成 Postgres 的利弊",
    "Draft a step-by-step plan to add Windows support.",
    "不要改代码，先分析一下这个模块该怎么拆",
    "What's your strategy for testing the web UI?",
    "帮我想想怎么组织这些评测脚本",
    "Give me three options for caching and their tradeoffs.",
    "做一个实现计划：给 codelet 加权限规则",
    "Plan the refactor of agent_loop.py.",
    "我们下一步该做什么",
    "Think through how the router should handle follow-ups before we build it.",
    "设计下数据库表结构，先不写代码",
    "Propose a naming scheme for the eval reports.",
    "给我一个分阶段的升级计划",
    "How should I structure the tests for the memory plugin?",
    "先出方案再动手：给 web 界面加登录",
    "What approach would you take to reduce flaky tests?",
]

UNCLEAR = [
    "优化一下",
    "fix it",
    "改改",
    "make it better",
    "弄一下那个",
    "帮我看看",
    "help",
    "处理一下",
    "clean this up",
    "能不能搞好一点",
    "do the thing we discussed",
    "那个问题再看看",
    "improve performance",
    "调一下",
    "搞一下性能",
    "update it",
    "你看着办",
    "sort it out",
    "把那个改了",
    "something's off with the output",
    "有点问题",
    "fix the bug",
    "优化代码",
    "tidy up",
    "修一下",
    "this doesn't work",
    "整理一下",
    "can you take a look",
    "老样子",
    "refactor",
    "完善一下",
    "make it faster",
]

LABELLED: list[tuple[str, str]] = [
    (text, label)
    for label, items in (("question", QUESTION), ("edit", EDIT), ("command", COMMAND),
                         ("plan", PLAN), ("unclear", UNCLEAR))
    for text in items
]


def split(name: str) -> list[tuple[str, str]]:
    """"dev" (even positions within each label) or "test" (odd positions)."""
    want = 0 if name == "dev" else 1
    out: list[tuple[str, str]] = []
    for label, items in (("question", QUESTION), ("edit", EDIT), ("command", COMMAND),
                         ("plan", PLAN), ("unclear", UNCLEAR)):
        out.extend((t, label) for i, t in enumerate(items) if i % 2 == want)
    return out
