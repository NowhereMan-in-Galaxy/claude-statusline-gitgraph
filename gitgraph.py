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
import unicodedata

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


def width_of(text):
    """Terminal columns: CJK characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def use_color(argv):
    """Color in a real terminal and in the status line (which renders it);
    plain text when the output is captured, e.g. by Claude Code's `!` prefix."""
    if "--no-color" in argv or "NO_COLOR" in os.environ:
        return False
    return "--color" in argv or "--statusline" in argv or sys.stdout.isatty()


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
    out.append(paint(COLORS["dim"], " more: git log main..BRANCH   ·   git diff --stat main...BRANCH"))
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
    """A beginner's tour of everyday git, run in a throwaway sandbox."""
    p = Paint(color)
    dim = COLORS["dim"]
    box = tempfile.mkdtemp(prefix="gitgraph-learn-")
    repo = os.path.join(box, "my-site")          # the learner's project
    remote = os.path.join(box, "github.git")     # a local folder standing in for GitHub
    mate = os.path.join(box, "teammate")         # a teammate's copy of the project
    os.makedirs(repo)
    clock = [int(time.time()) - 5 * 3600]
    env = dict(os.environ,
               GIT_AUTHOR_NAME="learner", GIT_AUTHOR_EMAIL="learner@example.com",
               GIT_COMMITTER_NAME="learner", GIT_COMMITTER_EMAIL="learner@example.com",
               GIT_CONFIG_COUNT="2",
               GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false",
               GIT_CONFIG_KEY_1="advice.defaultBranchName", GIT_CONFIG_VALUE_1="false",
               LANG="C", LC_ALL="C")

    def tick():
        clock[0] += 5 * 60
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{clock[0]} +0000"

    def sh(command, cwd=repo):
        r = subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True)
        return (r.stdout + r.stderr).strip()

    def say(command, note):
        line = "  " + p(dim, "$ ") + command
        if note:
            line += " " * max(2, 36 - width_of(command)) + p(dim, "# " + note)
        print(line)

    def git_(command, note="", show=False):
        """Run a git command, print it with a comment, optionally show git's own output."""
        if command.startswith(("git commit", "git merge")):
            tick()
        out = sh(command)
        say(command, note)
        if show and out:
            for line in out.splitlines()[:8]:
                print("      " + p(dim, line))

    def edit(path, text, note, append=True):
        with open(os.path.join(repo, path), "a" if append else "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("  " + p(COLORS["dirty"], f"✎ {note}"))

    def aside(text):
        print("  " + p(dim, f"（{text}）"))

    def save(path, text, message, note="存档；-m 后面是这次存档的说明"):
        edit(path, text, f"编辑 {path}")
        git_(f"git add {path}", "把这个文件放进暂存区，准备存档")
        git_(f'git commit -m "{message}"', note)

    # ------------------------------------------------------------------ steps
    def s_init():
        git_("git init -b main", "把当前文件夹变成 git 仓库，主线叫 main")

    def s_first():
        edit("index.html", "<h1>欢迎</h1>", "新建 index.html，写一行标题")
        git_("git status --short", "看看有什么变化；?? 表示 git 还没管这个文件", show=True)
        git_("git add index.html", "把它放进暂存区，意思是\"下次存档要包括它\"")
        git_('git commit -m "feat: add the home page"', "存档；-m 后面是这次存档的说明")

    def s_log():
        save("style.css", "h1 { color: teal; }", "feat: add basic styles")
        git_("git log --oneline", "列出所有存档：编号 + 说明，最新的在上面", show=True)

    def s_branch():
        git_("git switch -c feat/login", "新建分支 feat/login 并切换过去")

    def s_branch_commits():
        save("login.html", "<form>登录</form>", "feat: add the login form")
        save("login.html", "<script>login()</script>", "feat: call the login API")

    def s_main_moves():
        git_("git switch main", "切换回 main")
        save("index.html", "<p>欢迎光临</p>", "fix: correct a typo on the home page")
        git_("git switch feat/login", "再切回分支继续工作")

    def s_dirty():
        edit("login.html", "<p>密码错误</p>", "给 login.html 加一行，先不存档")
        git_("git status --short", " M 表示文件改过了但还没存档", show=True)
        git_("git diff", "看具体改了哪几行；+ 是新增，- 是删除", show=True)

    def s_commit_it():
        git_("git add login.html", "放进暂存区")
        git_('git commit -m "feat: show an error when the password is wrong"', "存档")

    def s_merge():
        git_("git switch main", "合并要站在\"接收方\"：先回到 main")
        git_("git merge --no-ff feat/login -m \"Merge branch 'feat/login'\"",
             "把分支合并进来；--no-ff 保留分叉的形状")
        git_("git branch -d feat/login", "删掉用完的分支名（提交都还在）")

    def s_conflict():
        git_("git switch -c feat/title", "开一个改标题的分支")
        edit("index.html", "<h1>欢迎来到小站</h1>", "把标题改成\"欢迎来到小站\"", append=False)
        git_("git commit -am \"feat: new site title\"", "-a 表示把改过的已跟踪文件一起存档")
        git_("git switch main", "回到 main")
        edit("index.html", "<h1>你好</h1>", "在 main 上把同一行改成\"你好\"", append=False)
        git_("git commit -am \"feat: friendlier title\"", "存档")
        git_("git merge feat/title", "合并……两边改了同一行，git 不知道听谁的", show=True)
        print()
        aside("打开 index.html，git 把两个版本都写了进去：")
        with open(os.path.join(repo, "index.html"), encoding="utf-8") as f:
            for line in f.read().splitlines():
                print("      " + p(COLORS["behind"] if line[:1] in "<=>" and line[:2] in ("<<", "==", ">>") else dim, line))
        print()
        edit("index.html", "<h1>你好，欢迎来到小站</h1>", "手动改成最终想要的样子，删掉 <<< === >>> 标记", append=False)
        git_("git add index.html", "告诉 git：这个冲突解决好了")
        git_("git commit --no-edit", "完成合并；--no-edit 使用默认的合并说明")
        git_("git branch -d feat/title", "删掉分支名")

    def s_remote():
        sh(f"git init -q --bare {remote}", cwd=box)
        aside("用一个本地文件夹 github.git 假装是 GitHub 上的仓库")
        git_("git remote add origin ../github.git", "登记远程仓库，起名叫 origin")
        git_("git push -u origin main", "把 main 上传；-u 让本地 main 记住远程的 main")

    def s_ahead():
        save("about.html", "<p>关于我们</p>", "feat: add the about page")
        save("about.html", "<p>联系方式</p>", "docs: add contact details")

    def s_push():
        git_("git push", "把新的存档上传到远程")

    def s_pull():
        sh(f"git clone -q {remote} {mate}", cwd=box)
        with open(os.path.join(mate, "README.md"), "w", encoding="utf-8") as f:
            f.write("# my-site\n")
        tick()
        sh('git add README.md && git commit -q -m "docs: add a README" && git push -q', cwd=mate)
        aside("与此同时，同事在他的电脑上提交了一次，并推送到了远程")
        git_("git fetch", "去远程看看有什么新东西：只下载，不合并")

    def s_pull_done():
        git_("git pull", "下载并合并到当前分支（相当于 fetch + merge）")

    def s_parallel():
        git_("git switch -c feat/search", "开第一个分支")
        save("search.html", "<input>", "feat: add a search box")
        git_("git switch main", "回到 main")
        git_("git switch -c feat/theme", "再开第二个分支")
        save("style.css", "body { background: #111; }", "feat: add dark mode")
        save("style.css", "img { filter: invert(1); }", "feat: invert icons in dark mode")

    def s_fold():
        for i in range(1, 8):
            with open(os.path.join(repo, "style.css"), "a", encoding="utf-8") as f:
                f.write(f"/* tweak {i} */\n")
            tick()
            sh(f'git commit -qam "style: tweak colors ({i})"')
        print("  " + p(COLORS["dirty"], "✎ 微调了 7 次配色，每次都存档"))
        say('git commit -am "style: tweak colors (n)"', "（重复 7 次）")

    chapters = [
        ("第一章  存档：把工作保存下来", [
            ("新建仓库", s_init,
             "仓库（repository）就是一个会记录历史的文件夹。现在还没有任何存档，所以图是空的。"),
            ("第一次存档", s_first,
             "存档分两步：add 把文件放进\"暂存区\"，commit 把暂存区里的东西存成一个存档（提交）。\n"
             "图上的 ● 就是一次提交，◉ 是你现在所在的位置（HEAD）。\n"
             "提交说明的规范写法（Conventional Commits）是\"类型: 做了什么\"，用英文、短句、动词开头。\n"
             "常用类型：feat 新功能 · fix 修问题 · docs 文档 · style 样式/格式 · refactor 重构 · chore 杂务。"),
            ("再存一次，然后看历史", s_log,
             "git log 列出所有存档。前面那串字母数字是提交编号，以后可以用它找回任何一个存档。\n"
             "好习惯：一次提交只做一件事。这样出了问题，很容易找到是哪一步引起的。"),
        ]),
        ("第二章  分支：在不影响主线的地方做新功能", [
            ("开一个新分支", s_branch,
             "开分支不是提交，不会多出新的 ●。分支只是给当前这个存档点起了一个新名字，\n"
             "并且让你之后的提交都记在这个名字下面。分支名的常见写法是\"类型/简短描述\"，比如 feat/login、fix/typo。"),
            ("在分支上提交", s_branch_commits,
             "分支长出了自己的一行，╰ 是它从 main 分出去的地方。+2 表示它比 main 多 2 次提交。\n"
             "这些提交只在分支上，main 完全不受影响。"),
            ("与此同时，main 也往前走了", s_main_moves,
             "−1 表示 main 有 1 次提交是你的分支还没有的。+ 和 − 同时出现，说明两条线各自往前走了。\n"
             "git switch 用来在分支之间切换，切换时文件夹里的文件会变成那个分支的样子。"),
            ("改了文件，但还没存档", s_dirty,
             "✎1 表示有 1 个文件改了还没存档。git status 看哪些文件变了，git diff 看具体变了什么。\n"
             "好习惯：存档之前先看一眼 diff，确认没有带进不该提交的东西。"),
            ("把它存档", s_commit_it,
             "✎ 消失了，+2 变成了 +3。"),
        ]),
        ("第三章  合并：把分支的成果并回主线", [
            ("合并分支", s_merge,
             "合并产生了一个新的提交（合并提交），平时画成 ◆；你现在正站在它上面，所以显示为 ◉。\n"
             "╯ 是分支回到主线的地方。合并完的分支变暗，名字也删掉了，但它的提交都还在。\n"
             "为什么用 --no-ff：否则在 main 没有新提交时，git 会直接把 main 挪到分支末尾，分叉的形状就看不出来了。"),
            ("合并冲突：两边改了同一行", s_conflict,
             "冲突不是出错，只是 git 在问你：\"同一行被改成了两个样子，要留哪个？\"\n"
             "解决的步骤永远是：打开文件 → 改成想要的样子并删掉 <<< === >>> 标记 → git add → git commit。"),
        ]),
        ("第四章  远程仓库：和 GitHub 同步", [
            ("连上远程仓库，第一次上传", s_remote,
             "远程仓库就是放在别处（比如 GitHub）的一份副本，用来备份和协作。origin 是它的名字，也是最常见的叫法。"),
            ("本地又存了两次档", s_ahead,
             "↑2 表示本地有 2 次提交还没上传。它们只在你的电脑上，电脑坏了就没了。"),
            ("上传", s_push,
             "↑ 消失了：本地和远程一样了。好习惯：做完一段工作就 push，远程就是你的备份。"),
            ("同事也上传了东西", s_pull,
             "↓1 表示远程有 1 次提交你还没拿到。fetch 只是\"看一眼\"，不会改动你的文件。"),
            ("拉下来", s_pull_done,
             "↓ 消失了。好习惯：开始工作前先 pull，push 之前也先 pull，能少遇到很多冲突。"),
        ]),
        ("第五章  进阶：同时做几件事", [
            ("同时开两个分支", s_parallel,
             "每个还没合并的分支各占一行。├ 表示两个分支从同一个存档点分出去。加粗的分支名就是你现在所在的分支。"),
            ("很长的分支会被折叠", s_fold,
             "中间的提交收成了 (n)，让图保持短小。开头、结尾、分叉点和合并点永远不会被折叠。"),
        ]),
    ]

    habits = [
        ("提交说明", "类型: 做了什么，比如 feat: add the login form；一次提交只做一件事"),
        ("分支名", "类型/简短描述，比如 feat/login、fix/typo；做完合并后删掉"),
        ("存档之前", "git status 看改了哪些文件，git diff 看改了什么，别带进密码和私人文件"),
        ("存档频率", "完成一小步就提交；宁可多存几次，也别攒一大堆"),
        ("和远程同步", "开工前 git pull，做完一段就 git push"),
        ("不要做", "已经 push 的历史不要改写（rebase / reset --hard / push --force）"),
        ("用 .gitignore", "把不该进仓库的文件（缓存、密钥、个人数据）写进去，git 就会忽略它们"),
    ]
    commands = [
        ("git status", "现在有哪些改动"), ("git diff", "具体改了什么"),
        ("git add 文件", "放进暂存区"), ("git commit -m \"说明\"", "存档"),
        ("git log --oneline", "看历史"), ("git switch -c 名字", "开新分支"),
        ("git switch 名字", "切换分支"), ("git merge 名字", "把分支合并进来"),
        ("git branch -d 名字", "删掉用完的分支"), ("git push / git pull", "上传 / 下载"),
    ]

    total = sum(len(steps) for _, steps in chapters)
    n = 0
    try:
        print(p("1", "gitgraph 学习模式") + p(dim, f"  （练习仓库放在临时目录里，结束后自动删除）"))
        for title, steps in chapters:
            print()
            print(p("1;" + COLORS["trunk"], title))
            for name, action, note in steps:
                n += 1
                print()
                print(p("1", f"第 {n}/{total} 步  {name}"))
                action()
                graph = render(repo, color)
                if graph:
                    print()
                    for line in graph.splitlines():
                        print("    " + line)
                print()
                for line in note.splitlines():
                    print("  " + line)
                if pause and sys.stdin.isatty():
                    input(p(dim, "\n  按回车继续 ⏎ "))
        print()
        print(p("1;" + COLORS["trunk"], "好习惯速查"))
        for k, v in habits:
            print("  " + p("1", k) + " " * (16 - width_of(k)) + v)
        print()
        print(p("1;" + COLORS["trunk"], "常用命令"))
        for k, v in commands:
            print("  " + k + " " * (26 - width_of(k)) + p(dim, v))
        print()
        print("  学完了。随时运行 " + p("1", "python3 gitgraph.py legend") + " 查看图上的符号。")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        shutil.rmtree(box, ignore_errors=True)


# ---------------------------------------------------------------------- cli

def main(argv):
    color = use_color(argv)
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
