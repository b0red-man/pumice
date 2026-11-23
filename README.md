# Pumice

**Like Obsidian, without the weight.**

<img src="assets/demo.gif" width="750" alt="Pumice demo">

Quick installation:
```sh
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/b0red-man/pumice/main/main.py -o ~/.local/bin/pumice
chmod +x ~/.local/bin/pumice
```

Pumice keeps a folder of plain Markdown notes healthy. It repairs `[[WikiLinks]]` when notes are renamed or deleted, and it commits and syncs the folder with Git. It is one Python file with no dependencies.

```console
$ mv ideas.md project-ideas.md
$ pumice sync
fixed 3 link(s): ideas -> project-ideas
committed: pumice: renamed ideas -> project-ideas (2 changed, 1 renamed)
synced with remote
pushed to remote
```

## Why

- **Edit with anything.** Use `vim`, `mv`, a script or an AI agent. Pumice fixes the links afterward.
- **One file, no dependencies.** Python 3 standard library only, short enough to read in an afternoon.
- **No index or database.** Your files and Git history are the only state.
- **Local and private.** No accounts, no telemetry. The only network traffic is your own `git`.
- **Every change is a Git commit,** and `pumice undo` reverts the last one.
- **Runs headless.** It works over SSH on a server or a Raspberry Pi and uses no memory between runs.
- **Easy to leave.** `pumice uninstall` removes the timer. What remains is plain Markdown in a normal Git repository.

## Install

Use the commands at the top of this page. Pumice needs `git` and Python 3. Automatic syncing needs Linux with `systemd --user`; elsewhere you run `pumice sync` by hand or from cron.

Make sure `~/.local/bin` is on your `PATH`. To update, run the same `curl` again, then `pumice init` in each vault.

## Quick start

```sh
mkdir ~/notes && cd ~/notes
pumice init --remote git@github.com:you/notes.git   # --remote is optional
```

`init` sets up Git, creates `.pumice/cfg.json`, writes `AGENTS.md` and `CLAUDE.md` for AI agents, installs a sync timer, and runs a health check (Git identity, remote, lingering). Then work normally:

```sh
mv old-name.md new-name.md
pumice status     # what a sync would fix
pumice sync       # fix links, commit, pull, push (the timer does this too)
pumice undo       # revert the last pumice commit
```

## Commands

| Command | What it does |
| :--- | :--- |
| `pumice init [path] [--remote URL] [--no-agents]` | Create or repair a vault. Safe to re-run. |
| `pumice sync` | Fix links, commit, then pull and push if a remote is set. |
| `pumice status` | Branch, last sync, timer state, and pending renames or deletes. |
| `pumice config [key] [value]` | List, read or change settings. |
| `pumice add [text] [-f FILE]` | Append a timestamped bullet to today's daily note (`YYYY-MM-DD.md`), or to `FILE`. Reads stdin if no text is given. |
| `pumice check` | List dead links and orphan notes (notes nothing links to). Exits 1 if there are dead links. |
| `pumice backlinks <note>` | List the notes that link to a note, with link counts. |
| `pumice doctor` | Check Git identity, remote access and lingering. Exits 1 if anything is wrong. |
| `pumice undo` | Revert the last commit, only if Pumice made it. |
| `pumice uninstall [path] [--all]` | Remove the timer for one vault or all. Notes are never touched. |

Commands find the vault by walking up from the current directory. Use `--vault PATH` or `PUMICE_VAULT` to run from anywhere.

## Settings

Stored in `.pumice/cfg.json` (git-ignored, per machine). Edit it by hand or with `pumice config`.

| Setting | Default | Meaning |
| :--- | :--- | :--- |
| `auto-fix-links` | `true` | Rewrite links after renames and deletes. |
| `on-delete` | `warn` | `warn` lists notes that still link to a deleted note. `unwrap` turns `[[gone\|Alias]]` into `Alias`. |
| `remote-url` | `""` | Git remote to sync with. |
| `sync-interval` | `600` | Seconds between automatic syncs (minimum 10). |
| `add-folder` | `""` | Folder for daily notes created by `pumice add`. Empty means the vault root. |

## Limitations

- Only `[[WikiLinks]]` are rewritten (notes and attachments such as `![[image.png]]`), not `[text](note.md)` links.
- Links inside code blocks are rewritten too.
- Renaming a note and rewriting most of it in one go looks like a delete to Git. Rename first, sync, then edit.
- If the remote conflicts with local changes, `sync` stops, leaves the repository clean and pushes nothing. Resolve it with `git pull --rebase`, then sync again.
- Timer syncs over SSH need a key without a passphrase, since a timer has no SSH agent. To keep the timer running after logout, run `loginctl enable-linger` once.

## Uninstall

```sh
pumice uninstall --all
rm ~/.local/bin/pumice
```

## License

MIT. See [LICENSE](LICENSE).
