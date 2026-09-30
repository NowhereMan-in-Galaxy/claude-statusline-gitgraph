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
    python3 gitgraph.py demo           a simulated team with subagents working in parallel
    python3 gitgraph.py themes [PATH]  the graph in every color theme
    python3 gitgraph.py theme NAME     switch theme (saved in ~/.config/gitgraph/theme)

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

# Color themes, as 256-color codes. "lanes" cycles through the open branches;
# "merged" colors merged branches (None: the lane's own color, dimmed).
# Each branch already has its own row, so most themes use one hue for all of them.
THEMES = {
    "default": {                                                    # a color per branch
        "trunk": "38;5;75",
        "lanes": ["38;5;176", "38;5;114", "38;5;179", "38;5;110"],  # pink, green, amber, steel
        "head": "1;38;5;222", "ahead": "38;5;114", "behind": "38;5;174",
        "sync": "38;5;110", "dirty": "38;5;179", "merged": None,
    },
    "gold": {                                                       # black and gold, for dark terminals
        "trunk": "38;5;178",
        "lanes": ["38;5;222", "38;5;180", "38;5;137"],              # shades of gold
        "head": "1", "ahead": "38;5;186", "behind": "38;5;173",
        "sync": "38;5;250", "dirty": "38;5;214", "merged": "38;5;94",
    },
    "mint": {                                                       # silver and green, for dark terminals
        "trunk": "38;5;252",
        "lanes": ["38;5;114", "38;5;79", "38;5;151"],               # shades of green
        "head": "1;38;5;48", "ahead": "38;5;114", "behind": "38;5;246",
        "sync": "38;5;159", "dirty": "38;5;229", "merged": "38;5;240",
    },
    "quiet": {                                                      # color only for what needs doing
        "trunk": "38;5;75",
        "lanes": ["38;5;110"],
        "head": "1", "ahead": "2", "behind": "2",
        "sync": "38;5;214", "dirty": "38;5;214", "merged": "38;5;243",
    },
    "colorblind": {                                                 # Okabe-Ito colors; no red/green pairs
        "trunk": "38;5;74",
        "lanes": ["38;5;214"],
        "head": "1", "ahead": "38;5;74", "behind": "38;5;166",
        "sync": "1", "dirty": "1;38;5;172", "merged": "38;5;243",
    },
}
# Light-background versions, used when the system is in light mode.
# Themes without one (gold, mint) are made for dark terminals and stay as they are.
LIGHT = {
    "default": {
        "trunk": "38;5;32",
        "lanes": ["38;5;133", "38;5;28", "38;5;136", "38;5;67"],
        "head": "1;38;5;130", "ahead": "38;5;28", "behind": "38;5;131",
        "sync": "38;5;25", "dirty": "38;5;136", "merged": "38;5;248",
    },
    "quiet": {
        "trunk": "38;5;32",
        "lanes": ["38;5;67"],
        "head": "1", "ahead": "2", "behind": "2",
        "sync": "38;5;166", "dirty": "38;5;166", "merged": "38;5;248",
    },
    "colorblind": {
        "trunk": "38;5;25",
        "lanes": ["38;5;172"],
        "head": "1", "ahead": "38;5;25", "behind": "38;5;166",
        "sync": "1", "dirty": "1;38;5;130", "merged": "38;5;248",
    },
}
# The terminal's own 16 colors: the terminal's theme decides the actual shades.
THEMES["terminal"] = {
    "trunk": "34", "lanes": ["35", "32", "33", "36"],
    "head": "1", "ahead": "32", "behind": "31",
    "sync": "36", "dirty": "33", "merged": None,
}
THEME_NAMES = {"default": "默认", "gold": "黑金", "mint": "银绿", "quiet": "素净",
               "colorblind": "色弱友好", "terminal": "跟随终端配色"}
COLORS = dict(THEMES["default"], dim="2")


class Paint:
    def __init__(self, enabled):
        self.enabled = enabled

    def __call__(self, code, text):
        if not self.enabled or not code or not text:
            return text
        return f"\033[{code}m{text}\033[0m"


LANG = "en"


def tr(en, zh):
    """Pick the string for the current language."""
    return zh if LANG == "zh" else en


def pick_lang(argv):
    """--zh / --en win, then GITGRAPH_LANG, then the system locale."""
    if "--zh" in argv:
        return "zh"
    if "--en" in argv:
        return "en"
    env = os.environ.get("GITGRAPH_LANG") or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    return "zh" if env.lower().startswith("zh") else "en"


def pad(text, n, right=False):
    """ljust/rjust by terminal width, so CJK text lines up."""
    gap = " " * max(0, n - width_of(text))
    return gap + text if right else text + gap


def width_of(text):
    """Terminal columns: CJK characters take two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def appearance():
    """"light" or "dark": GITGRAPH_APPEARANCE, then the terminal's COLORFGBG, then the system setting."""
    forced = os.environ.get("GITGRAPH_APPEARANCE", "").lower()
    if forced in ("light", "dark"):
        return forced
    bg = os.environ.get("COLORFGBG", "").split(";")[-1]
    if bg.isdigit():
        return "light" if bg in ("7", "15") else "dark"
    if sys.platform == "darwin":
        probe = ["defaults", "read", "-g", "AppleInterfaceStyle"]      # prints "Dark", or fails in light mode
        dark = lambda out, ok: ok and "dark" in out.lower()
    elif sys.platform == "win32":
        probe = ["reg", "query", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                 "/v", "AppsUseLightTheme"]
        dark = lambda out, ok: not ok or "0x0" in out
    else:
        probe = ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"]
        dark = lambda out, ok: not ok or "dark" in out.lower()
    try:
        r = subprocess.run(probe, capture_output=True, text=True, timeout=1)
        return "dark" if dark(r.stdout, r.returncode == 0) else "light"
    except (OSError, subprocess.SubprocessError):
        return "dark"


def palette(name):
    """A theme's colors, in its light version when the system is in light mode."""
    if name in LIGHT and appearance() == "light":
        return LIGHT[name]
    return THEMES[name]


def theme_file():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "gitgraph", "theme")


def saved_theme():
    try:
        with open(theme_file(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def pick_theme(argv):
    """--theme=NAME wins, then GITGRAPH_THEME, then the saved theme. None if unknown."""
    name = next((a.split("=", 1)[1] for a in argv if a.startswith("--theme=")),
                os.environ.get("GITGRAPH_THEME") or saved_theme() or "default")
    if name not in THEMES:
        return None
    COLORS.update(palette(name))
    return name


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
        code = ln.color if ln.join is None else COLORS["merged"] or COLORS["dim"] + ";" + ln.color
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
    return files, plus, minus


def plural(n, en_one, en_many, zh):
    return tr(f"{n} {en_one if n == 1 else en_many}", f"{n} {zh}")


def day_of(ts):
    t = time.localtime(ts)
    return tr(time.strftime("%b ", t) + str(t.tm_mday), f"{t.tm_mon}月{t.tm_mday}日")


def segment_table(cwd, r, tagged, paint):
    """One line per tagged branch segment: what it is, how big, when, and what it did."""
    rows = []
    for letter, ln in tagged:
        first, tip = ln.commits[0], ln.commits[-1]
        subject = lambda c: git(cwd, "log", "-1", "--format=%s", c)
        if ln.join:
            name, what = merge_summary(subject(ln.join))
        else:
            name, what = ln.name, ""
        is_open = ln.join is None
        what = what or subject(tip)
        base = ln.fork or (r["parents"].get(first) or [first])[0]
        files, plus, minus = short_stat(cwd, base, tip)
        start = day_of(r["times"].get(first, 0))
        end = tr("now", "至今") if is_open else day_of(r["times"].get(tip, 0))
        when = start if start == end else f"{start} → {end}"
        rows.append({
            "letter": (letter, "1;" + ln.color),
            "name": (name or "·", ln.color if is_open else COLORS["dim"]),
            "state": (tr("open", "进行中") if is_open else tr("merged", "已合并"),
                      COLORS["ahead"] if is_open else COLORS["dim"]),
            "size": (plural(len(ln.commits), "commit", "commits", "次提交"), ""),
            "files": (plural(files, "file", "files", "个文件"), ""),
            "plus": (f"+{plus}", COLORS["ahead"] if plus else COLORS["dim"]),
            "minus": (f"−{minus}", COLORS["behind"] if minus else COLORS["dim"]),
            "when": (when, COLORS["dim"]),
            "what": (what if len(what) <= 64 else what[:63] + "…", ""),
        })
    keys = ["letter", "name", "state", "size", "files", "plus", "minus", "when", "what"]
    width = {k: max(width_of(row[k][0]) for row in rows) for k in keys}
    out = []
    for row in rows:
        cells = []
        for k in keys:
            text, code = row[k]
            cells.append(paint(code, text if k == "what" else pad(text, width[k], k in ("plus", "minus"))))
        out.append(" " + "  ".join(cells).rstrip())
    out.append("")
    out.append(paint(COLORS["dim"], tr(" more: git log main..BRANCH   ·   git diff --stat main...BRANCH",
                                       " 更多：git log main..分支名   ·   git diff --stat main...分支名")))
    return out


# ------------------------------------------------------------------- legend

def legend(color=True):
    p = Paint(color)
    lanes = COLORS["lanes"]
    t, l1, l2 = COLORS["trunk"], lanes[0], lanes[1 % len(lanes)]
    dim = COLORS["dim"]
    rows = [
        ("", "", "The graph", "图上的符号"),
        (t, "●", "a commit: one saved snapshot", "一次提交，也就是一个存档点"),
        (COLORS["head"], "◉", "where you are now (HEAD)", "你现在所在的位置（HEAD）"),
        (t, "◆", "a merge commit: a branch came back here", "合并提交：有分支在这里并回来"),
        (dim + ";" + l1, "(5)", "5 commits folded away", "折叠起来的 5 次提交"),
        (l1, "╰", "a branch starts here", "分支从这里分出去"),
        (l1, "╯", "a branch is merged back here", "分支在这里合并回主线"),
        (l1, "├", "two branches start from the same commit", "两个分支从同一个存档点分出去"),
        (l1, "┴", "one branch merged, the next starts right there", "一个分支刚合并，下一个紧接着分出去"),
        (l1, "┼", "two lines cross", "两条线在这里交叉而过"),
        (l1, "┄", "forked too long ago to fit", "分叉点太早，不在图里"),
        (t, "─", "time flows left to right; the top row is always main", "时间从左往右走；第一行永远是主线 main"),
        (COLORS["merged"] or dim + ";" + l2, "──", "dimmed: a branch that is already merged", "暗色的线：已经合并完的旧分支"),
        ("", "", "", ""),
        ("", "", "Next to a branch name", "分支名后面的小标记"),
        (COLORS["ahead"], "+3", "3 commits not in main yet", "比 main 多 3 次提交（还没合并进 main 的工作）"),
        (COLORS["behind"], "−1", "main has 1 commit this branch doesn't", "比 main 少 1 次提交（main 有你还没同步的新东西）"),
        (COLORS["sync"], "↑2", "2 commits not pushed yet (git push)", "有 2 次提交还没推送到远程仓库（git push）"),
        (COLORS["sync"], "↓1", "1 commit on the remote you haven't pulled (git pull)", "远程仓库有 1 次提交你还没拉下来（git pull）"),
        (COLORS["dirty"], "✎2", "2 files changed but not committed", "有 2 个文件改了还没提交"),
        (dim, "12m", "time since the last commit (m · h · d)", "距离这个分支上次提交过了多久（m 分钟 · h 小时 · d 天）"),
        (dim, "+2 more", "2 more branches that didn't fit", "还有 2 个分支因为放不下没画出来"),
    ]
    out = []
    for code, sym, en, zh in rows:
        text = tr(en, zh)
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

    def run(command, note="", show=False):
        """Run a git command, print it with a comment, optionally show git's own output."""
        if command.startswith(("git commit", "git merge")):
            tick()
        out = sh(command)
        say(command, note)
        if show and out:
            for line in out.splitlines()[:10]:
                code = dim
                if line.startswith("+") and not line.startswith("+++"):
                    code = COLORS["ahead"]
                elif line.startswith("-") and not line.startswith("---"):
                    code = COLORS["behind"]
                print("      " + p(code, line))

    def edit(path, text, note, append=True):
        with open(os.path.join(repo, path), "a" if append else "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("  " + p(COLORS["dirty"], "✎ " + note))

    def aside(text):
        print("  " + p(dim, tr(f"({text})", f"（{text}）")))

    def save(path, text, message):
        edit(path, text, tr(f"edit {path}", f"编辑 {path}"))
        run(f"git add {path}", tr("stage it: include it in the next commit", "把这个文件放进暂存区，准备存档"))
        run(f'git commit -m "{message}"', tr("commit; -m is the message", "存档；-m 后面是这次存档的说明"))

    # ------------------------------------------------------------------ steps
    def s_init():
        run("git init -b main", tr("make this folder a repository; the trunk is main",
                                   "把当前文件夹变成 git 仓库，主线叫 main"))

    def s_first():
        edit("index.html", tr("<h1>Welcome</h1>", "<h1>欢迎</h1>"),
             tr("create index.html with a heading", "新建 index.html，写一行标题"))
        run("git status --short", tr("what changed? ?? = git isn't tracking it yet",
                                     "看看有什么变化；?? 表示 git 还没管这个文件"), show=True)
        run("git add index.html", tr("stage it: \"include this in the next commit\"",
                                     "把它放进暂存区，意思是\"下次存档要包括它\""))
        run('git commit -m "feat: add the home page"', tr("commit; -m is the message", "存档；-m 后面是这次存档的说明"))

    def s_log():
        save("style.css", "h1 { color: teal; }", "feat: add basic styles")
        run("git log --oneline", tr("list commits: id + message, newest first",
                                    "列出所有存档：编号 + 说明，最新的在上面"), show=True)

    def s_branch():
        run("git switch -c feat/login", tr("create branch feat/login and move onto it", "新建分支 feat/login 并切换过去"))

    def s_branch_commits():
        save("login.html", tr("<form>Log in</form>", "<form>登录</form>"), "feat: add the login form")
        save("login.html", "<script>login()</script>", "feat: call the login API")

    def s_main_moves():
        run("git switch main", tr("back to main", "切换回 main"))
        save("index.html", tr("<p>Hello!</p>", "<p>欢迎光临</p>"), "fix: correct a typo on the home page")
        run("git switch feat/login", tr("back to the branch", "再切回分支继续工作"))

    def s_dirty():
        edit("login.html", tr("<p>Wrong password</p>", "<p>密码错误</p>"),
             tr("add a line to login.html, don't commit yet", "给 login.html 加一行，先不存档"))
        run("git status --short", tr(" M = modified, not committed", " M 表示文件改过了但还没存档"), show=True)
        run("git diff", tr("the exact lines: + added, - removed", "看具体改了哪几行；+ 是新增，- 是删除"), show=True)

    def s_commit_it():
        run("git add login.html", tr("stage it", "放进暂存区"))
        run('git commit -m "feat: show an error when the password is wrong"', tr("commit", "存档"))

    def s_merge():
        run("git switch main", tr("merge from the receiving side: go to main", "合并要站在\"接收方\"：先回到 main"))
        run("git merge --no-ff feat/login -m \"Merge branch 'feat/login'\"",
            tr("bring the branch in; --no-ff keeps the fork visible", "把分支合并进来；--no-ff 保留分叉的形状"))
        run("git branch -d feat/login", tr("delete the name; the commits stay", "删掉用完的分支名（提交都还在）"))

    def s_conflict():
        run("git switch -c feat/title", tr("a branch that changes the title", "开一个改标题的分支"))
        edit("index.html", tr("<h1>Welcome to my site</h1>", "<h1>欢迎来到小站</h1>"),
             tr("change the heading on the branch", "把标题改成\"欢迎来到小站\""), append=False)
        run("git commit -am \"feat: new site title\"", tr("-a commits every changed tracked file", "-a 表示把改过的已跟踪文件一起存档"))
        run("git switch main", tr("back to main", "回到 main"))
        edit("index.html", tr("<h1>Hi there</h1>", "<h1>你好</h1>"),
             tr("change the same line differently on main", "在 main 上把同一行改成\"你好\""), append=False)
        run("git commit -am \"feat: friendlier title\"", tr("commit", "存档"))
        run("git merge feat/title", tr("both sides changed one line: git can't choose",
                                       "合并……两边改了同一行，git 不知道听谁的"), show=True)
        print()
        aside(tr("open index.html: git wrote both versions into it", "打开 index.html，git 把两个版本都写了进去"))
        with open(os.path.join(repo, "index.html"), encoding="utf-8") as f:
            for line in f.read().splitlines():
                marker = line[:2] in ("<<", "==", ">>")
                print("      " + p(COLORS["behind"] if marker else dim, line))
        print()
        edit("index.html", tr("<h1>Hi there, welcome to my site</h1>", "<h1>你好，欢迎来到小站</h1>"),
             tr("write the version you want and delete the <<< === >>> markers",
                "手动改成最终想要的样子，删掉 <<< === >>> 标记"), append=False)
        run("git add index.html", tr("tell git: this conflict is resolved", "告诉 git：这个冲突解决好了"))
        run("git commit --no-edit", tr("finish the merge with the default message", "完成合并；--no-edit 使用默认的合并说明"))
        run("git branch -d feat/title", tr("delete the branch name", "删掉分支名"))

    def s_remote():
        sh(f"git init -q --bare {remote}", cwd=box)
        aside(tr("a local folder, github.git, plays the part of GitHub", "用一个本地文件夹 github.git 假装是 GitHub 上的仓库"))
        run("git remote add origin ../github.git", tr("register the remote as origin", "登记远程仓库，起名叫 origin"))
        run("git push -u origin main", tr("upload main; -u links it to the remote main", "把 main 上传；-u 让本地 main 记住远程的 main"))

    def s_ahead():
        save("about.html", tr("<p>About us</p>", "<p>关于我们</p>"), "feat: add the about page")
        save("about.html", tr("<p>Contact</p>", "<p>联系方式</p>"), "docs: add contact details")

    def s_push():
        run("git push", tr("upload the new commits", "把新的存档上传到远程"))

    def s_pull():
        sh(f"git clone -q {remote} {mate}", cwd=box)
        with open(os.path.join(mate, "README.md"), "w", encoding="utf-8") as f:
            f.write("# my-site\n")
        tick()
        sh('git add README.md && git commit -q -m "docs: add a README" && git push -q', cwd=mate)
        aside(tr("meanwhile a teammate committed on their machine and pushed",
                 "与此同时，同事在他的电脑上提交了一次，并推送到了远程"))
        run("git fetch", tr("check the remote for news: download, don't merge", "去远程看看有什么新东西：只下载，不合并"))

    def s_pull_done():
        run("git pull", tr("download and merge into this branch (fetch + merge)", "下载并合并到当前分支（相当于 fetch + merge）"))

    def s_parallel():
        run("git switch -c feat/search", tr("first branch", "开第一个分支"))
        save("search.html", "<input>", "feat: add a search box")
        run("git switch main", tr("back to main", "回到 main"))
        run("git switch -c feat/theme", tr("second branch", "再开第二个分支"))
        save("style.css", "body { background: #111; }", "feat: add dark mode")
        save("style.css", "img { filter: invert(1); }", "feat: invert icons in dark mode")

    def s_fold():
        for i in range(1, 8):
            with open(os.path.join(repo, "style.css"), "a", encoding="utf-8") as f:
                f.write(f"/* tweak {i} */\n")
            tick()
            sh(f'git commit -qam "style: tweak colors ({i})"')
        print("  " + p(COLORS["dirty"], tr("✎ tweak the colors 7 times, committing each time",
                                          "✎ 微调了 7 次配色，每次都存档")))
        say('git commit -am "style: tweak colors (n)"', tr("(7 times)", "（重复 7 次）"))

    chapters = [
        (tr("Chapter 1  Commits: saving your work", "第一章  存档：把工作保存下来"), [
            (tr("Create a repository", "新建仓库"), s_init, tr(
                "A repository is a folder that remembers its history. Nothing is saved yet, so the graph is empty.",
                "仓库（repository）就是一个会记录历史的文件夹。现在还没有任何存档，所以图是空的。")),
            (tr("Your first commit", "第一次存档"), s_first, tr(
                "Saving takes two steps: add puts files in the staging area, commit saves what's staged as a commit.\n"
                "Each ● is a commit; ◉ is where you are now (HEAD).\n"
                "Conventional Commits: \"type: what it does\", short, starting with a verb.\n"
                "Common types: feat new feature · fix bug fix · docs documentation · style formatting · refactor · chore.",
                "存档分两步：add 把文件放进\"暂存区\"，commit 把暂存区里的东西存成一个存档（提交）。\n"
                "图上的 ● 就是一次提交，◉ 是你现在所在的位置（HEAD）。\n"
                "提交说明的规范写法（Conventional Commits）是\"类型: 做了什么\"，用英文、短句、动词开头。\n"
                "常用类型：feat 新功能 · fix 修问题 · docs 文档 · style 样式/格式 · refactor 重构 · chore 杂务。")),
            (tr("Commit again, then read the history", "再存一次，然后看历史"), s_log, tr(
                "git log lists every commit. The short code in front is its id; you can always get back to it.\n"
                "Good habit: one commit, one change. When something breaks, you'll know which step did it.",
                "git log 列出所有存档。前面那串字母数字是提交编号，以后可以用它找回任何一个存档。\n"
                "好习惯：一次提交只做一件事。这样出了问题，很容易找到是哪一步引起的。")),
        ]),
        (tr("Chapter 2  Branches: new work without touching main", "第二章  分支：在不影响主线的地方做新功能"), [
            (tr("Create a branch", "开一个新分支"), s_branch, tr(
                "Creating a branch is not a commit: no new ● appears. A branch is just a new name for this commit,\n"
                "and your next commits will be recorded under it. Name branches \"type/short-description\", e.g. feat/login.",
                "开分支不是提交，不会多出新的 ●。分支只是给当前这个存档点起了一个新名字，\n"
                "并且让你之后的提交都记在这个名字下面。分支名的常见写法是\"类型/简短描述\"，比如 feat/login、fix/typo。")),
            (tr("Commit on the branch", "在分支上提交"), s_branch_commits, tr(
                "The branch grows its own row; ╰ is where it left main. +2 means it has 2 commits main doesn't.\n"
                "These commits live only on the branch; main is untouched.",
                "分支长出了自己的一行，╰ 是它从 main 分出去的地方。+2 表示它比 main 多 2 次提交。\n"
                "这些提交只在分支上，main 完全不受影响。")),
            (tr("Meanwhile, main moves on", "与此同时，main 也往前走了"), s_main_moves, tr(
                "−1 means main has 1 commit your branch doesn't. + and − together: both lines moved forward.\n"
                "git switch moves between branches, and the files in the folder change to match.",
                "−1 表示 main 有 1 次提交是你的分支还没有的。+ 和 − 同时出现，说明两条线各自往前走了。\n"
                "git switch 用来在分支之间切换，切换时文件夹里的文件会变成那个分支的样子。")),
            (tr("A change you haven't committed", "改了文件，但还没存档"), s_dirty, tr(
                "✎1 means 1 file is changed but not committed. git status says which files, git diff says what changed.\n"
                "Good habit: glance at the diff before committing, so nothing unintended sneaks in.",
                "✎1 表示有 1 个文件改了还没存档。git status 看哪些文件变了，git diff 看具体变了什么。\n"
                "好习惯：存档之前先看一眼 diff，确认没有带进不该提交的东西。")),
            (tr("Commit it", "把它存档"), s_commit_it, tr(
                "✎ is gone and +2 became +3.", "✎ 消失了，+2 变成了 +3。")),
        ]),
        (tr("Chapter 3  Merging: bring the work back to main", "第三章  合并：把分支的成果并回主线"), [
            (tr("Merge the branch", "合并分支"), s_merge, tr(
                "Merging made a new commit, a merge commit, normally drawn as ◆; you're standing on it, so it shows as ◉.\n"
                "╯ is where the branch rejoins main. Merged branches are dimmed and their names deleted; the commits stay.\n"
                "Why --no-ff: when main hasn't moved, git would otherwise just slide main forward and the fork would vanish.",
                "合并产生了一个新的提交（合并提交），平时画成 ◆；你现在正站在它上面，所以显示为 ◉。\n"
                "╯ 是分支回到主线的地方。合并完的分支变暗，名字也删掉了，但它的提交都还在。\n"
                "为什么用 --no-ff：否则在 main 没有新提交时，git 会直接把 main 挪到分支末尾，分叉的形状就看不出来了。")),
            (tr("A merge conflict: both sides changed one line", "合并冲突：两边改了同一行"), s_conflict, tr(
                "A conflict isn't an error. Git is asking: \"this line was changed two ways, which do you want?\"\n"
                "The fix is always: open the file → write what you want, delete <<< === >>> → git add → git commit.",
                "冲突不是出错，只是 git 在问你：\"同一行被改成了两个样子，要留哪个？\"\n"
                "解决的步骤永远是：打开文件 → 改成想要的样子并删掉 <<< === >>> 标记 → git add → git commit。")),
        ]),
        (tr("Chapter 4  Remotes: syncing with GitHub", "第四章  远程仓库：和 GitHub 同步"), [
            (tr("Connect a remote and push", "连上远程仓库，第一次上传"), s_remote, tr(
                "A remote is a copy somewhere else, like GitHub, for backup and teamwork. origin is its conventional name.",
                "远程仓库就是放在别处（比如 GitHub）的一份副本，用来备份和协作。origin 是它的名字，也是最常见的叫法。")),
            (tr("Two more local commits", "本地又存了两次档"), s_ahead, tr(
                "↑2: 2 commits exist only on your machine. If the disk dies, they're gone.",
                "↑2 表示本地有 2 次提交还没上传。它们只在你的电脑上，电脑坏了就没了。")),
            (tr("Push", "上传"), s_push, tr(
                "↑ is gone: local and remote match. Good habit: push when you finish a piece of work; the remote is your backup.",
                "↑ 消失了：本地和远程一样了。好习惯：做完一段工作就 push，远程就是你的备份。")),
            (tr("A teammate pushed too", "同事也上传了东西"), s_pull, tr(
                "↓1: the remote has 1 commit you don't. fetch only looks; it doesn't change your files.",
                "↓1 表示远程有 1 次提交你还没拿到。fetch 只是\"看一眼\"，不会改动你的文件。")),
            (tr("Pull", "拉下来"), s_pull_done, tr(
                "↓ is gone. Good habit: pull before you start and before you push; it saves many conflicts.",
                "↓ 消失了。好习惯：开始工作前先 pull，push 之前也先 pull，能少遇到很多冲突。")),
        ]),
        (tr("Chapter 5  Going further: several things at once", "第五章  进阶：同时做几件事"), [
            (tr("Two branches at once", "同时开两个分支"), s_parallel, tr(
                "Each unmerged branch gets its own row. ├ means two branches start from the same commit.\n"
                "The bold branch name is the one you're on.",
                "每个还没合并的分支各占一行。├ 表示两个分支从同一个存档点分出去。加粗的分支名就是你现在所在的分支。")),
            (tr("Long branches fold", "很长的分支会被折叠"), s_fold, tr(
                "The middle commits fold into (n) to keep the graph short. Starts, ends, forks and merges never fold.",
                "中间的提交收成了 (n)，让图保持短小。开头、结尾、分叉点和合并点永远不会被折叠。")),
        ]),
    ]

    habits = [
        (tr("Messages", "提交说明"), tr("type: what it does, e.g. feat: add the login form; one change per commit",
                                        "类型: 做了什么，比如 feat: add the login form；一次提交只做一件事")),
        (tr("Branch names", "分支名"), tr("type/short-description, e.g. feat/login, fix/typo; delete after merging",
                                          "类型/简短描述，比如 feat/login、fix/typo；做完合并后删掉")),
        (tr("Before commit", "存档之前"), tr("git status for which files, git diff for what; no passwords or private files",
                                              "git status 看改了哪些文件，git diff 看改了什么，别带进密码和私人文件")),
        (tr("How often", "存档频率"), tr("commit every small step; many small commits beat one huge one",
                                        "完成一小步就提交；宁可多存几次，也别攒一大堆")),
        (tr("Syncing", "和远程同步"), tr("git pull before you start, git push when you finish a piece",
                                        "开工前 git pull，做完一段就 git push")),
        (tr("Don't", "不要做"), tr("rewrite history you've pushed (rebase / reset --hard / push --force)",
                                   "已经 push 的历史不要改写（rebase / reset --hard / push --force）")),
        (".gitignore", tr("list files that must stay out (caches, secrets, personal data)",
                          "把不该进仓库的文件（缓存、密钥、个人数据）写进去，git 就会忽略它们")),
    ]
    commands = [
        ("git status", tr("what changed", "现在有哪些改动")),
        ("git diff", tr("the exact changes", "具体改了什么")),
        (tr("git add FILE", "git add 文件"), tr("stage a file", "放进暂存区")),
        (tr('git commit -m "msg"', 'git commit -m "说明"'), tr("commit", "存档")),
        ("git log --oneline", tr("history", "看历史")),
        (tr("git switch -c NAME", "git switch -c 名字"), tr("new branch", "开新分支")),
        (tr("git switch NAME", "git switch 名字"), tr("change branch", "切换分支")),
        (tr("git merge NAME", "git merge 名字"), tr("merge a branch in", "把分支合并进来")),
        (tr("git branch -d NAME", "git branch -d 名字"), tr("delete a merged branch", "删掉用完的分支")),
        ("git push / git pull", tr("upload / download", "上传 / 下载")),
    ]

    total = sum(len(steps) for _, steps in chapters)
    n = 0
    try:
        print(p("1", tr("gitgraph learn", "gitgraph 学习模式")) +
              p(dim, tr("  (a practice repository in a temp folder, deleted at the end)",
                        "  （练习仓库放在临时目录里，结束后自动删除）")))
        for title, steps in chapters:
            print()
            print(p("1;" + COLORS["trunk"], title))
            for name, action, note in steps:
                n += 1
                print()
                print(p("1", tr(f"Step {n}/{total}  {name}", f"第 {n}/{total} 步  {name}")))
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
                    input(p(dim, tr("\n  Press Enter to continue ⏎ ", "\n  按回车继续 ⏎ ")))
        print()
        print(p("1;" + COLORS["trunk"], tr("Good habits", "好习惯速查")))
        for k, v in habits:
            print("  " + p("1", pad(k, 16)) + v)
        print()
        print(p("1;" + COLORS["trunk"], tr("Everyday commands", "常用命令")))
        for k, v in commands:
            print("  " + pad(k, 26) + p(dim, v))
        print()
        print("  " + tr("Done. Run ", "学完了。随时运行 ") + p("1", "python3 gitgraph.py legend") +
              tr(" any time to look up a symbol.", " 查看图上的符号。"))
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        shutil.rmtree(box, ignore_errors=True)


# --------------------------------------------------------------------- demo

def demo(pause=True, color=True):
    """A simulated team: you, a lead agent and three subagents building one project."""
    p = Paint(color)
    dim = COLORS["dim"]
    box = tempfile.mkdtemp(prefix="gitgraph-demo-")
    repo = os.path.join(box, "shop")              # your folder, on main
    remote = os.path.join(box, "github.git")      # stands in for GitHub
    mate = os.path.join(box, "teammate")          # a teammate's clone
    os.makedirs(repo)
    clock = [int(time.time()) - 6 * 3600]
    env = dict(os.environ,
               GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.com",
               GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.com",
               GIT_CONFIG_COUNT="2",
               GIT_CONFIG_KEY_0="commit.gpgsign", GIT_CONFIG_VALUE_0="false",
               GIT_CONFIG_KEY_1="advice.defaultBranchName", GIT_CONFIG_VALUE_1="false",
               LANG="C", LC_ALL="C")
    agents = {"cart": "feat/cart", "checkout": "feat/checkout", "docs": "docs/guide"}

    def sh(command, cwd=repo):
        clock[0] += 4 * 60
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"{clock[0]} +0000"
        subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True)

    def run(who, command, note="", cwd=repo):
        sh(command, cwd)
        line = "  " + p("1", pad(who, 16)) + p(dim, "$ ") + command
        if note:
            line += " " * max(2, 44 - width_of(command)) + p(dim, "# " + note)
        print(line)

    def work(agent, path, message):
        """A subagent edits a file in its own worktree and commits."""
        folder = os.path.join(box, "agent-" + agent)
        with open(os.path.join(folder, path), "a", encoding="utf-8") as f:
            f.write(message + "\n")
        run(tr(f"{agent} agent", f"{agent} 子代理"), f'git commit -am "{message}"', cwd=folder)

    def setup():
        for path, message in (("index.html", "feat: add the home page"),
                              ("products.html", "feat: list the products"),
                              ("style.css", "feat: add basic styles")):
            with open(os.path.join(repo, path), "w", encoding="utf-8") as f:
                f.write(message + "\n")
            sh(f'git add {path} && git commit -q -m "{message}"')
        for agent in agents:
            with open(os.path.join(repo, {"cart": "cart.js", "checkout": "checkout.js",
                                          "docs": "GUIDE.md"}[agent]), "w", encoding="utf-8") as f:
                f.write("\n")
        sh('git add -A && git commit -q -m "chore: add empty files for the next features"')
        sh(f"git init -q --bare {remote}", cwd=box)
        sh(f"git remote add origin {remote} && git push -q -u origin main")
        print("  " + p(dim, tr("(a small shop site: 4 commits on main, already pushed to GitHub)",
                               "（一个小网店项目：main 上已有 4 次提交，并且已经推送到 GitHub）")))

    def s_split():
        print("  " + p("1", pad(tr("you", "你"), 16)) +
              tr("\"Add a cart, a checkout page and a user guide. Use subagents.\"",
                 "「加购物车、结算页和使用说明，用 subagent 并行做」"))
        for agent, branch in agents.items():
            run(tr("lead agent", "主代理"), f"git worktree add -b {branch} ../agent-{agent}",
                tr("own folder + own branch", "独立文件夹 + 独立分支"))

    def s_first():
        work("cart", "cart.js", "feat: add items to the cart")
        work("docs", "GUIDE.md", "docs: how to place an order")
        work("checkout", "checkout.js", "feat: add the checkout form")

    def s_parallel():
        work("cart", "cart.js", "feat: change item quantity")
        work("checkout", "checkout.js", "feat: validate the address")
        work("cart", "cart.js", "fix: keep the cart after reload")
        work("checkout", "checkout.js", "feat: add card payment")

    def s_teammate():
        sh(f"git clone -q {remote} {mate}", cwd=box)
        with open(os.path.join(mate, "products.html"), "a", encoding="utf-8") as f:
            f.write("fix\n")
        sh('git commit -qam "fix: wrong price on the product page" && git push -q', cwd=mate)
        print("  " + p("1", pad(tr("teammate", "同事"), 16)) +
              p(dim, tr("fixed a price bug on their laptop and pushed it", "在自己电脑上修了一个价格 bug 并推送")))
        run(tr("you", "你"), "git pull", tr("bring main up to date", "把 main 更新到最新"))

    def s_merge_docs():
        run(tr("lead agent", "主代理"), "git diff main...docs/guide --stat", tr("review first", "合并前先看改了什么"))
        run(tr("lead agent", "主代理"), "git merge --no-ff docs/guide -m \"Merge branch 'docs/guide'\"")
        run(tr("lead agent", "主代理"), "git worktree remove ../agent-docs && git branch -d docs/guide",
            tr("clean up", "收尾：删掉文件夹和分支名"))

    def s_merge_cart():
        run(tr("lead agent", "主代理"), "git merge --no-ff feat/cart -m \"Merge branch 'feat/cart'\"")
        run(tr("lead agent", "主代理"), "git worktree remove ../agent-cart && git branch -d feat/cart")
        folder = os.path.join(box, "agent-checkout")
        with open(os.path.join(folder, "checkout.js"), "a", encoding="utf-8") as f:
            f.write("// retry on network errors\n")
        print("  " + p("1", pad(tr("checkout agent", "checkout 子代理"), 16)) +
              p(COLORS["dirty"], tr("✎ still editing checkout.js, not committed yet",
                                    "✎ 还在改 checkout.js，没有提交")))

    def s_finish():
        work("checkout", "checkout.js", "feat: retry on network errors")
        run(tr("lead agent", "主代理"), "git merge --no-ff feat/checkout -m \"Merge branch 'feat/checkout'\"")
        run(tr("lead agent", "主代理"), "git worktree remove ../agent-checkout && git branch -d feat/checkout")

    def s_push():
        run(tr("you", "你"), "git push", tr("back up the finished work", "把做完的工作备份到 GitHub"))

    steps = [
        (tr("The starting point", "起点"), setup, repo, tr(
            "One trunk, nothing open. ◉ is where you are.",
            "只有一条主线，没有进行中的分支。◉ 是你现在的位置。")),
        (tr("The lead agent splits the work", "主代理把任务拆给三个子代理"), s_split, repo, tr(
            "A worktree is a second folder attached to the same repository, checked out on its own branch.\n"
            "Each subagent works in its own folder, so they never overwrite each other's files.\n"
            "No new rows yet: a new branch is only a name, sitting on main's last commit until someone commits on it.",
            "worktree 是挂在同一个仓库上的另一个文件夹，它检出自己的分支。\n"
            "每个子代理在自己的文件夹里干活，所以不会互相覆盖文件。\n"
            "还没有新的行：分支在有人提交之前只是一个名字，挂在 main 最后一个提交上。")),
        (tr("Each subagent commits", "三个子代理各自提交"), s_first, repo, tr(
            "Three rows appear, one per subagent. ├ means they all started from the same commit.",
            "出现了三行，每个子代理一行。├ 表示它们都从同一个提交分出去。")),
        (tr("They keep going, side by side", "它们继续并行工作"), s_parallel, repo, tr(
            "+n is how many commits each branch has that main doesn't: how far each job has got.",
            "+n 表示这个分支比 main 多几次提交，也就是每件活干到了哪里。")),
        (tr("A teammate pushes a fix", "同事推送了一个修复"), s_teammate, repo, tr(
            "main moved on, so every open branch now shows −1: main has a commit they don't.",
            "main 往前走了，所以每个还开着的分支都显示 −1：main 有一次提交是它们没有的。")),
        (tr("The guide is done: review and merge", "说明文档做完了：检查后合并"), s_merge_docs, repo, tr(
            "◆ is the merge. The merged branch dims and curves back into main with ╯.\n"
            "↑2: the merge and the guide's commit are only on your machine so far, not on GitHub.",
            "◆ 是合并提交。合并完的分支变暗，用 ╯ 弯回主线。\n"
            "↑2：这次合并和文档的提交目前只在你的电脑上，GitHub 上还没有。")),
        (tr("The cart is merged; checkout is still busy", "购物车合并了；结算页还在忙"), s_merge_cart, os.path.join(box, "agent-checkout"), tr(
            "This graph is drawn from the checkout agent's folder, so ◉ sits on its branch.\n"
            "✎1: one file changed and not committed. If the agent stopped now, that work would be easy to lose.",
            "这张图是从 checkout 子代理的文件夹里画的，所以 ◉ 在它的分支上。\n"
            "✎1：有一个文件改了还没提交。子代理这时候中断的话，这部分最容易丢。")),
        (tr("Checkout is done too", "结算页也做完了"), s_finish, repo, tr(
            "Three merges, one per subagent. No open rows left: nothing half-finished.",
            "三次合并，每个子代理一次。没有还开着的行，说明没有半截的工作。")),
        (tr("Push", "推送"), s_push, repo, tr(
            "↑ is gone: GitHub has everything. That's a clean finish.",
            "↑ 消失了：GitHub 上什么都有了。这就是一个干净的收尾。")),
    ]

    try:
        print(p("1", tr("gitgraph demo", "gitgraph 演示")) +
              p(dim, tr("  (a simulated team with subagents, in a temp folder deleted at the end)",
                        "  （模拟一个有子代理的团队项目，放在临时目录里，结束后自动删除）")))
        sh("git init -q -b main")
        for n, (name, action, where, note) in enumerate(steps, 1):
            print()
            print(p("1", tr(f"Step {n}/{len(steps)}  {name}", f"第 {n}/{len(steps)} 步  {name}")))
            action()
            graph = render(where, color)
            if graph:
                print()
                for line in graph.splitlines():
                    print("    " + line)
            print()
            for line in note.splitlines():
                print("  " + line)
            if pause and sys.stdin.isatty():
                input(p(dim, tr("\n  Press Enter to continue ⏎ ", "\n  按回车继续 ⏎ ")))
        print()
        print("  " + tr("In your own project, the status line shows the same picture while agents work.",
                        "在你自己的项目里，agent 干活时状态栏会实时显示同样的图。"))
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        shutil.rmtree(box, ignore_errors=True)


# ------------------------------------------------------------------- themes

def themes(path, color=True):
    """The graph of the repository at PATH, drawn once in every theme."""
    out = []
    for name in THEMES:
        COLORS.update(palette(name))
        graph = render(path, color)
        if not graph:
            return tr("not inside a git repository; run it in one to preview the themes",
                      "这里不是 git 仓库；在一个仓库里运行才能预览主题")
        title = f"--theme={name}" + ("" if LANG != "zh" else f"  {THEME_NAMES[name]}")
        out += [Paint(color)("1", title), *("  " + line for line in graph.splitlines()), ""]
    out.append(tr("Switch with: gitgraph theme NAME", "切换主题：gitgraph theme 名字"))
    return "\n".join(out)


def set_theme(name):
    """Save the theme used whenever --theme and GITGRAPH_THEME are not given."""
    choices = "  ".join(f"{n} ({THEME_NAMES[n]})" if LANG == "zh" else n for n in THEMES)
    if not name:
        return (tr("current theme: ", "当前主题：") + (saved_theme() or "default") + "\n" +
                tr("choose one: gitgraph theme NAME\n  ", "切换：gitgraph theme 名字\n  ") + choices)
    if name not in THEMES:
        return tr("no such theme; choose one of:\n  ", "没有这个主题，可选：\n  ") + choices
    path = theme_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(name + "\n")
    return (tr(f"theme set to {name}; the status line picks it up on its next refresh",
               f"已切换到 {name}（{THEME_NAMES[name]}），状态栏下次刷新就会生效"))


# ---------------------------------------------------------------------- cli

def main(argv):
    global LANG
    LANG = pick_lang(argv)
    color = use_color(argv)
    if not pick_theme(argv) and "--statusline" not in argv and args[:1] != ["theme"]:
        print(tr("unknown theme; choose one of: ", "没有这个主题，可选：") + ", ".join(THEMES), file=sys.stderr)
        return
    args = [a for a in argv if not a.startswith("--")]
    if "-h" in argv or "--help" in argv:
        print(__doc__.strip())
    elif args[:1] == ["legend"]:
        print(legend(color))
    elif args[:1] == ["log"]:
        out = render(args[1] if len(args) > 1 else ".", color, details=True)
        print(out or tr("not inside a git repository", "这里不是 git 仓库"))
    elif args[:1] == ["learn"]:
        learn(pause="--no-pause" not in argv, color=color)
    elif args[:1] == ["theme"]:
        print(set_theme(args[1] if len(args) > 1 else ""))
    elif args[:1] == ["themes"]:
        print(themes(args[1] if len(args) > 1 else ".", color))
    elif args[:1] == ["demo"]:
        demo(pause="--no-pause" not in argv, color=color)
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
