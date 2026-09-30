# claude-statusline-gitgraph

一张**横向**的 git 分支图，常驻在 Claude Code 输入框下方：主线在上，分支在下，时间从左往右走。一眼就能看出你在哪个分支、比 main 多了几次提交、有没有改了没提交的文件。

![预览](docs/preview.svg)

- **简洁**：不显示提交说明，只画结构。已经合并完的旧分支会变暗，太长的分支会折叠成 `(5)`。
- **信息刚好够用**：`+3 −1` 表示比 main 多几次、少几次提交，`↑2 ↓1` 表示和远程仓库差几次提交，`✎2` 表示有几个文件改了还没提交，`1h` 表示离上次提交过了多久。
- **对新手友好**：自带符号表（`legend`）和一个 5 分钟的学习模式（`learn`）。学习模式在临时的沙盒仓库里一步步演示分支是怎么长出来、怎么合并的，不会碰你的项目。
- **零依赖**：一个 Python 文件，只用标准库，有 `git` 和 `python3` 就能跑。

## 快速开始

```bash
git clone https://github.com/NowhereMan-in-Galaxy/claude-statusline-gitgraph.git ~/tools/claude-statusline-gitgraph
cd ~/tools/claude-statusline-gitgraph

python3 gitgraph.py log              # 详情：每一段分支做了什么、改了多少
python3 gitgraph.py learn            # 学习模式：跟着走一遍分支的日常用法
python3 gitgraph.py legend           # 符号表：每个符号是什么意思
python3 gitgraph.py ~/your/project   # 画出某个仓库的分支图
```

## 放进 Claude Code 的状态栏

在 `~/.claude/settings.json` 里加上（路径换成你 clone 的位置）：

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --statusline",
    "refreshInterval": 10
  }
}
```

- Claude Code 每轮对话后都会运行一次这个命令，另外每 `refreshInterval` 秒再运行一次，把输出显示在输入框下方。
- `--statusline` 会从 Claude Code 传来的信息里读出当前目录，所以换项目时图会自动跟着换。
- 不在 git 仓库里时什么都不输出，状态栏保持空白。

**已经有自己的状态栏脚本？** 在脚本末尾加一行，把图接在原来的内容下面：

```bash
printf '\n%s' "$(python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --color "$cwd")"
```

## 看每一段做了什么：`log`

状态栏平时保持简洁。想看细节时运行 `log`：图上每一段分支会标一个字母，下面逐段列出它的状态、提交数、改动量、时间，以及它做了什么（已合并的分支用合并说明，没合并的用最近一次提交的说明）：

```
●─●───────●─◆───────────◆─●─────────●    main
  ╰─A─●─●───┴─B─(5)─●─●─╯ ├─C─●          feat/search +2 −1
                          ╰─────D─●───◉  feat/theme +3 −1 ✎2 1h

 A  feat/login   merged  3 commits  2 files   +96   −4  Sep 26 → Sep 27  Log-in page and API
 B  feat/api     merged  8 commits  6 files  +340  −41  Sep 27 → Sep 28  Connect the backend API
 C  feat/search  open    2 commits  1 file    +30   −0  Sep 29 → now     Add a search box
 D  feat/theme   open    3 commits  2 files   +55   −4  Sep 29 → now     Dark mode icons

 more: git log main..BRANCH   ·   git diff --stat main...BRANCH
```

把它做成一个短命令会更顺手，比如放一个可执行文件到 `~/.local/bin/gitgraph`：

```sh
#!/bin/sh
exec python3 "$HOME/tools/claude-statusline-gitgraph/gitgraph.py" log --color "$@"
```

之后在任何终端里输入 `gitgraph` 就能看；在 Claude Code 里输入 `! gitgraph`。

## 看懂这张图

```
●─●───────●─◆───────────◆─●─────────●    main
  ╰─●─●─●───┴─●─(5)─●─●─╯ ├─●─●          feat/search +2 −1
                          ╰─────●─●───◉  feat/theme +3 −1 ✎2 1h
```

| 符号 | 意思 |
|---|---|
| `●` | 一次提交（一个存档点） |
| `◉` | 你现在所在的位置（HEAD） |
| `◆` | 合并提交：有分支在这里并回主线 |
| `(5)` | 折叠起来的 5 次提交 |
| `╰` `╯` | 分支从这里分出去 / 在这里合并回来 |
| `├` | 两个分支从同一个存档点分出去 |
| `┴` | 一个分支刚合并，下一个紧接着分出去 |
| `┼` | 两条线在这里交叉而过 |
| `┄` | 分叉点太早，不在图里 |
| 暗色的线 | 已经合并完的旧分支 |
| `+3` / `−1` | 比 main 多 3 次 / 少 1 次提交 |
| `↑2` / `↓1` | 还有 2 次提交没推送 / 远程有 1 次提交没拉下来 |
| `✎2` | 有 2 个文件改了还没提交 |
| `1h` | 离这个分支上次提交过了多久（m 分钟 · h 小时 · d 天） |
| `+2 more` | 还有 2 个分支放不下没画出来 |
| **加粗的分支名** | 你现在所在的分支 |

## 调整

`gitgraph.py` 开头有几个常量，可以按喜好改：

| 常量 | 默认 | 作用 |
|---|---|---|
| `MAX_COLS` | 30 | 最多画多少列，决定图有多宽 |
| `MAX_ROWS` | 3 | main 下面最多画几行分支 |
| `FOLD_OVER` | 4 | 一个分支连续超过几次提交就折叠 |
| `COLORS` | — | 各部分的颜色（终端 256 色编号） |

输出被其他程序接走时（比如存进文件、交给另一个脚本）会自动去掉颜色。如果接收方能显示颜色，比如你自己的状态栏脚本，或者 Claude Code 里的 `!` 命令，就加上 `--color` 强制开启。`--no-color` 或环境变量 `NO_COLOR=1` 关闭颜色。

## 它是怎么画出来的

见 [docs/how-it-works.md](docs/how-it-works.md)：怎么从 git 读出分叉和合并、为什么按拓扑顺序排列而不按时间排、字符格子里的拐角是怎么合成的。

## English

A horizontal git branch graph for the Claude Code status line (or any terminal). The trunk is on top, branches below, and time flows left to right. Each branch shows how far it is ahead of and behind `main`, how far it is ahead of and behind its upstream, how many files are uncommitted, and how long ago its last commit was. Run `python3 gitgraph.py learn` for a guided tour in a throwaway sandbox repository (in Chinese). It is a single Python file with no dependencies beyond the standard library. The status line setup is shown above.

## 许可证

[MIT](LICENSE)
