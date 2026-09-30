# How it works

**English** · [中文](how-it-works.zh-CN.md)

The whole program is one file, `gitgraph.py`. It helps to look at it in two layers.

## Layer 1: the Claude Code status line

Claude Code's status line is a simple mechanism. It runs the command you set in `settings.json`, passes the current session as JSON on standard input, and displays whatever the command prints under the prompt.

So a status line is really **a script that runs on a timer, with its output pinned under the prompt**. `gitgraph.py --statusline` reads `workspace.current_dir` from that JSON to know which repository to draw.

## Layer 2: drawing, in four steps

### 1. Ask git for the data

One `git log --date-order --format="%H %ct %P"` returns the recent commits and each one's parents. Git's history is a graph of "who is whose parent", and everything after this point is computed on that graph in Python instead of calling git again and again.

- **The trunk.** Start at main's tip and keep following the *first* parent. At a merge, the first parent is where main was before the merge, so this walk traces main's own line.
- **Merged branches.** A merge commit has two parents. The second one is the branch that came in. Walk back from it until you reach a commit main already has; that commit is the fork point.
- **Open branches.** Walk back from each branch tip until you reach a commit main already has. A branch cut from another branch stops at that branch's commits, so branches of branches draw correctly too.

### 2. Columns: topological order, not timestamps

Each commit gets a column, and **a parent must always be left of its children**.

Sorting by commit time looks natural but breaks. `rebase` and `commit --amend` rewrite timestamps, so a child can end up with an earlier time than its parent, and lines would run backwards. Instead the columns use git's own `--date-order`, which keeps parents first and sorts by time only where it's free to.

Then runs of more than four ordinary commits on one branch fold into `(n)`. Fork points, merges, branch tips and HEAD never fold, or the lines couldn't connect.

### 3. Rows: like booking meeting rooms

main is always the top row. Each branch occupies a span of time, from where it forked to where it merged.

- Branches whose spans don't overlap can share a row, the way meetings at different times can share a room.
- An open branch's span runs to the right edge, so each open branch gets its own row.
- Open branches come first and each gets a row, up to eight, so five subagents at work means five rows. Merged branches only fill the gaps, and add rows only while there are fewer than three. Once the work is merged, the graph shrinks back.
- Past eight open branches, the rest are listed by name as `+N more: …` next to main. The branch you're on is always drawn.

### 4. Draw on a character grid

Picture graph paper with one character per cell. The hard part is several lines passing through the same cell. A vertical line crossing another branch's corner should become `├`, not whichever character was drawn last.

So each box-drawing character is treated as **the set of directions it connects**:

```
─ = left right   │ = up down   ╰ = up right   ╯ = up left
├ = up down right   ┴ = up left right   ┼ = all four   …
```

When two lines land in one cell, their directions are combined and turned back into a character. `│` plus `╰` is {up, down, right}, which is `├`. However many lines overlap, the corner comes out right.

Last, each cell gets an ANSI color code, and the branch names and markers like `+3 −1 ✎2` are aligned into one column on the right.

## Lessons learned

- **The status line must be fast.** It runs every few seconds, so gitgraph makes only a handful of git calls and does the rest in Python. It also passes `--no-optional-locks` so it never fights your own git commands for a lock.
- **The tutorial uses a sandbox.** `learn` creates a repository in a temp folder and deletes it at the end, so real projects are never touched. Commit times come from a fake clock that ticks forward, which keeps the demo's order stable.
- **Terminals can't do hover.** A status line is plain text, and a terminal can't attach a tooltip to one character. That's why the details live in `gitgraph log` instead.
- **Know when to color.** Colors are escape codes. They're right for a terminal, but noise in a file. gitgraph colors only when writing to a terminal, unless `--color` says the receiver can handle it.
