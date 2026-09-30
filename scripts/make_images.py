#!/usr/bin/env python3
"""Regenerate the README images in docs/ from real gitgraph output.

    python3 scripts/make_images.py

Builds throwaway demo repositories in a temp folder, renders them with
gitgraph, and turns the colored terminal text into SVG. Lines, corners and
commit dots are drawn as vector shapes so they join cleanly at any size.
"""
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gitgraph  # noqa: E402

DOCS = os.path.join(ROOT, "docs")
BG, FG = "#1c1d22", "#c9ccd3"
CW, LH = 9.4, 26                      # cell width and line height in px
FONT = "SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace"
BOX = {"─": "LR", "│": "UD", "╰": "UR", "╭": "DR", "╯": "UL", "╮": "DL",
       "├": "UDR", "┤": "UDL", "┴": "ULR", "┬": "DLR", "┼": "UDLR"}
NODES = "●◆◉"


# ------------------------------------------------------------ text to cells

def c256(n):
    if n >= 232:
        v = 8 + (n - 232) * 10
        return f"#{v:02x}{v:02x}{v:02x}"
    n -= 16
    lv = [0, 95, 135, 175, 215, 255]
    return "#%02x%02x%02x" % (lv[n // 36], lv[n // 6 % 6], lv[n % 6])


def blend(color, alpha=0.42):
    f = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(BG[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x * alpha + y * (1 - alpha)) for x, y in zip(f, b))


def cells(ansi):
    """ANSI text -> rows of (char, color, bold). Wide characters take two cells."""
    rows = []
    for line in ansi.split("\n"):
        row, fg, bold, dim = [], FG, False, False
        for tok in re.split(r"(\x1b\[[0-9;]*m)", line):
            m = re.fullmatch(r"\x1b\[([0-9;]*)m", tok)
            if m:
                codes = [int(x) for x in m.group(1).split(";") if x] or [0]
                i = 0
                while i < len(codes):
                    k = codes[i]
                    if k == 0:
                        fg, bold, dim = FG, False, False
                    elif k == 1:
                        bold = True
                    elif k == 2:
                        dim = True
                    elif k == 38:
                        fg = c256(codes[i + 2])
                        i += 2
                    i += 1
                continue
            for ch in tok:
                row.append((ch, blend(fg) if dim else fg, bold))
                if gitgraph.width_of(ch) == 2:
                    row.append(("", None, False))     # the second half of a wide char
        rows.append(row)
    return rows


def svg_rows(rows, x0, y0):
    """SVG elements for rows of cells, top-left at (x0, y0)."""
    lines, shapes, texts = [], [], []
    for y, row in enumerate(rows):
        cy = y0 + y * LH + LH / 2
        for x, (ch, col, bold) in enumerate(row):
            if not ch or ch == " ":
                continue
            cx = x0 + x * CW + CW / 2
            above = rows[y - 1][x][0] if y > 0 and x < len(rows[y - 1]) else " "
            below = rows[y + 1][x][0] if y + 1 < len(rows) and x < len(rows[y + 1]) else " "
            if ch in BOX:
                d = ""
                for k in BOX[ch]:
                    ex, ey = {"L": (cx - CW / 2, cy), "R": (cx + CW / 2, cy),
                              "U": (cx, cy - (LH if above in NODES else LH / 2)),
                              "D": (cx, cy + (LH if below in NODES else LH / 2))}[k]
                    d += f"M{cx:.1f} {cy:.1f}L{ex:.1f} {ey:.1f}"
                lines.append(f'<path d="{d}" stroke="{col}"/>')
            elif ch == "┄":
                lines.append(f'<path d="M{cx - CW / 2:.1f} {cy:.1f}L{cx + CW / 2:.1f} {cy:.1f}" '
                             f'stroke="{col}" stroke-dasharray="2 3"/>')
            elif ch == "●":
                shapes.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.6" fill="{col}"/>')
            elif ch == "◆":
                shapes.append(f'<path d="M{cx:.1f} {cy - 6:.1f}L{cx + 6:.1f} {cy:.1f}L{cx:.1f} {cy + 6:.1f}'
                              f'L{cx - 6:.1f} {cy:.1f}Z" fill="{col}"/>')
            elif ch == "◉":
                shapes.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{BG}" stroke="{col}" stroke-width="2"/>'
                              f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.6" fill="{col}"/>')
            else:
                if gitgraph.width_of(ch) == 2:
                    cx += CW / 2
                weight = ' font-weight="700"' if bold else ""
                texts.append(f'<text x="{cx:.1f}" y="{cy + 5:.1f}" fill="{col}"{weight}>{html.escape(ch)}</text>')
    return (['<g fill="none" stroke-width="2" stroke-linecap="round">'] + lines + ["</g>"] + shapes +
            [f'<g font-family="{FONT}" font-size="15.5" text-anchor="middle">'] + texts + ["</g>"])


def window(frames, title, path, seconds=2.6):
    """A terminal window around one frame, or an animation looping through several."""
    cols = max(len(r) for f in frames for r in f)
    nrows = max(len(f) for f in frames)
    pad_x, top, bottom = 26, 50, 18
    w, h = int(pad_x * 2 + cols * CW), int(top + nrows * LH + bottom)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
           f'<rect width="{w}" height="{h}" rx="12" fill="{BG}"/>',
           '<circle cx="22" cy="20" r="6" fill="#ff5f57"/><circle cx="42" cy="20" r="6" fill="#febc2e"/>'
           '<circle cx="62" cy="20" r="6" fill="#28c840"/>',
           f'<text x="{w / 2:.0f}" y="25" fill="#7d8190" font-family="{FONT}" font-size="13" '
           f'text-anchor="middle">{html.escape(title)}</text>']
    n = len(frames)
    if n > 1:
        total = n * seconds
        share = 100 / n
        out.append(f"<style>.f{{opacity:0;animation:show {total:.1f}s infinite}}"
                   f"@keyframes show{{0%{{opacity:1}}{share - 0.01:.2f}%{{opacity:1}}"
                   f"{share:.2f}%{{opacity:0}}100%{{opacity:0}}}}</style>")
    for i, f in enumerate(frames):
        if n > 1:
            out.append(f'<g class="f" style="animation-delay:{i * seconds - n * seconds:.1f}s">')
        out += svg_rows(f, pad_x, top)
        if n > 1:
            out.append("</g>")
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("wrote", os.path.relpath(path, ROOT))


# ---------------------------------------------------------- demo repository

class Demo:
    """A scripted repository with a fake clock, plus a bare 'GitHub' remote."""

    def __init__(self, base):
        self.dir = os.path.join(base, "my-site")
        self.remote = os.path.join(base, "github.git")
        os.makedirs(self.dir)
        self.clock = int(time.time()) - 3 * 3600
        self.env = dict(os.environ, GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="demo@example.com",
                        GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="demo@example.com",
                        GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="commit.gpgsign",
                        GIT_CONFIG_VALUE_0="false", LANG="C", LC_ALL="C")

    def sh(self, cmd, step=6 * 60):
        self.clock += step
        self.env["GIT_AUTHOR_DATE"] = self.env["GIT_COMMITTER_DATE"] = f"{self.clock} +0000"
        r = subprocess.run(cmd, shell=True, cwd=self.dir, env=self.env, capture_output=True, text=True)
        return (r.stdout + r.stderr).strip()

    def write(self, path, text):
        with open(os.path.join(self.dir, path), "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def commit(self, path, msg):
        self.write(path, msg)
        return self.sh(f'git add -A && git commit -q -m "{msg}"')

    def graph(self, details=False):
        return gitgraph.render(self.dir, True, details)


def build_history(d):
    """Some history so the graph has shape before the animation starts."""
    d.sh("git init -q -b main")
    d.commit("index.html", "feat: add the home page")
    d.commit("style.css", "feat: add basic styles")
    d.sh("git switch -q -c feat/nav")
    for m in ("feat: add a nav bar", "feat: highlight the current page", "fix: nav on small screens"):
        d.commit("nav.html", m)
    d.sh("git switch -q main && git merge -q --no-ff feat/nav -m \"Merge branch 'feat/nav': a nav bar on every page\""
         " && git branch -q -d feat/nav")
    d.sh("git switch -q -c feat/blog")
    for i in range(1, 8):
        d.commit("blog.html", f"feat: blog post layout ({i})")
    d.sh("git switch -q main")
    d.commit("index.html", "docs: update the footer")
    d.sh("git merge -q --no-ff feat/blog -m \"Merge branch 'feat/blog': a blog with post pages\""
         " && git branch -q -d feat/blog")
    d.sh(f"git init -q --bare {d.remote} && git remote add origin {d.remote} && git push -q -u origin main")


# ------------------------------------------------------------------ frames

DIM, PINK, GREEN, ORANGE = "\033[2m", "\033[38;5;176m", "\033[38;5;114m", "\033[38;5;209m"
R = "\033[0m"


def claude_frame(d, prompt, reply, box_cols=70):
    """A Claude Code screen: the last exchange, the input box, the status line."""
    if prompt.startswith("! "):
        head = f"{DIM}>{R} {PINK}!{R} {prompt[2:]}"
    else:
        head = f"{DIM}>{R} {prompt}"
    lines = ["", head]
    for i, text in enumerate(reply):
        if text.startswith("● "):
            lines.append(f"{GREEN}●{R} {text[2:]}")
        else:
            lines.append(f"  {DIM}╰ {text}{R}" if i == len(reply) - 1 or not reply[i + 1].startswith("  ")
                         else f"  {DIM}  {text}{R}")
    while len(lines) < 4:
        lines.append("")
    lines += ["",
              f"{DIM}╭{'─' * (box_cols - 2)}╮{R}",
              f"{DIM}│{R} {ORANGE}>{R}{' ' * (box_cols - 4)}{DIM}│{R}",
              f"{DIM}╰{'─' * (box_cols - 2)}╯{R}",
              f"  {DIM}~/my-site  ·  Opus 5.5  ·  ctx 72%{R}"]
    lines += ["  " + g for g in d.graph().split("\n")]
    return cells("\n".join(lines))


def animation(base):
    d = Demo(base)
    build_history(d)
    frames = []

    def step(prompt, reply, action):
        out = action()
        frames.append(claude_frame(d, prompt, reply(out) if callable(reply) else reply))

    step("! git switch -c feat/login", ["Switched to a new branch 'feat/login'"],
         lambda: d.sh("git switch -q -c feat/login"))
    step('! git commit -am "feat: add the login form"',
         lambda out: [f"[feat/login {head(d)}] feat: add the login form", " 1 file changed, 1 insertion(+)"],
         lambda: d.commit("login.html", "feat: add the login form"))
    step("Show a friendly error when the password is wrong",
         ["● Update(login.html)", "Added 3 lines"],
         lambda: d.write("login.html", "<p class='error'>That password didn't work.</p>"))
    step('! git commit -am "feat: show a password error"',
         lambda out: [f"[feat/login {head(d)}] feat: show a password error", " 1 file changed, 1 insertion(+)"],
         lambda: d.sh('git commit -q -am "feat: show a password error"'))
    step("! git switch main && git merge --no-ff feat/login",
         ["Merge made by the 'ort' strategy.", " login.html | 3 +++"],
         lambda: d.sh("git switch -q main && git merge -q --no-ff feat/login -m \"Merge branch 'feat/login': "
                      "log in with a friendly error\" && git branch -q -d feat/login"))
    step("! git push", ["To github.com:you/my-site.git", f"   {head(d)}..{head(d)}  main -> main"],
         lambda: d.sh("git push -q"))
    frames[-1] = claude_frame(d, "! git push", ["To github.com:you/my-site.git", "   main -> main"])
    step("! git switch -c feat/search", ["Switched to a new branch 'feat/search'"],
         lambda: d.sh("git switch -q -c feat/search"))
    step('! git commit -am "feat: add a search box"',
         lambda out: [f"[feat/search {head(d)}] feat: add a search box", " 1 file changed, 1 insertion(+)"],
         lambda: d.commit("search.html", "feat: add a search box"))
    frames.append(frames[-1])                    # linger on the last frame
    return d, frames


def themes_image(base):
    """One repository drawn in every theme, stacked in one window."""
    d = Demo(os.path.join(base, "themes"))
    build_history(d)
    for branch, n in (("feat/login", 3), ("feat/search", 2), ("fix/footer", 1)):
        d.sh(f"git switch -q main && git switch -q -c {branch}")
        for i in range(n):
            d.commit(branch.split("/")[1] + ".html", f"feat: {branch} ({i})")
    d.sh("git switch -q main")
    d.commit("index.html", "fix: footer link")
    d.sh("git switch -q feat/login")
    d.write("login.html", "<p>work in progress</p>")
    lines = []
    for name in gitgraph.THEMES:
        gitgraph.COLORS.update(gitgraph.THEMES[name])
        lines += [f"{DIM}--theme={name}{R}"] + ["  " + g for g in d.graph().split("\n")] + [""]
    gitgraph.COLORS.update(gitgraph.THEMES["default"])
    return cells("\n".join(lines[:-1]))


def head(d):
    return d.sh("git rev-parse --short HEAD", step=0)


# -------------------------------------------------------------------- main

def main():
    base = tempfile.mkdtemp(prefix="gitgraph-images-")
    try:
        d, frames = animation(base)
        window(frames, "claude — my-site", os.path.join(DOCS, "demo.svg"))
        window([themes_image(base)], "gitgraph themes", os.path.join(DOCS, "themes.svg"))

        for lang, suffix in (("en", ""), ("zh", ".zh-CN")):
            gitgraph.LANG = lang
            log = cells(d.graph(details=True))
            window([log], "gitgraph log", os.path.join(DOCS, f"log{suffix}.svg"))

            out = subprocess.run([sys.executable, os.path.join(ROOT, "gitgraph.py"), "learn",
                                  "--no-pause", "--color", f"--{lang}"],
                                 capture_output=True, text=True).stdout
            plain = re.sub(r"\x1b\[[0-9;]*m", "", out).split("\n")
            ansi = out.split("\n")
            start = next(i for i, l in enumerate(plain) if l.startswith(("Step 7/", "第 7/")))
            end = next(i for i, l in enumerate(plain) if l.startswith(("Step 8/", "第 8/")))
            window([cells("\n".join(ansi[start:end - 1]))], "gitgraph learn",
                   os.path.join(DOCS, f"learn{suffix}.svg"))

            out = subprocess.run([sys.executable, os.path.join(ROOT, "gitgraph.py"), "demo",
                                  "--no-pause", "--color", f"--{lang}"],
                                 capture_output=True, text=True).stdout
            plain = re.sub(r"\x1b\[[0-9;]*m", "", out).split("\n")
            ansi = out.split("\n")
            starts = [i for i, l in enumerate(plain) if l.startswith(("Step ", "第 "))]
            frames = [cells("\n".join(ansi[a:b - 1]))
                      for a, b in zip(starts, starts[1:] + [len(ansi) - 2])]
            window(frames[2:] + [frames[-1]], "gitgraph demo",
                   os.path.join(DOCS, f"team{suffix}.svg"), seconds=3.2)
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
