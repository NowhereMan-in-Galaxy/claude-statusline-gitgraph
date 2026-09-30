# claude-statusline-gitgraph

**English** · [中文](README.zh-CN.md)

Most status lines tell you which branch you're on. This one shows you **the shape of your history**: a live, horizontal git graph under the Claude Code prompt. The trunk runs across the top, branches fork off below, and time flows left to right.

![gitgraph in the Claude Code status line](docs/demo.svg)

- **Glanceable.** The status line shows no commit messages, only structure. Merged branches fade, and long runs fold into `(5)`.
- **Just enough numbers.** `+3 −1` shows how far a branch is ahead of and behind main, `↑2 ↓1` compares it with its remote, `✎2` counts uncommitted files, and `12m` is the time since the last commit.
- **Details on demand.** `gitgraph log` letters each branch segment and tells you what it did.
- **Teaches git.** `gitgraph learn` is a 17-step, hands-on git tutorial that runs in a throwaway sandbox. Every command is annotated, and you watch the graph change as you go.
- **Zero dependencies.** One Python file, standard library only. All you need is `git` and `python3`.
- **English and Chinese.** The language follows your system, and `--en` / `--zh` switch it.

## Quick start

```bash
git clone https://github.com/NowhereMan-in-Galaxy/claude-statusline-gitgraph.git ~/tools/claude-statusline-gitgraph
cd ~/tools/claude-statusline-gitgraph

python3 gitgraph.py ~/your/project   # the graph
python3 gitgraph.py log              # the graph, with each branch segment explained
python3 gitgraph.py learn            # the git tutorial (press Enter to step through)
python3 gitgraph.py legend           # what every symbol means
```

## Add it to the Claude Code status line

Add this to `~/.claude/settings.json`, using the path you cloned to:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --statusline",
    "refreshInterval": 10
  }
}
```

- Claude Code runs the command after every turn, and again every `refreshInterval` seconds. It shows whatever the command prints under the prompt.
- `--statusline` reads the current folder from the JSON Claude Code sends, so the graph follows you from project to project.
- Outside a git repository it prints nothing, and the status line stays empty.

**Already have a status line script?** Append the graph to it:

```bash
printf '\n%s' "$(python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --color "$cwd")"
```

## See what each branch did: `log`

The status line stays minimal. When you want the story behind the shape, run `log`. Each branch segment gets a letter, and a line below says whether it's merged or still open, how big it is, when it happened, and what it did. Merged branches use their merge message; open branches use their latest commit message.

![gitgraph log](docs/log.svg)

To make it a one-word command, save this as `~/.local/bin/gitgraph` and make it executable (`chmod +x`):

```sh
#!/bin/sh
case "$1" in
  learn|legend|log) exec python3 "$HOME/tools/claude-statusline-gitgraph/gitgraph.py" "$@" --color ;;
  *)                exec python3 "$HOME/tools/claude-statusline-gitgraph/gitgraph.py" log --color "$@" ;;
esac
```

Then run `gitgraph` in any terminal, or `! gitgraph` inside Claude Code. `gitgraph learn` and `gitgraph legend` work too.

## Learn git with it: `learn`

A beginner-friendly tour in five chapters. Every command carries a one-line comment, key steps show git's own output (`status`, `diff`, the conflict markers), and the graph redraws after each step so you can see what the command did.

| Chapter | You learn | You see |
|---|---|---|
| 1. Commits | `init`, `status`, `add`, `commit`, `log`, and how to write commit messages | `●` appear one by one |
| 2. Branches | creating a branch (and why it isn't a commit), `switch`, `diff` | a new row, `+2 −1`, `✎1` |
| 3. Merging | a clean merge, then a real conflict you resolve | `◆`, `╯`, the branch fading |
| 4. Remotes | `push`, `fetch`, `pull` against a local stand-in for GitHub | `↑2` and `↓1` come and go |
| 5. Going further | parallel branches, long branches folding | `├`, `(6)` |

It ends with a page of good habits and an everyday-commands cheat sheet. Everything runs in a temporary repository that is deleted afterwards, so your projects are never touched. Press Ctrl+C to quit at any time.

![gitgraph learn](docs/learn.svg)

## Reading the graph

```
●─●───────●─◆───────────◆─●─────────●    main
  ╰─●─●─●───┴─●─(5)─●─●─╯ ├─●─●          feat/search +2 −1
                          ╰─────●─●───◉  feat/theme +3 −1 ✎2 1h
```

| Symbol | Meaning |
|---|---|
| `●` | a commit |
| `◉` | where you are (HEAD) |
| `◆` | a merge commit: a branch came back here |
| `(5)` | 5 commits folded away |
| `╰` `╯` | a branch starts here / is merged back here |
| `├` | two branches start from the same commit |
| `┴` | one branch merged, the next starts right there |
| `┼` | two lines cross |
| `┄` | forked too long ago to fit |
| dimmed line | a branch that is already merged |
| `+3` / `−1` | 3 commits ahead of main / 1 behind |
| `↑2` / `↓1` | 2 commits to push / 1 to pull |
| `✎2` | 2 files changed but not committed |
| `1h` | time since the branch's last commit (m · h · d) |
| `+2 more` | 2 more branches that didn't fit |
| **bold name** | the branch you're on |

## Options

| Constant in `gitgraph.py` | Default | Controls |
|---|---|---|
| `MAX_COLS` | 30 | how many columns to draw (the graph's width) |
| `MAX_ROWS` | 3 | how many branch rows under main |
| `FOLD_OVER` | 4 | fold runs longer than this |
| `COLORS` | — | colors, as 256-color codes |

- **Color.** When the output is captured (piped into a file or another script), color is turned off. If the receiver can show color, as your own status line script or Claude Code's `!` can, pass `--color`. `--no-color` or `NO_COLOR=1` turns it off.
- **Language.** The language follows `LANG`. `--en` / `--zh` switch it for one run, and `GITGRAPH_LANG=zh` sets it permanently.

## How it works

See [docs/how-it-works.md](docs/how-it-works.md). It covers how forks and merges are read from git, why the columns follow topological order instead of timestamps, and how crossing lines merge into the right box-drawing character.

The images in this README are generated from real `gitgraph` output by `python3 scripts/make_images.py`.

## License

[MIT](LICENSE)
