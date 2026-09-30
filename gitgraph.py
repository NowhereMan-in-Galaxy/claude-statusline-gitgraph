#!/usr/bin/env python3
"""gitgraph: a horizontal git branch graph for the terminal and the Claude Code status line.

Time flows left to right. The trunk (main/master) is the top row. Every branch
that forked off it gets a row below, curving out where it forked and, once
merged, curving back in at its merge commit.

    python3 gitgraph.py [PATH]         draw the graph for the repository at PATH
    python3 gitgraph.py --statusline   same, reading Claude Code's status line JSON on stdin
    python3 gitgraph.py log [PATH]     the graph with each branch segment explained
    python3 gitgraph.py legend         what every symbol means
    python3 gitgraph.py learn          a guided tour in a throwaway sandbox repository

Only the Python standard library is used.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

MAX_TRUNK = 14    # trunk commits to show
MAX_LANE = 40     # commits to follow back along one branch
MAX_COLS = 30     # columns to draw; a folded run counts as one column
MAX_ROWS = 3      # branch rows under the trunk
FOLD_OVER = 4     # a run longer than this on one branch is folded into "(n)"
HISTORY = 2000    # commits read from git log

COLORS = {
    "trunk": "38;5;75",                                         # soft blue
    "lanes": ["38;5;176", "38;5;114", "38;5;179", "38;5;110"],  # pink, green, amber, steel
    "head": "1;38;5;222",
    "ahead": "38;5;114",
    "behind": "38;5;174",
    "sync": "38;5;110",
    "dirty": "38;5;179",
    "dim": "2",
}


class Paint:
    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, code, text):
        if not self.enabled or not code or not text:
            return text
        return f"\033[{code}m{text}\033[0m"


def use_color(flag_off=False):
    return not flag_off and "NO_COLOR" not in os.environ


# ---------------------------------------------------------------- reading git

def git(cwd, *args):
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-C", cwd, *args],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def ancestors(tip, parents):
    seen, todo = set(), [tip]
    while todo:
        c = todo.pop()
        if c in seen or c not in parents:
            continue
        seen.add(c)
        todo.extend(parents[c])
    return seen


def walk_first_parent(tip, parents, stop, limit):
    """Follow first parents from tip until a commit in `stop`.
    Returns (commits newest-first, the commit it stopped at or None)."""
    out, c = [], tip
    while c and c in parents and c not in stop and len(out) < limit:
        out.append(c)
        ps = parents[c]
        c = ps[0] if ps else None
    return out, (c if c in stop else None)


class Lane:
    def __init__(self, commits, fork, join, name):
        self.commits = commits      # oldest -> newest
        self.fork = fork            # commit it branched from (or None if unknown)
        self.join = join            # merge commit on the trunk, None while still open
        self.name = name            # branch name for open lanes
        self.row = None
        self.color = None


def read_repo(cwd):
    if not git(cwd, "rev-parse", "--git-dir"):
        return None
    head = git(cwd, "rev-parse", "-q", "--verify", "HEAD")
    if not head:
        return None
    r = {"head": head, "head_branch": git(cwd, "symbolic-ref", "--short", "-q", "HEAD")}

    refs = {}
    fmt = "%(refname:short)%09%(objectname)%09%(upstream:track,nobracket)"
    for row in git(cwd, "for-each-ref", f"--format={fmt}", "refs/heads").splitlines():
        name, sha, track = (row.split("\t") + ["", ""])[:3]
        refs[name] = (sha, track)
    r["refs"] = refs

    order, parents, times = [], {}, {}
    log = git(cwd, "log", "--date-order", f"-n{HISTORY}", "--format=%H %ct %P", "--branches", "HEAD")
    for row in log.splitlines():
        sha, ct, *ps = row.split()
        order.append(sha)
        parents[sha] = ps
        times[sha] = int(ct)
    order.reverse()                      # oldest first; parents always before children
    r.update(order=order, parents=parents, times=times)

    trunk = next((b for b in ("main", "master") if b in refs), r["head_branch"] or None)
    r["trunk"] = trunk
    r["trunk_tip"] = refs[trunk][0] if trunk in refs else head

    status = git(cwd, "status", "--porcelain")
    r["dirty"] = len([x for x in status.splitlines() if x.strip()])

    r["vs_trunk"] = {}
    for name in refs:
        if trunk and name != trunk:
            counts = git(cwd, "rev-list", "--left-right", "--count", f"{trunk}...{name}").split()
            if len(counts) == 2:
                r["vs_trunk"][name] = (int(counts[1]), int(counts[0]))   # (ahead, behind)
    return r


def build_lanes(r):
    parents = r["parents"]
    trunk_commits, _ = walk_first_parent(r["trunk_tip"], parents, set(), MAX_TRUNK)
    trunk_commits.reverse()

    lanes = []
    # branches already merged: the second parent of each merge commit on the trunk
    for m in trunk_commits:
        ps = parents.get(m, [])
        if len(ps) < 2:
            continue
        side, fork = walk_first_parent(ps[1], parents, ancestors(ps[0], parents), MAX_LANE)
        if side:
            lanes.append(Lane(side[::-1], fork, m, None))

    # branches still open; older tips first so a branch cut from another branch
    # stops where that one's commits begin
    claimed = ancestors(r["trunk_tip"], parents)
    stubs = []       # branches with no commits of their own: (name, tip)
    open_refs = [(n, sha) for n, (sha, _) in r["refs"].items() if n != r["trunk"]]
    open_refs.sort(key=lambda x: r["times"].get(x[1], 0))
    for name, sha in open_refs:
        commits, fork = walk_first_parent(sha, parents, claimed, MAX_LANE)
        if commits:
            lanes.append(Lane(commits[::-1], fork, None, name))
            claimed.update(commits)
        else:
            stubs.append((name, sha))
    return trunk_commits, lanes, stubs


# ------------------------------------------------------------------- layout

def layout(r, trunk_commits, lanes):
    """Pick columns (with folding) and rows. Returns (entries, col_of, rows_used, lanes, hidden)."""
    head = r["head"]
    shown = set(trunk_commits)
    owner = {c: "trunk" for c in trunk_commits}
    for i, ln in enumerate(lanes):
        shown.update(ln.commits)
        for c in ln.commits:
            owner[c] = i

    # commits that must stay visible: forks, merges, tips, HEAD
    pinned = {head, *(sha for sha, _ in r["refs"].values())}
    for ln in lanes:
        pinned.update(x for x in (ln.fork, ln.join, ln.commits[-1]) if x)
    pinned.update(c for c in shown if len(r["parents"].get(c, [])) > 1)

    commits = [c for c in r["order"] if c in shown]
    entries, run = [], []

    def flush():
        if len(run) > FOLD_OVER:
            entries.append(("c", run[0]))
            entries.append(("f", owner[run[0]], run[1:-1]))
            entries.append(("c", run[-1]))
        else:
            entries.extend(("c", c) for c in run)
        run.clear()

    for c in commits:
        if run and (owner[c] != owner[run[-1]] or c in pinned):
            flush()
        if c in pinned:
            entries.append(("c", c))
        else:
            run.append(c)
    flush()
    entries = entries[-MAX_COLS:]
    while entries and not (entries[0][0] == "c" and owner[entries[0][1]] == "trunk"):
        entries.pop(0)                  # always start the picture on the trunk

    col_of = {}
    for i, e in enumerate(entries):
        if e[0] == "c":
            col_of[e[1]] = i
        else:
            for c in e[2]:
                col_of[c] = i

    # rows: open branches first (HEAD's branch and the newest ones win), then merged ones
    open_lanes = [ln for ln in lanes if ln.join is None and any(c in col_of for c in ln.commits)]
    open_lanes.sort(key=lambda ln: (head not in ln.commits, -r["times"].get(ln.commits[-1], 0)))
    hidden = max(0, len(open_lanes) - MAX_ROWS)
    open_lanes = open_lanes[:MAX_ROWS]
    open_lanes.sort(key=lambda ln: r["times"].get(ln.commits[-1], 0))
    merged = [ln for ln in lanes if ln.join is not None
              and ln.join in col_of and any(c in col_of for c in ln.commits)]

    row_of = {c: 0 for c in trunk_commits}
    rows = []            # per row: list of (start, end) spans
    placed = []

    def span(ln):
        first = min(col_of[c] for c in ln.commits if c in col_of)
        start = col_of[ln.fork] if ln.fork in col_of and ln.fork in row_of else first - 1
        end = col_of[ln.join] if ln.join else float("inf")
        return start, end

    candidates = open_lanes + sorted(merged, key=lambda ln: span(ln)[0])
    for ln in candidates:
        s, e = span(ln)
        for i, spans in enumerate(rows):
            if all(s >= oe or e <= os_ for os_, oe in spans):
                spans.append((s, e))
                ln.row = i + 1
                break
        else:
            if len(rows) < MAX_ROWS:
                rows.append([(s, e)])
                ln.row = len(rows)
        if ln.row:
            placed.append(ln)
            for c in ln.commits:
                row_of[c] = ln.row
    for i, ln in enumerate(placed):
        ln.color = COLORS["lanes"][i % len(COLORS["lanes"])]
    return entries, col_of, row_of, len(rows), placed, hidden


# ------------------------------------------------------------------ drawing

# box-drawing characters as the directions they reach; overlapping lines are
# merged by combining directions, so crossings always get the right glyph
BOX = {"─": "LR", "│": "UD", "╰": "UR", "╭": "DR", "╯": "UL", "╮": "DL",
       "├": "UDR", "┤": "UDL", "┴": "ULR", "┬": "DLR", "┼": "UDLR"}
BOX_OF = {frozenset(v): k for k, v in BOX.items()}


class Grid:
    def __init__(self, height, width):
        self.cells = [[(" ", "")] * width for _ in range(height)]

    def put(self, r, x, ch, code):
        if not (0 <= r < len(self.cells) and 0 <= x < len(self.cells[r])):
            return
        old = self.cells[r][x][0]
        if ch in BOX and old in BOX:
            ch = BOX_OF.get(frozenset(BOX[ch] + BOX[old]), ch)
        elif ch in BOX and old != " ":
            return                      # never draw a line over a commit
        self.cells[r][x] = (ch, code)

    def text(self, r, x, s, code):
        for i, ch in enumerate(s):
            self.cells[r][x + i] = (ch, code)

    def rows(self, paint):
        out = []
        for row in self.cells:
            n = len(row)
            while n and row[n - 1][0] == " ":
                n -= 1
            out.append((n, "".join(paint(code, ch) for ch, code in row[:n])))
        return out


def human_age(seconds):
    m = seconds // 60
    if m < 1:
        return "now"
    if m < 60:
        return f"{m}m"
    if m < 60 * 24:
        return f"{m // 60}h"
    d = m // (60 * 24)
    return f"{d}d" if d < 60 else f"{d // 30}mo"


def branch_info(r, name, paint):
    parts = []
    ahead, behind = r["vs_trunk"].get(name, (0, 0))
    if ahead:
        parts.append(paint(COLORS["ahead"], f"+{ahead}"))
    if behind:
        parts.append(paint(COLORS["behind"], f"−{behind}"))
    track = r["refs"].get(name, ("", ""))[1]
    for word in track.split(","):
        word = word.strip()
        if word.startswith("ahead "):
            parts.append(paint(COLORS["sync"], "↑" + word[6:]))
        elif word.startswith("behind "):
            parts.append(paint(COLORS["sync"], "↓" + word[7:]))
    if name == r["head_branch"]:
        if r["dirty"]:
            parts.append(paint(COLORS["dirty"], f"✎{r['dirty']}"))
        tip = r["refs"][name][0]
        parts.append(paint(COLORS["dim"], human_age(int(time.time()) - r["times"].get(tip, int(time.time())))))
    return " ".join(parts)


def render(cwd, color=True, details=False):
    paint = Paint(color)
    r = read_repo(cwd)
    if not r:
        return ""
    trunk_commits, lanes, stubs = build_lanes(r)
    entries, col_of, row_of, nrows, lanes, hidden = layout(r, trunk_commits, lanes)
    if not entries:
        return ""

    widths = [2 if e[0] == "c" else len(f"({len(e[2])})") + 1 for e in entries]
    xs = [sum(widths[:i]) for i in range(len(widths))]
    grid = Grid(nrows + 1, sum(widths) + 1)
    parents, head = r["parents"], r["head"]

    def node(c):
        if c == head:
            return "◉", COLORS["head"]
        return ("◆" if len(parents.get(c, [])) > 1 else "●"), None

    def draw_nodes(commits, row, code):
        done = set()
        for c in commits:
            if c not in col_of or col_of[c] in done:
                continue
            i = col_of[c]
            done.add(i)
            e = entries[i]
            if e[0] == "f":
                grid.text(row, xs[i], f"({len(e[2])})", COLORS["dim"] + ";" + code)
            else:
                ch, special = node(c)
                grid.put(row, xs[i], ch, special or code)

    # trunk
    tcols = [col_of[c] for c in trunk_commits if c in col_of]
    if tcols:
        for x in range(xs[min(tcols)], xs[max(tcols)] + 1):
            grid.put(0, x, "─", COLORS["trunk"])
        draw_nodes(trunk_commits, 0, COLORS["trunk"])

    # branches
    for ln in lanes:
        code = ln.color if ln.join is None else COLORS["dim"] + ";" + ln.color
        vis = [col_of[c] for c in ln.commits if c in col_of]
        first, last = min(vis), max(vis)
        if ln.fork in col_of and ln.fork in row_of:
            sx, fr = xs[col_of[ln.fork]], row_of[ln.fork]
            lo, hi = sorted((fr, ln.row))
            for rr in range(lo + 1, hi):
                grid.put(rr, sx, "│", code)
            grid.put(ln.row, sx, "╰" if fr < ln.row else "╭", code)
            x0 = sx + 1
        else:
            x0 = xs[first]
            grid.put(ln.row, x0 - 1, "┄", code)
        if ln.join:
            jx = xs[col_of[ln.join]]
            for rr in range(1, ln.row):
                grid.put(rr, jx, "│", code)
            grid.put(ln.row, jx, "╯", code)
            x1 = jx
        else:
            x1 = xs[last]
        for x in range(x0, x1):
            grid.put(ln.row, x, "─", code)
        draw_nodes(ln.commits, ln.row, code)

    # details mode: tag each branch segment with a letter, explained below the graph
    tagged = []
    if details:
        for ln in sorted(lanes, key=lambda l: min(col_of[c] for c in l.commits if c in col_of)):
            spot = next((c for c in ln.commits if c in col_of and c != head
                         and entries[col_of[c]][0] == "c"), None)
            if spot and len(tagged) < 26:
                letter = chr(ord("A") + len(tagged))
                grid.text(ln.row, xs[col_of[spot]], letter, "1;" + ln.color)
                tagged.append((letter, ln))

    # labels: branch names and their info, aligned in one column
    labels = {i: [] for i in range(nrows + 1)}
    if r["trunk"] in r["refs"]:
        labels[0].append((r["trunk"], COLORS["trunk"]))
    for ln in lanes:
        if ln.join is None:
            labels[ln.row].append((ln.name, ln.color))
    for name, sha in stubs:
        if sha in row_of:
            labels[row_of[sha]].append((name, COLORS["dim"]))
    if not r["head_branch"] and head in row_of:
        labels[row_of[head]].append(("(detached)", COLORS["dim"]))

    drawn = grid.rows(paint)
    pad = max(n for n, _ in drawn) + 2
    out = []
    for i, (n, s) in enumerate(drawn):
        items = []
        for name, code in labels[i]:
            if name == r["head_branch"]:
                code = "1;" + code
            info = branch_info(r, name, paint) if name in r["refs"] else ""
            items.append(paint(code, name) + (" " + info if info else ""))
        if i == 0 and hidden:
            items.append(paint(COLORS["dim"], f"+{hidden} more"))
        line = s + (" " * (pad - n) + "  ".join(items) if items else "")
        out.append(line.rstrip())
    if tagged:
        out += [""] + segment_table(cwd, r, tagged, paint)
    return "\n".join(out)


def merge_summary(subject):
    """Split a merge commit subject into (branch name, what it did)."""
    if subject.startswith("Merge branch '"):
        name, _, rest = subject[len("Merge branch '"):].partition("'")
        rest = rest.lstrip(" :")
        if rest.startswith("into "):
            rest = ""
        return name, rest
    for prefix in ("merge: ", "Merge: "):
        if subject.startswith(prefix):
            return "", subject[len(prefix):]
    if subject.startswith("Merge pull request"):
        return "", ""
    return "", subject


def short_stat(cwd, base, tip):
    words = git(cwd, "diff", "--shortstat", base, tip).replace(",", "").split()
    files = plus = minus = 0
    for i, w in enumerate(words):
        if w.startswith("file"):
            files = int(words[i - 1])
        elif w.startswith("insertion"):
            plus = int(words[i - 1])
        elif w.startswith("deletion"):
            minus = int(words[i - 1])
    text = f"{files} file" + ("s" if files != 1 else "")
    return text, plus, minus


def segment_table(cwd, r, tagged, paint):
    """One line per tagged branch segment: what it is, how big, when, and what it did."""
    rows = []
    for letter, ln in tagged:
        first, tip = ln.commits[0], ln.commits[-1]
        subject = lambda c: git(cwd, "log", "-1", "--format=%s", c)
        if ln.join:
            name, what = merge_summary(subject(ln.join))
            state = "merged"
        else:
            name, what, state = ln.name, "", "open"
        what = what or subject(tip)
        base = ln.fork or (r["parents"].get(first) or [first])[0]
        files, plus, minus = short_stat(cwd, base, tip)
        day = lambda c: time.strftime("%b %d", time.localtime(r["times"].get(c, 0))).replace(" 0", " ")
        start, end = day(first), ("now" if state == "open" else day(tip))
        when = start if start == end else f"{start} → {end}"
        n = len(ln.commits)
        rows.append({
            "letter": (letter, "1;" + ln.color),
            "name": (name or "·", ln.color if state == "open" else COLORS["dim"]),
            "state": (state, COLORS["ahead"] if state == "open" else COLORS["dim"]),
            "size": (f"{n} commit" + ("s" if n != 1 else ""), ""),
            "files": (files, ""),
            "plus": (f"+{plus}", COLORS["ahead"]),
            "minus": (f"−{minus}", COLORS["behind"]),
            "when": (when, COLORS["dim"]),
            "what": (what if len(what) <= 64 else what[:63] + "…", ""),
        })
    keys = ["letter", "name", "state", "size", "files", "plus", "minus", "when", "what"]
    width = {k: max(len(row[k][0]) for row in rows) for k in keys}
    right = {"plus", "minus"}
    out = []
    for row in rows:
        cells = []
        for k in keys:
            text, code = row[k]
            padded = text.rjust(width[k]) if k in right else text.ljust(width[k])
            cells.append(paint(code, padded) if k != "what" else paint(code, text))
        out.append(" " + "  ".join(cells).rstrip())
    out.append("")
    out.append(paint(COLORS["dim"], " more: git log main..<branch>   ·   git diff --stat main...<branch>"))
    return out


# ------------------------------------------------------------------- legend

def legend(color=True):
    p = Paint(color)
    t, l1, l2 = COLORS["trunk"], COLORS["lanes"][0], COLORS["lanes"][1]
    dim = COLORS["dim"]
    rows = [
        ("", "", "图上的符号"),
        (t, "●", "一次提交，也就是一个存档点"),
        (COLORS["head"], "◉", "你现在所在的位置（HEAD）"),
        (t, "◆", "合并提交：有分支在这里并回来"),
        (dim + ";" + l1, "(5)", "折叠起来的 5 次提交"),
        (l1, "╰", "分支从这里分出去"),
        (l1, "╯", "分支在这里合并回主线"),
        (l1, "├", "两个分支从同一个存档点分出去"),
        (l1, "┴", "一个分支刚合并，下一个紧接着分出去"),
        (l1, "┼", "两条线在这里交叉而过"),
        (l1, "┄", "分叉点太早，不在图里"),
        (t, "─", "时间从左往右走；第一行永远是主线 main"),
        (dim + ";" + l2, "──", "暗色的线：已经合并完的旧分支"),
        ("", "", ""),
        ("", "", "分支名后面的小标记"),
        (COLORS["ahead"], "+3", "比 main 多 3 次提交（还没合并进 main 的工作）"),
        (COLORS["behind"], "−1", "比 main 少 1 次提交（main 有你还没同步的新东西）"),
        (COLORS["sync"], "↑2", "有 2 次提交还没推送到远程仓库（git push）"),
        (COLORS["sync"], "↓1", "远程仓库有 1 次提交你还没拉下来（git pull）"),
        (COLORS["dirty"], "✎2", "有 2 个文件改了还没提交"),
        (COLORS["dim"], "12m", "距离这个分支上次提交过了多久（m 分钟 · h 小时 · d 天）"),
        (COLORS["dim"], "+2 more", "还有 2 个分支因为放不下没画出来"),
    ]
    out = []
    for code, sym, text in rows:
        if not sym:
            out.append(p("1", text) if text else "")
        else:
            out.append("  " + p(code, sym) + " " * (9 - len(sym)) + text)
    return "\n".join(out)


# -------------------------------------------------------------------- learn

def learn(pause=True, color=True):
    """Walk through the everyday branch workflow in a throwaway repository."""
    p = Paint(color)
    box = tempfile.mkdtemp(prefix="gitgraph-learn-")
    clock = [int(time.time()) - 3 * 3600]
    env = dict(os.environ,
               GIT_AUTHOR_NAME="learner", GIT_AUTHOR_EMAIL="learner@example.com",
               GIT_COMMITTER_NAME="learner", GIT_COMMITTER_EMAIL="learner@example.com",
               GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false")

    def sh(cmd):
        subprocess.run(cmd, shell=True, cwd=box, env=env, capture_output=True)

    def show(cmd):
        print("  " + p(COLORS["dim"], "$ ") + cmd)

    def run(cmd):
        show(cmd)
        sh(cmd + " -q" if cmd.startswith("git switch") else cmd)

    def commit(msg, path):
        clock[0] += 5 * 60
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{clock[0]} +0000"
        show(f'git commit -m "{msg}"')
        sh(f'echo "{msg}" >> {path} && git add -A && git commit -q -m "{msg}"')

    def merge(branch):
        clock[0] += 5 * 60
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{clock[0]} +0000"
        run(f"git merge --no-ff --no-edit {branch}")

    steps = [
        ("新建仓库，在 main 上存两次档",
         lambda: (run("git init -b main"), commit("搭好项目骨架", "app.txt"), commit("写首页", "home.txt")),
         "每个 ● 是一次提交，就像游戏存档。线从左往右是时间，越往右越新。\n"
         "◉ 是你现在所在的位置（叫 HEAD）。第一行永远是主线 main。"),
        ("开一个新分支",
         lambda: run("git switch -c feat/login"),
         "分支就是给某个存档点起了一个新名字。它现在和 main 指着同一个点，\n"
         "所以图上还没有新的线，只是 main 旁边多了 feat/login 这个名字。"),
        ("在分支上提交两次",
         lambda: (commit("写登录表单", "login.txt"), commit("接上登录接口", "login.txt")),
         "分支长出了自己的一行，╰ 是它从 main 分出去的地方。\n"
         "+2 表示它比 main 多 2 次提交；后面的时间是距离上次提交过了多久。\n"
         "这些提交只在分支上，main 完全不受影响。"),
        ("与此同时，main 也往前走了",
         lambda: (run("git switch main"), commit("修首页错别字", "home.txt"), run("git switch feat/login")),
         "−1 表示 main 有 1 次提交是你的分支还没有的。\n"
         "+ 和 − 同时出现，说明两条线各自往前走了；合并时 git 会把两边的改动合到一起。"),
        ("改了文件，但还没提交",
         lambda: (show("（编辑 login.txt，先不提交）"), sh("echo draft >> login.txt")),
         "✎1 表示有 1 个文件改了还没提交。\n"
         "它在提醒你：这些改动还没存档，出了意外就找不回来。"),
        ("把它提交",
         lambda: commit("登录失败时给出提示", "login.txt"),
         "✎ 消失了，+2 变成了 +3。"),
        ("把分支合并回 main",
         lambda: (run("git switch main"), merge("feat/login"), run("git branch -d feat/login")),
         "◆ 是合并提交，╯ 是分支回到主线的地方。\n"
         "合并完的分支变暗，名字也删掉了。它的提交都还在，只是不再需要一个名字指着它。"),
        ("同时开两个分支",
         lambda: (commit("更新说明文档", "readme.txt"),
                  run("git switch -c feat/search"), commit("加搜索框", "search.txt"),
                  run("git switch main"), run("git switch -c feat/theme"),
                  commit("加深色模式", "theme.txt"), commit("深色模式下的图标", "theme.txt")),
         "每个还没合并的分支各占一行。├ 表示两个分支从同一个存档点分出去。\n"
         "加粗的分支名就是你现在所在的分支。"),
        ("很长的分支会被折叠",
         lambda: [commit(f"调整配色 {i}", "theme.txt") for i in range(1, 8)],
         "中间的提交收成了 (n)，让图保持短小。\n"
         "开头、结尾、分叉点和合并点永远不会被折叠。"),
    ]

    try:
        print(p("1", "gitgraph 学习模式") + p(COLORS["dim"], f"  （沙盒仓库：{box}，结束后自动删除）"))
        for i, (title, action, note) in enumerate(steps, 1):
            print()
            print(p("1", f"第 {i}/{len(steps)} 步  {title}"))
            action()
            print()
            for line in render(box, color).splitlines():
                print("    " + line)
            print()
            for line in note.splitlines():
                print("  " + line)
            if pause and sys.stdin.isatty() and i < len(steps):
                input(p(COLORS["dim"], "\n  按回车继续 ⏎ "))
        print()
        print("  学完了。随时运行 " + p("1", "python3 gitgraph.py legend") + " 查看符号表。")
    finally:
        shutil.rmtree(box, ignore_errors=True)


# ---------------------------------------------------------------------- cli

def main(argv):
    color = use_color("--no-color" in argv)
    args = [a for a in argv if not a.startswith("--")]
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
    elif args[:1] == ["legend"]:
        print(legend(color))
    elif args[:1] == ["log"]:
        out = render(args[1] if len(args) > 1 else ".", color, details=True)
        print(out or "not inside a git repository")
    elif args[:1] == ["learn"]:
        learn(pause="--no-pause" not in argv, color=color)
    elif "--statusline" in argv:
        try:
            data = json.load(sys.stdin)
        except ValueError:
            data = {}
        cwd = (data.get("workspace") or {}).get("current_dir") or data.get("cwd") or os.getcwd()
        out = render(cwd, color)
        if out:
            print(out)
    else:
        out = render(args[0] if args else ".", color)
        if out:
            print(out)


if __name__ == "__main__":
    main(sys.argv[1:])
