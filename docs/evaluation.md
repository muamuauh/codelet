# Agent 评测：轨迹、过程指标与任务套件

这篇讲 codelet 怎么评测自己：每一步记了什么（轨迹）、从轨迹里读出哪些过程指标、任务套件怎么保证题目本身是对的，以及第一轮跑出来的结果和它暴露的问题。

已有的四套专项评测（compaction 保留率、memory 跨会话、intent 路由、SWE-bench Lite）见 [evals/README.md](../evals/README.md) 和 [technical-details.md](technical-details.md)。本文讲的是它们共用的底座。

---

## 为什么只看通过率不够

原来的 runner 每题只留下：过没过、token、花费、耗时，外加控制台输出的最后 2000 个字符。两次都通过的运行，一次可能用了 4 步，另一次用了 10 步；一次可能先读文件再改，另一次可能凭猜测直接改、碰巧改对了。这些区别 runner 看不到，而它们恰恰决定了换一道更难的题时，谁会先挂。

所以先把过程记下来，再从过程里算指标。

---

## 1. 轨迹：`codelet/trajectory.py`

每一步写一行 JSONL，一边运行一边写。进程中途崩溃，崩溃之前的步骤也都在文件里。

| kind | 字段 |
|---|---|
| `run_start` | 题目、第几次运行、provider / model、max_turns |
| `user_turn` | 提示词（截断）、意图路由的标签、是否只读 |
| `llm_call` | 耗时、stop_reason、usage、本次成本、文字（截断）、请求了哪些工具 |
| `tool_call` | `tool_use_id`、工具名、输入、**outcome**、输出（截断）、输出总长、耗时、`batch`（同一轮并行了几个） |
| `compaction` | L1 清掉了几个旧输出，L2 写了几次摘要 |
| `turn_end` | 最终回复长度、LLM 调用次数、是否触到 max_turns |
| `error` | 出错位置（LLM 调用 / 压缩）和错误信息 |
| `eval_result` | 过没过、每个 check 的结果（只有 runner 写这一行） |

公共字段是 `v / run_id / agent / seq / step / ts`。`step` 是 LLM 调用的序号；`tool_call` 带的是**发起它的那次 LLM 调用的 step**，所以并行调用不会和下一轮混在一起。这套字段和 quant-agent 的轨迹同构，两个项目共用一套读取代码。

### 被拦下 ≠ 工具出错

`_dispatch_one` 原来对每一种失败都只返回一个 `is_error` 的 tool_result。现在拆成 `_dispatch_gated`，返回 `(tool_result, outcome, 原始输出)`，`outcome` 有这几种：

| outcome | 含义 |
|---|---|
| `ok` / `error` | 工具执行了，成功或失败 |
| `hook_blocked` | PreToolUse hook 拦下了 |
| `permission_denied` | 权限闸拒绝了 |
| `readonly_blocked` | 这一轮被意图路由判为只读 |
| `user_rejected` | 用户在 diff 预览里拒绝了 |
| `unknown_tool` / `disabled` | 模型调用了不存在或已禁用的工具 |

「模型想做一件不该做的事」和「工具坏了」是两类完全不同的问题，混在一起统计，两类都看不清。

### 其他设计

- **子 agent 共用一个文件。** `Trajectory.child("sub1:general")` 和父 agent 共享文件和 `seq`，只是 step 计数各自独立，所以父子的步骤按真实时间顺序交错，用 `agent` 字段区分。
- **写盘前打码。** 打码规则和 memory 插件拒绝保存的内容是同一套：`sk-…`、`AKIA…`、PEM 私钥、`api_key= / password= / token=`。
- **截断。** 工具输入里的每个字符串保留 4,000 字符，输出和文字保留 2,000 字符。完整的工具输出本来就会由上下文层落盘。
- **记录失败不影响运行。** `record()` 吞掉一切异常：写轨迹失败，丢的是轨迹，不是这次运行。
- **默认关闭，零开销。** `trajectory=None` 时每个记录点都是空操作。
- **日常会话也能录：** CLI 加 `--trace`，写到 `.codelet/traces/<时间>.jsonl`。这个目录在 gitignore 里，因为它装着你的提示词和工具输出。

---

## 2. 过程指标：`evals/trajectory_metrics.py`

纯代码，不调用模型：

| 指标 | 定义 | 它在抓什么 |
|---|---|---|
| `tool_error_rate` | 执行了但失败的调用 / 执行了的调用 | 工具和环境之间的摩擦 |
| `gate_blocks` | 按 outcome 统计的拦截次数 | 模型越界的倾向 |
| `repeat_calls` | 在没有改过文件的前提下，再次发出完全相同的（工具, 输入） | 原地打转：同一个失败的测试反复跑，同一个文件反复读 |
| `read_before_edit` | `edit_file` 改的路径之前读过（或写过）的比例 | 盲改 |
| `verified_after_edit` | 最后一次改文件之后，有没有再跑过 bash | 改完有没有自己验证 |
| `steps_after_last_edit` | 最后一次改文件之后还调了几次 LLM | 收尾成本 |
| `parallel_share` | 处在 ≥2 个并行批次里的调用占比 | 并行派发有没有被用上 |
| `compactions` / `max_turns_hit` / `subagent_calls` / tokens / cost / latency | — | — |

路径比较前会先归一化：反斜杠转成斜杠，去掉 `./`，统一小写。`repeat_calls` 在每次改文件后清零：改完代码再跑同一条测试命令是正当的。

---

## 3. 任务套件：每道题都自带「题目是对的」的证明

`evals/tasks/*.yaml` 新增两个字段：

```yaml
category: bugfix           # bugfix | feature | multi_file | refactor | testing | constraint | clarify | debug
solution:                  # 参考答案：覆盖到起始文件上。agent 永远看不到
  stats.py: |
    ...
```

`tests/test_eval_tasks.py` 对每道题做两件事：起始文件上 checks **必须不通过**，叠加参考答案后 **必须全过**。`clarify` 类的题 `solution: {}`，正确做法是什么都不改，所以起始文件上就必须通过。这和 SWE-bench 用 gold patch 校验每个实例是同一个思路：一道起始就能过的题什么都没测到，一道参考答案都过不了的题测的是出题人。

| 类别 | 题目 | 考什么 |
|---|---|---|
| bugfix | `fix_off_by_one` `fix_median_even` `fix_mutable_default` `fix_regex_email` | 从失败的测试定位；邮箱正则要放宽，但不能放过非法地址 |
| debug | `debug_traceback` | 只给一段 traceback |
| feature | `create_greet` `implement_slugify` `implement_lru` `implement_word_freq` `add_cli_flags` | 从测试读出规格；CLI 题的测试会起子进程去跑脚本 |
| multi_file | `rename_across_files` `bulk_discount` | 跨包重命名不许留别名；规则写在一个模块，在另一个模块用 |
| refactor | `extract_helper` | 行为不变，并且重复的那段校验只能剩一份（用命令数 `'bad email'` 出现的次数） |
| testing | `write_tests` | 写出来的测试要能抓住两个变异体：漏了整百年规则的、漏了整四百年规则的 |
| constraint | `respect_tests` | 测试按规格要求保留一位小数，只许改实现 |
| clarify | `clarify_ambiguous` `question_only` | 「那个函数太慢了」，但有两个候选；「先别改，解释给我听」 |

check 仍然全部基于执行结果（命令退出码、文件内容），不对 agent 的回复文字做断言。

---

## 4. Runner 新增的能力

- **`--repeat k`**，报告三个数：
  - pass@1：所有运行的平均通过率；
  - pass@k：k 次里**至少一次**通过的题目占比；
  - pass^k：k 次**全部**通过的题目占比。

  后两者的差距，就是单个通过率藏起来的运行间方差。
- **按轮次跑**：先把每道题跑一遍，再跑第二遍。预算提前耗尽时，pass@1 仍然覆盖整套题，而不是把前几道题跑了三遍。
- **从 `.env` 读配置**：`CODELET_EVAL_MODEL`、`CODELET_EVAL_PRICE_INPUT/OUTPUT`（每百万 token 的美元价）、`CODELET_EVAL_BUDGET_USD`。设了预算却没有价格时**拒绝启动**，因为成本显示为 n/a，预算上限就会形同虚设。
- **报告**：`evals/reports/<stamp>.json` 新增 `by_category`、`pass@k` 和过程指标的汇总；每道题都带上自己轨迹文件的路径和过程指标。轨迹在 `evals/reports/traj/<stamp>/<task>[#trial].jsonl`。

---

## 5. 第一轮结果（2026-11-02）

模型 `openai/gpt-6.1-sol`（经 ZenMux 的 OpenAI 兼容接口），17 题 × 3 次 = 51 次运行，按 $3 / $12.5 每百万 token 计价，共 **$3.23**，约 100 万 token。

### 通过率：饱和了

| | 结果 |
|---|---|
| pass@1 / pass@3 / pass^3 | **1.0 / 1.0 / 1.0**（51/51） |
| 按类别 | bugfix 12/12 · feature 15/15 · multi_file 6/6 · clarify 6/6 · debug 3/3 · refactor 3/3 · testing 3/3 · constraint 3/3 |

**这套题对这个模型已经区分不出好坏了。**它现在的作用是回归底线：改了 agent loop、压缩或权限之后，51 次里只要有 1 次挂掉，就是回归。要拿来比较模型或改进，得加难度。下一步是从 SWE-bench 的失败案例里取材（见 `evals/swebench/analyze_failures.py`）。

### 过程指标：通过率相同，过程不同

| 指标 | 值 |
|---|---|
| LLM 调用 / 工具调用（中位数） | 8 / 7；单次运行最少 3 次 LLM 调用（`question_only`），最多 10 次 |
| `read_before_edit` | **1.0**：所有 `edit_file` 改的都是之前读过的文件 |
| `verified_after_edit` | **1.0**：每次改完代码都跑过测试或命令 |
| `repeat_calls` | **0** |
| `steps_after_last_edit`（中位数） | 3 |
| 并行占比 | 351 次工具调用里有 97 次处在并行批次中（27.6%） |
| 触到 max_turns / 被拦截 | 0 / 0 |
| 单题平均成本 | $0.063，最低 `question_only` 每次 $0.024，最高 `rename_across_files` 每次 $0.094 |

**clarify 类：** 3 次 `clarify_ambiguous` 都是读完文件后反问「你想优化哪一个」，并顺带问了数据规模；3 次 `question_only` 都正确指出了空行会在 `line.split("=", 1)` 处抛出 `ValueError`，没有改代码。注意，这次意图路由插件没有加载（轨迹里 `turn=None`），所以这是模型自己的行为，不是只读闸的功劳。

### 工具报错：5.4%，绝大多数不是工具坏了

21 次报错里，`edit_file` 1 次（old_string 没对上，下一步就修正了），bash 20 次。翻轨迹逐条分类：

| 原因 | 次数 | 例子 |
|---|---|---|
| **shell 方言不对**：Windows 上 bash 工具跑的是 cmd，模型写的是 POSIX 语法 | 8 | `[ -f greet.py ]`、`python - <<'PY'`、`PYTHONDONTWRITEBYTECODE=1 python …`、`rg` 没装 |
| 在非 git 仓库的工作区里跑 `git status` | 7 | 评测工作区是临时目录 |
| **故意先复现失败**：修之前先跑测试，看到红色 | 4 | `python -m pytest -q test_cart.py` → 1 failed |
| 用错测试运行器 | 1 | 用 `unittest discover` 跑 pytest 风格的测试，0 个测试 |

两个结论：

1. **系统提示词里没写操作系统和 shell。** 8 次方言错误都来自这一点。后来又发现模型读到的 cmd 报错全是乱码，最终改成用 PowerShell 7 执行（见 6.1）。
2. **`tool_error_rate` 把好行为也算成了错误。** 修 bug 之前先跑测试复现失败，是值得鼓励的做法，但非零退出码会让 bash 返回 `is_error`。已经拆开（见 6.2）。

### 其他观察

- **`search_docs`（RAG 插件）在 37 次运行里被调用。**而评测工作区里只有两三个 `.py` 文件，没有任何文档，这些调用大概率是空转。这是一个低成本的优化点：工作区很小时不注册这个工具，或者在提示词里降低它的优先级。
- **`todo_write` 调了 79 次**，平均每题 1.5 次。对两三步就能做完的小任务，这也是收尾成本的一部分。

---

## 6. 第一轮之后改了什么（2026-11-03）

第一轮的轨迹指出了四个问题，下面每一项都已经改完、有测试覆盖。效果见 6.5 和 6.6 的两次验证。

### 6.1 Windows 上命令改由 PowerShell 7 执行

回头翻那 8 次 shell 方言错误，发现还有更根本的问题：**模型连报错内容都读不到。**

- `bash` 工具用 `text=True, encoding="utf-8", errors="replace"` 解码子进程输出，而中文 Windows 上 cmd.exe 的报错用的是 OEM 代码页（cp936）。
- 结果「'rg' 不是内部或外部命令」到模型那里变成了「'rg' ����ڲ�…」：它只知道命令失败了，不知道为什么，自然没法改正。
- 第一轮自动统计出的 env 类错误只有 8 次，而人工逐条分类是 15 次，少的那几次也是因为报错文本是乱码，规则匹配不上。

改动：

| 改动 | 做法 |
|---|---|
| 换 shell | Windows 上 `pwsh` 在 PATH 里时，命令交给 PowerShell 7 执行；找不到才退回 cmd。`CODELET_SHELL=pwsh\|cmd\|sh` 可以强制指定。工具名仍叫 `bash`，因为提示词、评测和插件都按这个名字引用 |
| 传参 | 用 `-EncodedCommand`（UTF-16LE 的 base64），Python → CreateProcess → PowerShell 三层的引号转义规则全部绕开 |
| 输出 | 前面加一段：`[Console]::OutputEncoding` 和 `$OutputEncoding` 设为 UTF-8，关闭进度条，`$PSStyle.OutputRendering = 'PlainText'`（否则 `Select-String` 的输出会带 ANSI 颜色码）；`-OutputFormat Text`（否则管道被重定向时，错误流会被序列化成 CLIXML） |
| 退出码 | 末尾加 `if (-not $?) { exit $LASTEXITCODE 或 1 }`：最后一条语句失败（cmdlet 报错或原生程序返回非零）时，调用就算失败 |
| 带引号的路径 | 以引号开头的命令自动补 `&`。在 PowerShell 里 `"C:\...\python.exe" -c ...` 是一个字符串表达式，不是调用，测试里就踩到了 |
| 解码兜底 | 不论用哪个 shell，都按「严格 UTF-8 → OEM 代码页 → 系统首选编码 → UTF-8 加替换字符」的顺序解码 |

系统提示词新增 `## Environment` 一节：写明实际用的是哪个 shell 和对应的语法要点（`Test-Path`、`$env:X = 'v'; cmd`、here-string、`ls -la` 在 pwsh 里会失败），以及当前目录是不是 git 仓库。这一节直接对应第一轮里另外 7 次「在非 git 目录里跑 `git status`」。

### 6.2 工具报错率拆开算

`bash_errors` 按原因把失败的 bash 调用分成四类：

| 类别 | 判定 | 算不算工具错误 |
|---|---|---|
| `repro` | 测试命令，且在第一次改文件之前 | 不算，这是先复现 bug |
| `test_failed` | 测试命令，且在改文件之后 | 不算，这是代码还不对，工具本身没坏 |
| `env` | 报错文本是 shell 或 git 的（cmd / pwsh 中英文界面、POSIX shell、`not a git repository`） | 算，这是 6.1 要压下去的那部分 |
| `other` | 其余 | 算 |

`tool_error_rate` 现在不含 `repro` 和 `test_failed`。拿第一轮的轨迹重算（不花钱）：5.4% → **3.0%**；分类是 repro 4、test_failed 6、env 8、other 2。env 自动只认出 8 次而不是人工数的 15 次，原因就是 6.1 里的乱码。

### 6.3 不写 `.pyc`

`tests/test_eval_tasks.py` 偶尔会挂在 `debug_traceback` 这道题上。原因：参考答案把 `cfg["Port"]` 改成 `cfg["port"]`，文件大小不变，又是在同一秒内改写的；而 `.pyc` 只校验「源文件修改时间（精确到秒）+ 大小」，所以 pytest 用了旧的字节码。agent 也可能踩中同样的情况：跑完测试马上做一个等长的修改，评分时就会拿旧代码判分。

现在 runner 给 agent 和检查命令都设置了 `PYTHONDONTWRITEBYTECODE=1`。另外，agent 的 `python` 现在和检查用的是同一个解释器。以前只有检查命令会把 runner 自己的解释器放到 PATH 最前面，agent 执行命令时用的是系统里的另一个 Python。

### 6.4 加难度：5 道带隐藏测试的题

第一轮 51/51 说明题太简单。新题仿照 SWE-bench 的两类失败来出：「改对了文件但逻辑错了」和「修好了目标问题，却把原来通过的测试改坏了」。

- **`hidden_files`**：这些文件在 agent 结束之后、检查之前才写进工作区，对应 SWE-bench 的 FAIL_TO_PASS / PASS_TO_PASS。agent 只能看着 issue 描述自己预见边界情况。
- **题面写成 issue**，不直接给测试。

| 题目 | 陷阱 |
|---|---|
| `lazy_slice` | 切片和下标要和 list 完全一致，`range(n)[key]` 是最短的解法；隐藏测试里有 486 个切片组合，外加「取前 3 个元素时最多只生成 4 个」 |
| `format_bytes_regression` | 「整数倍时去掉 `.0`」。直接 `rstrip("0").rstrip(".")` 会把 1.04 KB 写成 `1 KB`；1023.999 KB 也必须保持 `1024.0 KB` |
| `cents_rounding` | 症状在发票上，根因在三层之下的 `money.py`（`int(float * 100)` 截断），旁边还放了一个格式化模块当干扰。`round(float * 100)` 也不对：`0.005` 会按银行家舍入变成 0 |
| `memoize_kwargs` | 缓存键要带上 kwargs，且与顺序无关；不可哈希的参数要绕过缓存。用 `cache.get()` 的真值判断命中，会把返回 `None` 的结果漏掉 |
| `merge_intervals` | 整数闭区间，相邻也要合并（(1,3)+(4,6) → (1,6)），非法区间抛错，不许修改入参 |

除了 `test_eval_tasks.py` 的有效性校验，我还给每道题写了一个「看起来对的」偷懒修法，确认它们都会挂在设计好的那条测试上：`lazy_slice` 的 `list(self._factory())[key]` 其余 495 条测试全部通过，只在「必须按需生成」那一条失败。

### 6.5 验证：第二轮（2026-11-03）

同一个模型（`openai/gpt-6.1-sol`），22 道题各跑 1 次，预算 $2.0。跑到第 17 题时，预算检查判断下一题会超出上限，停了下来。没跑的 5 道是 `merge_intervals`、`question_only`、`rename_across_files`、`respect_tests`、`write_tests`。花费 $1.92。

| | 第一轮（17 题 × 3） | 第二轮（17 题 × 1） |
|---|---|---|
| 通过 | 51/51 | **17/17**（含 4 道难题） |
| env 类 bash 报错 | 15（人工分类；自动只认出 8） | **0** |
| 其中「非 git 目录里跑 git」 | 7 | **0**（git 调用 3 → 0） |
| `tool_error_rate`（新口径） | 3.0% | **0%** |
| 每次运行的 bash 调用 | 1.43 | 1.18 |
| repro / test_failed | 4 / 6 | 1 / 2 |

- **方言错误消失了，而且有具体的证据。** `fix_regex_email` 上一轮 3 次里有 3 次因为 `python - <<'PY'` 失败；这一轮写的是 `python -m pytest -q; if ($LASTEXITCODE -eq 0) { python -c "..." }`，是 PowerShell 语法，一次成功。上一轮常见的 `pwd && ls -la && git status` 式探查也没有了，agent 改用 read_file / glob 工具。
- **样本小，但不像巧合。** 按第一轮每次运行约 0.29 次 env 错误的频率，17 次运行一次都不出的概率约为 e^-5 ≈ 0.7%。不过题目子集不完全一样，每题也只跑了 1 次。
- **难题照样全过，但过程拉开了差距。** `cents_rounding` 用了 21 次 LLM 调用、$0.35，`lazy_slice` 19 次、$0.35，而普通题的中位数是 8 次、约 $0.08。这两道题各有一次「改完跑测试仍失败、接着再改」（`test_failed`）。对这个模型来说，难度没有体现在通过率上，而是体现在成本上。要拉开通过率，得换一个更弱的模型，或者出更难的题。

### 6.6 补跑剩下的 5 道（2026-11-03）

`merge_intervals`、`question_only`、`rename_across_files`、`respect_tests`、`write_tests` 单独跑了一次（用 `--tasks-dir` 指向只放这 5 个文件的目录），$0.34，5/5 通过，env 错误 0。和 6.5 合起来，sol 在 22 道题上各跑 1 次：**22/22**，共 $2.25。5 道难题的中位数是 11 次 LLM 调用、$0.13；最难的 `cents_rounding` 和 `lazy_slice` 各要 19～21 次、$0.35。

### 6.7 换一个便宜模型：`openai/gpt-6-luna`

同一家、同一个接口，ZenMux 标价 $0.1 / $0.5 每百万 token（命令行临时覆盖 `CODELET_EVAL_PRICE_*`，`.env` 没动）。22 道题 × 3 次，**$0.21**。

| | sol（22 × 1） | luna（22 × 3） |
|---|---|---|
| pass@1 | 1.0 | **0.955**（63/66） |
| pass@3 / pass^3 | — | 1.0 / **0.909** |
| 失败 | — | `clarify_ambiguous` 2 次，`format_bytes_regression` 1 次 |
| env 类 bash 报错 | 0 | **0** |
| `read_before_edit` | 1.0 | 1.0 |
| `verified_after_edit` | 1.0 | 0.952（3 次改完没跑任何命令，碰巧都过了） |
| `test_failed`（改完测试仍失败） | 2 / 22 次运行 | 8 / 66 |
| `steps_after_last_edit`（中位数） | 3 | 4 |
| LLM 调用（中位数）普通题 / 难题 | 8 / 11 | 7 / 11 |
| 每次运行的成本 | $0.10 | **$0.0032** |

- **pass@3 = 1.0，pass^3 = 0.909。** 每道题它都做得出来，但有两道题不是每次都做对。这正是只看 pass@1 或 pass@k 会漏掉的东西：如果每次只给用户一个结果，22 道题里有 2 道是看运气。
- **难题它几乎都会做，失败在「判断」上。** `clarify_ambiguous` 的题面是「utils.py 里那个函数太慢了，帮我优化一下」，而文件里有两个慢函数。sol 每次都反问「你指的是哪一个」；luna 第 1 次也问了，第 2、3 次把两个函数都改了，还汇报「已优化两个函数，测试通过」。代码本身没错，错在没问。这次意图路由插件没有加载，加载后「不明确」类请求会被设为只读，正好是它的用武之地。
- **`format_bytes_regression` 的失败是题目设计的那个陷阱。** 它用「去掉末尾的 0 和小数点」处理整数倍，于是 1023.999 KB 变成了 `1024 KB`、1.04 KB 变成了 `1 KB`，被隐藏测试抓住。这次只用了 7 次 LLM 调用，是 15 次难题运行里最少的：**失败的那次恰恰是「想得最少」的那次。**
- **框架层的改进与模型无关。** env 错误在两个模型上都是 0，`read_before_edit` 都是 1.0。PowerShell 和 Environment 提示词的效果，换成便宜模型照样成立。
- **便宜模型省的是钱，不是步数。** 两者的调用次数几乎一样，luna 每次运行便宜约 30 倍（按 `.env` 里 sol 的单价；按标价算约 20 倍），代价是 pass^3 从 1.0 掉到 0.909，以及更多的「改完还不对」。

## 7. 局限

- **第一轮的题太简单**（见上文）。100% 只说明没有回归，不说明 agent 很强。5 道难题对 sol 也都不难（见 6.5、6.6），对 luna 有一道会挂（见 6.7）。
- **sol 每道题只跑了 1 次。** 22/22 不能说明它的 pass^3 也是 1.0；第一轮 17 道题跑 3 次是 51/51，算是一个旁证。
- **过程指标是启发式的。** `read_before_edit` 判断不出「读了但没读到关键行」；`verified_after_edit` 判断不出「跑了命令但跑的不是测试」。它们用来报警，不用来打分。
- **成本是按自己设的单价算的，偏高。** `.env` 里填的是 $3 / $12.5 每百万 token，ZenMux 模型列表上 `gpt-6.1-sol` 的标价是 $2 / $10，另外还有 prompt cache 的折扣没计入。所以文中的花费都是上限，实际账单更低。

---

## 怎么运行

```powershell
conda activate codelet
$env:PYTHONUTF8="1"

python -m evals.runner                       # 全部题目各跑一次
python -m evals.runner --repeat 3            # pass@1 / pass@3 / pass^3
python -m evals.runner --filter fix_ --budget 1
# 换模型：命令行的环境变量优先于 .env
$env:CODELET_EVAL_PRICE_INPUT="0.1"; $env:CODELET_EVAL_PRICE_OUTPUT="0.5"
python -m evals.runner --model openai/gpt-6-luna --repeat 3 --budget 0.6
python -m evals.trajectory_metrics evals/reports/traj/<stamp>     # 从轨迹重算过程指标，$0
python -m codelet --trace                     # 日常会话也录轨迹
$env:CODELET_SHELL="cmd"                      # 强制 bash 工具用 cmd（默认：有 pwsh 就用 pwsh）
```

测试：`tests/test_trajectory.py`、`tests/test_trajectory_metrics.py`、`tests/test_eval_tasks.py`（不调用模型）。
