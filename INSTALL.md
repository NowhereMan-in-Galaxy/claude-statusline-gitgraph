# Install guide for agents

You are a coding agent (Claude Code or similar) and the user asked you to install claude-statusline-gitgraph. Follow these steps in order. Talk to the user in the language they used with you.

## 1. Check the requirements

Run `git --version` and `python3 --version`. Python 3.8 or newer is enough. Nothing else is needed: gitgraph uses only the standard library, so do not create a virtual environment or install packages.

## 2. Get the code

```sh
git clone https://github.com/NowhereMan-in-Galaxy/claude-statusline-gitgraph.git ~/tools/claude-statusline-gitgraph
```

If that folder already exists, run `git -C ~/tools/claude-statusline-gitgraph pull` instead. If the user asked for another location, use it and replace the path in every step below.

## 3. Add the graph to the status line

Read `~/.claude/settings.json` first (it may not exist yet). Copy it to `~/.claude/settings.json.bak` before changing it. Then pick one case:

- **No `statusLine` yet.** Add this and keep every other setting as it was:

  ```json
  "statusLine": {
    "type": "command",
    "command": "python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --statusline",
    "refreshInterval": 10
  }
  ```

- **`statusLine` runs a script file.** Do not replace it. Read the script, find the variable that holds the current folder (often read from the JSON's `workspace.current_dir`), and append the graph as the last output:

  ```sh
  printf '\n%s' "$(python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --color "$cwd")"
  ```

  Use the script's own variable name in place of `$cwd`. Also add `"refreshInterval": 10` to the `statusLine` block if it has none.

- **`statusLine` is an inline command.** Show the user the current command and ask whether to replace it or to move it into a script file that also prints the graph.

## 4. Add the `gitgraph` command

Create `~/.local/bin/gitgraph`:

```sh
#!/bin/sh
case "$1" in
  learn|legend|log|demo) exec python3 "$HOME/tools/claude-statusline-gitgraph/gitgraph.py" "$@" --color ;;
  *)                     exec python3 "$HOME/tools/claude-statusline-gitgraph/gitgraph.py" log --color "$@" ;;
esac
```

Run `chmod +x ~/.local/bin/gitgraph`. If `~/.local/bin` is not on the user's `PATH`, tell them and show the line to add to their shell profile; do not edit the profile without asking.

If the user talks to you in Chinese, change both `exec python3` to `exec env GITGRAPH_LANG=zh python3` so the tutorial and details are in Chinese.

## 5. Check that it works

Run `python3 ~/tools/claude-statusline-gitgraph/gitgraph.py --color` inside any git repository and show the user the output. Then tell them:

- the graph appears under the prompt from the next turn on (outside a git repository it stays empty);
- `! gitgraph` shows the graph with each branch explained;
- `! gitgraph learn` is a hands-on git tutorial for beginners;
- `! gitgraph demo` shows a simulated team of subagents working on branches;
- `! gitgraph legend` explains every symbol.

## Do not

- use `sudo`, or install anything with pip, brew or npm;
- change settings other than `statusLine`;
- commit, push or change anything in the user's own repositories.
