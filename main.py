#!/usr/bin/env python3
# Pumice, MIT License, Copyright (c) 2026 Arav Verma
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import Counter, namedtuple
from datetime import datetime
from pathlib import Path

# ---- constants ----

MIN_INTERVAL = 10
SPEC = {
    "auto-fix-links": (True, lambda v: type(v) is bool, "true or false"),
    "on-delete": ("warn", lambda v: v in ("warn", "unwrap"), "one of: warn, unwrap"),
    "remote-url": ("", lambda v: isinstance(v, str), "text"),
    "sync-interval": (
        600,
        lambda v: type(v) is int and v >= MIN_INTERVAL,
        f"a whole number of seconds, at least {MIN_INTERVAL}",
    ),
    "add-folder": (
        "",
        lambda v: isinstance(v, str) and not v.startswith("/") and ".." not in v.split("/") and not hidden(v),
        "a folder inside the vault (empty for the vault root)",
    ),
}
SETTINGS = {key: default for key, (default, _, _) in SPEC.items()}
COMMIT_PREFIX = "pumice:"
LINK_RE = re.compile(r"(!?)\[\[([^\[\]|#]+?)(#[^\[\]|]*)?(\|[^\[\]]*)?\]\]")
AGENT_FILES = {
    "AGENTS.md": """# Notes vault

Plain Markdown notes linked with [[WikiLinks]]. Pumice keeps links and git history in sync.

- Rename, move and delete notes with `mv`, `git mv` and `rm` inside this directory.
- Afterwards run `pumice sync` to fix links to the changed notes and commit.
- If a rewrite looks wrong, run `pumice undo`.
- `pumice status` shows pending changes.
- Never edit `.git/` or `.pumice/`.
""",
    "CLAUDE.md": "@AGENTS.md\n",
}

# ---- helpers ----


def die(msg):
    print(f"pumice: {msg}", file=sys.stderr)
    sys.exit(1)


def git_run(vault, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env} if env else None,
    )


def git(vault, *args, check=True, env=None):
    p = git_run(vault, *args, env=env)
    if check and p.returncode:
        die(f"git {args[0]} failed: {p.stderr.strip()}")
    return p.stdout


def git_dir(vault):
    return Path(git(vault, "rev-parse", "--absolute-git-dir").strip())


def has_origin(vault):
    return "origin" in git(vault, "remote").split()


def try_lock(vault, mode):
    f = open(git_dir(vault) / "pumice.lock", "w")
    try:
        fcntl.flock(f, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def hidden(path):
    return any(part.startswith(".") for part in path.split("/"))


def ago(seconds):
    seconds = max(0, int(seconds))
    for unit, size in (("day", 86400), ("hour", 3600), ("min", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}" + ("s" if unit == "day" and n > 1 else "")
    return f"{seconds} s"


# ---- vault and settings ----


def cfg_path(vault):
    return vault / ".pumice" / "cfg.json"


def is_vault(path):
    return cfg_path(path).is_file()


def find_vault(arg=None):
    start = arg or os.environ.get("PUMICE_VAULT")
    if start:
        path = Path(start).expanduser().resolve()
        if not is_vault(path):
            die(f"not a vault: {path} (run 'pumice init')")
        return path
    for d in [Path.cwd(), *Path.cwd().parents]:
        if is_vault(d):
            return d
    die("no vault found (run 'pumice init', or use --vault / PUMICE_VAULT)")


def load_cfg(vault):
    try:
        data = json.loads(cfg_path(vault).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"invalid {cfg_path(vault)}: {e}")
    if not isinstance(data, dict):
        die(f"invalid {cfg_path(vault)}: expected a JSON object")
    for key, value in data.items():
        if key in SPEC and not SPEC[key][1](value):
            die(f"invalid {key} in {cfg_path(vault)}: {value!r} (expects {SPEC[key][2]})")
    return {**SETTINGS, **data}


def write_cfg(vault, cfg):
    path = cfg_path(vault)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def parse_value(key, raw):
    default, raw = SETTINGS[key], raw.strip()
    if isinstance(default, bool):
        value = True if raw.lower() in ("1", "true", "yes", "on") else False if raw.lower() in ("0", "false", "no", "off") else raw
    elif isinstance(default, int):
        value = int(raw) if raw.isdigit() else raw
    else:
        value = raw
    if not SPEC[key][1](value):
        die(f"{key} expects {SPEC[key][2]}")
    return value


def ensure_gitignore(vault, entries):
    path = vault / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    have = {line.strip() for line in text.splitlines()}
    missing = [e for e in entries if e not in have]
    if missing:
        sep = "" if not text or text.endswith("\n") else "\n"
        path.write_text(text + sep + "\n".join(missing) + "\n", encoding="utf-8")


def apply_remote(vault, url):
    exists = has_origin(vault)
    if url:
        git(vault, "remote", "set-url" if exists else "add", "origin", url)
    elif exists:
        git(vault, "remote", "remove", "origin")


def reconcile_remote(vault, url):
    if not url or git(vault, "remote", "get-url", "origin", check=False).strip() == url:
        return False
    apply_remote(vault, url)
    return True


# ---- link engine ----


def leaf(path):
    return path.rsplit("/", 1)[-1]


def base(path):
    return leaf(path).casefold()


def by_base(paths):
    index = {}
    for p in paths:
        index.setdefault(base(p), []).append(p)
    return index


def match(target, path):
    target, path = target.casefold(), path.casefold()
    return path == target or path.endswith("/" + target)


def retarget(target, old, new):
    if "/" not in target:
        return leaf(new)
    if target == old:
        return new
    return "/".join(new.split("/")[-(target.count("/") + 1):])


class Links:
    def __init__(self, existing, renames=None, deleted=None):
        self.renames = renames or {}
        self.renamed = by_base(self.renames)
        self.gone = by_base(deleted or ())
        self.existing = by_base(existing)
        self.hits = Counter()
        self.unwrapped = Counter()

    def targets(self, target):
        return [p for p in self.existing.get(base(target), ()) if match(target, p)]

    def resolves(self, target):
        return bool(self.targets(target))

    def rewrite(self, text, unwrap):
        updated, dead = 0, set()

        def sub(m):
            nonlocal updated
            embed, raw, anchor, alias = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
            target = link_key(raw)
            if self.resolves(target):
                return m.group(0)
            for old in self.renamed.get(base(target), ()):
                if match(target, old):
                    new = retarget(target, old, self.renames[old])
                    if new == target:
                        return m.group(0)
                    updated += 1
                    self.hits[old] += 1
                    return f"{embed}[[{new}{raw[len(target):]}{anchor or ''}{alias or ''}]]"
            for p in self.gone.get(base(target), ()):
                if match(target, p):
                    self.hits[p] += 1
                    if unwrap and not embed:
                        updated += 1
                        self.unwrapped[p] += 1
                        return alias[1:] if alias else target
                    dead.add(target)
                    break
            return m.group(0)

        return LINK_RE.sub(sub, text), updated, dead


def read_note(path):
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_note(path, text):
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def link_key(path):
    return path[:-3] if path.endswith(".md") else path


def vault_files(vault):
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if not f.startswith("."):
                yield (Path(root) / f).relative_to(vault).as_posix()


def vault_index(vault, renames=None, deleted=None):
    files = list(vault_files(vault))
    notes = [f for f in files if f.endswith(".md") and f not in AGENT_FILES]
    return notes, Links([link_key(f) for f in files], renames, deleted)


def scan_links(vault):
    notes, links = vault_index(vault)
    edges = []
    for rel in notes:
        for m in LINK_RE.finditer(read_note(vault / rel)):
            raw = m.group(2).strip()
            edges.append((rel, f"{m.group(1)}[[{raw}]]", links.targets(link_key(raw))))
    return notes, links, edges


# ---- git sync ----


def detect_changes(vault):
    gd = git_dir(vault)
    index = gd / "pumice-index"
    if (gd / "index").exists():
        shutil.copyfile(gd / "index", index)
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        git(vault, "add", "-A", env=env)
        out = git(vault, "diff", "--cached", "--name-status", "-M", "-z", env=env)
    finally:
        index.unlink(missing_ok=True)
    renames, deleted = {}, set()
    tokens = iter(out.split("\0"))
    for status in tokens:
        if not status:
            break
        old = next(tokens)
        if status[0] == "R":
            new = next(tokens)
            if not hidden(old) and not hidden(new):
                renames[link_key(old)] = link_key(new)
        elif status[0] == "D" and not hidden(old):
            deleted.add(link_key(old))
    return renames, deleted


Plan = namedtuple("Plan", "renames deleted links pending dead total")


def plan_links(vault):
    cfg = load_cfg(vault)
    renames, deleted = detect_changes(vault) if cfg["auto-fix-links"] else ({}, set())
    notes, links = vault_index(vault, renames, deleted)
    pending, dead, total = [], {}, 0
    for rel in notes:
        new, n, stems = links.rewrite(read_note(vault / rel), cfg["on-delete"] == "unwrap")
        for stem in stems:
            dead.setdefault(stem, []).append(rel)
        if n:
            total += n
            pending.append((rel, new))
    return Plan(renames, deleted, links, pending, dead, total)


def commit_message(plan):
    if len(plan.renames) == 1:
        (old, new), = plan.renames.items()
        return f"{COMMIT_PREFIX} renamed {old} -> {new}"
    if plan.renames:
        return f"{COMMIT_PREFIX} renamed {len(plan.renames)} files"
    if plan.total:
        return f"{COMMIT_PREFIX} unwrapped {plan.total} link(s)"
    return f"{COMMIT_PREFIX} sync"


def sync_remote(vault):
    if not has_origin(vault):
        return False
    if git_run(vault, "rev-parse", "--verify", "-q", "HEAD").returncode:
        return True
    branch = git(vault, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        die("detached HEAD, cannot sync with remote")
    fetch = git_run(vault, "fetch", "-q", "origin")
    if fetch.returncode:
        die(f"fetch failed (local changes are committed): {fetch.stderr.strip()}")
    if git_run(vault, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}").returncode == 0:
        pull = git_run(vault, "pull", "--rebase", "--autostash", "-q", "origin", branch)
        if pull.returncode:
            git_run(vault, "rebase", "--abort")
            conflicts = [l for l in pull.stdout.splitlines() if l.startswith("CONFLICT")]
            die("remote changes conflict with local changes; rebase aborted, nothing pushed.\n" + "\n".join(conflicts))
        print("synced with remote")
    push = git_run(vault, "push", "-q", "-u", "origin", branch)
    if push.returncode:
        die(f"push failed (local changes are committed): {push.stderr.strip()}")
    print("pushed to remote")
    return True


# ---- systemd and cron ----


def unit_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def vault_hash(vault):
    return hashlib.sha1(str(vault).encode()).hexdigest()[:6]


def unit_base(vault):
    return f"pumice-{re.sub(r'[^A-Za-z0-9_-]', '-', vault.name) or 'vault'}-{vault_hash(vault)}"


def unit_quote(arg):
    arg = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{arg}"'


def systemctl(*args, check=True):
    p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if check and p.returncode:
        die(f"systemctl {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def systemd_ok():
    return bool(shutil.which("systemctl")) and systemctl("show-environment", check=False).returncode == 0


def sync_command(vault):
    return [sys.executable, os.path.abspath(sys.argv[0]), "sync", "--vault", str(vault)]


def cron_schedule(interval):
    minutes = max(1, interval // 60)
    if minutes < 60:
        return f"*/{minutes} * * * *"
    hours = minutes // 60
    return f"0 */{hours} * * *" if hours < 24 else "0 0 * * *"


def install_units(vault):
    name = unit_base(vault)
    interval = load_cfg(vault)["sync-interval"]
    exec_start = " ".join(unit_quote(a) for a in sync_command(vault))
    label = str(vault).replace("%", "%%")
    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}-sync.service").write_text(
        f"[Unit]\nDescription=Pumice sync for {label}\n\n"
        f"[Service]\nType=oneshot\nTimeoutStartSec=5min\nExecStart={exec_start}\n",
        encoding="utf-8",
    )
    (d / f"{name}-sync.timer").write_text(
        f"[Unit]\nDescription=Pumice sync timer for {label}\n\n"
        f"[Timer]\nOnBootSec=60\nOnUnitActiveSec={interval}\nAccuracySec=1s\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    systemctl("daemon-reload")
    systemctl("enable", f"{name}-sync.timer")
    systemctl("restart", f"{name}-sync.timer")


def remove_units(pattern):
    files = sorted(unit_dir().glob(pattern))
    for f in files:
        if f.suffix == ".timer":
            systemctl("disable", "--now", f.name, check=False)
    for f in files:
        f.unlink()
    if files:
        systemctl("daemon-reload", check=False)
    return files


def timer_status(vault):
    if not systemd_ok():
        return "no systemd (run 'pumice sync' manually or from cron)"
    name = f"{unit_base(vault)}-sync.timer"
    if not (unit_dir() / name).exists():
        return "not installed (run 'pumice init')"
    state = systemctl("is-active", name, check=False).stdout.strip() or "unknown"
    out = systemctl("list-timers", name, "--all", "--output=json", "--no-pager", check=False).stdout
    try:
        nxt = next(t["next"] for t in json.loads(out) if t.get("unit") == name and t.get("next"))
        state += f", next in {ago(nxt / 1e6 - time.time())}"
    except (ValueError, StopIteration, KeyError, TypeError):
        pass
    return f"{state} (every {load_cfg(vault)['sync-interval']}s)"


def health_checks(vault):
    checks = []
    configured = all(git_run(vault, "config", key).stdout.strip() for key in ("user.name", "user.email"))
    checks.append((
        "git identity",
        None if configured else 'set it with: git config --global user.name "Your Name" && git config --global user.email you@example.com',
    ))
    if has_origin(vault):
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
        env.pop("SSH_AUTH_SOCK", None)
        try:
            p = subprocess.run(["git", "-C", str(vault), "ls-remote", "--exit-code", "origin"], capture_output=True, text=True, timeout=15, env=env)
            problem = None if p.returncode in (0, 2) else "cannot reach the remote without a prompt or SSH agent, which timers cannot use: " + (p.stderr.strip().splitlines() or ["unknown error"])[0]
        except subprocess.TimeoutExpired:
            problem = "remote did not answer within 15 seconds"
        checks.append(("remote", problem))
    if systemd_ok() and shutil.which("loginctl"):
        out = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger"], capture_output=True, text=True).stdout
        checks.append(("linger", None if "Linger=yes" in out else "off, so the timer stops when you log out. Fix: loginctl enable-linger"))
    return checks


def report_health(vault):
    print("health check:")
    checks = health_checks(vault)
    for label, problem in checks:
        print(f"  {label:<14}{'ok' if problem is None else 'WARNING: ' + problem}")
    return all(problem is None for _, problem in checks)


# ---- commands ----


def cmd_init(args):
    if not shutil.which("git"):
        die("git is not installed")
    vault = Path(args.path).expanduser().resolve()
    vault.mkdir(parents=True, exist_ok=True)
    if not (vault / ".git").exists():
        git(vault, "init", "-q", "-b", "main")
        print(f"initialized git repository in {vault}")
    agent_files = {} if args.no_agents else AGENT_FILES
    ensure_gitignore(vault, [".pumice/", *agent_files])
    for name, text in agent_files.items():
        if not (vault / name).exists():
            (vault / name).write_text(text, encoding="utf-8")
            print(f"created {name}")
    if is_vault(vault):
        print(f"already a pumice vault: {vault}")
        cfg = load_cfg(vault)
    else:
        cfg = dict(SETTINGS)
        write_cfg(vault, cfg)
        print(f"created {cfg_path(vault).relative_to(vault)}")
    if args.remote is not None:
        cfg["remote-url"] = args.remote.strip()
        write_cfg(vault, cfg)
        apply_remote(vault, cfg["remote-url"])
        print(f"remote origin: {cfg['remote-url'] or '(none)'}")
    elif reconcile_remote(vault, cfg["remote-url"]):
        print(f"remote origin set to {cfg['remote-url']}")
    if systemd_ok():
        install_units(vault)
        print(f"systemd timer installed: sync every {cfg['sync-interval']}s")
    else:
        line = f"{cron_schedule(cfg['sync-interval'])} {shlex.join(sync_command(vault))}"
        print(f"systemd not found. Automation is off.\nRun 'pumice sync' manually, or add this line with 'crontab -e':\n{line}")
    report_health(vault)


def change_summary(vault):
    counts = Counter(l[0] for l in git(vault, "diff", "--cached", "--name-status", "-M").splitlines())
    words = (("A", "added"), ("M", "changed"), ("D", "removed"), ("R", "renamed"))
    return ", ".join(f"{counts[k]} {word}" for k, word in words if counts[k])


def cmd_sync(args):
    vault = find_vault(args.vault)
    lock = try_lock(vault, fcntl.LOCK_EX)
    if not lock:
        print("sync already running")
        return
    url = load_cfg(vault)["remote-url"]
    if reconcile_remote(vault, url):
        print(f"remote origin set to {url}")
    plan = plan_links(vault)
    for old, new in sorted(plan.renames.items()):
        if plan.links.hits[old]:
            print(f"fixed {plan.links.hits[old]} link(s): {old} -> {new}")
    for path in sorted(plan.deleted):
        if plan.links.unwrapped[path]:
            print(f"unwrapped {plan.links.unwrapped[path]} link(s) to deleted {path}")
    for stem, files in sorted(plan.dead.items()):
        print(f"warning: [[{stem}]] was deleted but is still linked in: {', '.join(sorted(files))}")
    for rel, new in plan.pending:
        write_note(vault / rel, new)
    git(vault, "add", "-A")
    summary = change_summary(vault)
    if summary:
        message = commit_message(plan)
        git(vault, "commit", "-q", "-m", message)
        print(f"committed: {message} ({summary})")
    if not sync_remote(vault) and not summary:
        print("nothing to sync")


def cmd_status(args):
    vault = find_vault(args.vault)
    cfg = load_cfg(vault)
    rows = [("vault", str(vault))]

    branch = git_run(vault, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "(detached)"
    if not has_origin(vault):
        remote = "no remote"
    elif git_run(vault, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}").returncode:
        remote = "origin: not pushed yet"
    else:
        ahead, behind = git(vault, "rev-list", "--left-right", "--count", f"HEAD...refs/remotes/origin/{branch}").split()
        remote = f"origin: {ahead} ahead, {behind} behind"
    rows.append(("branch", f"{branch} ({remote})"))

    last = git_run(vault, "log", "-1", "--grep", f"^{COMMIT_PREFIX}", "--format=%ct%x09%s").stdout.strip()
    if last:
        stamp, subject = last.split("\t", 1)
        rows.append(("last sync", f"{ago(time.time() - int(stamp))} ago ({subject})"))
    else:
        rows.append(("last sync", "never"))
    rows.append(("timer", timer_status(vault)))

    probe = try_lock(vault, fcntl.LOCK_SH)
    if probe:
        probe.close()
    else:
        rows.append(("sync", "running now"))

    changed = len(git(vault, "status", "--porcelain").splitlines())
    pending = f"{changed} uncommitted file(s)" if changed else "nothing"
    rows.append(("pending", pending + ("" if cfg["auto-fix-links"] else ", auto-fix-links is off")))

    plan = plan_links(vault)
    hits = plan.links.hits
    detail = [f"  rename  {old} -> {new}  ({hits[old]} links to fix)" for old, new in sorted(plan.renames.items())]
    outcome = "will be unwrapped" if cfg["on-delete"] == "unwrap" else "still point at it"
    detail += [f"  delete  {path}  ({hits[path]} links {outcome})" for path in sorted(plan.deleted)]

    for key, value in rows:
        print(f"{key + ':':<11}{value}")
    for line in detail:
        print(line)


def cmd_config(args):
    vault = find_vault(args.vault)
    cfg = load_cfg(vault)
    if args.key is None:
        for key in SETTINGS:
            print(f"{key} {json.dumps(cfg[key])}")
        return
    if args.key not in SETTINGS:
        die(f"unknown setting: {args.key} (known: {', '.join(SETTINGS)})")
    if args.value is None:
        print(json.dumps(cfg[args.key]))
        return
    cfg[args.key] = parse_value(args.key, args.value)
    write_cfg(vault, cfg)
    if args.key == "remote-url":
        apply_remote(vault, cfg["remote-url"])
    if args.key == "sync-interval" and (unit_dir() / f"{unit_base(vault)}-sync.timer").exists():
        install_units(vault)


def cmd_doctor(args):
    sys.exit(0 if report_health(find_vault(args.vault)) else 1)


def cmd_check(args):
    notes, _, edges = scan_links(find_vault(args.vault))
    dead = {}
    for source, shown, candidates in edges:
        if not candidates and shown not in dead.setdefault(source, []):
            dead[source].append(shown)
    linked = {c for source, _, candidates in edges for c in candidates if c != link_key(source)}
    orphans = [n for n in notes if link_key(n) not in linked]
    if dead:
        print(f"dead links ({sum(len(v) for v in dead.values())}):")
        for source in sorted(dead):
            print(f"  {source}: {', '.join(dead[source])}")
    if orphans:
        print(f"orphan notes ({len(orphans)}):")
        for n in sorted(orphans):
            print(f"  {n}")
    if not dead and not orphans:
        print("no problems found")
    sys.exit(1 if dead else 0)


def cmd_backlinks(args):
    _, links, edges = scan_links(find_vault(args.vault))
    wanted = links.targets(link_key(args.note))
    if not wanted:
        die(f"no note matches {args.note}")
    counts = Counter(source for source, _, candidates in edges if set(candidates) & set(wanted) and link_key(source) not in wanted)
    print(f"{len(counts)} note(s) link to {', '.join(wanted)}:")
    for source in sorted(counts):
        print(f"  {source} ({counts[source]})")


def cmd_add(args):
    vault = find_vault(args.vault)
    text = " ".join(args.text).strip() or ("" if sys.stdin.isatty() else sys.stdin.read().strip())
    if not text:
        die("nothing to add (give text or pipe it in)")
    now = datetime.now()
    if args.file:
        rel = args.file if Path(args.file).suffix else args.file + ".md"
    else:
        rel = f"{load_cfg(vault)['add-folder'].strip('/')}/{now:%Y-%m-%d}.md".lstrip("/")
    path = (vault / rel).resolve()
    try:
        inside = path.relative_to(vault).as_posix()
    except ValueError:
        die(f"{rel} is outside the vault")
    if hidden(inside):
        die(f"{rel} is in a hidden folder")
    first, *rest = text.splitlines()
    entry = f"- {now:%H:%M} {first}\n" + "".join(f"  {line}\n" for line in rest)
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = ""
    if path.exists() and path.stat().st_size:
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            prefix = "" if f.read(1) == b"\n" else "\n"
    elif not args.file:
        prefix = f"# {now:%Y-%m-%d}\n\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(prefix + entry)
    print(f"added to {inside}")


def cmd_undo(args):
    vault = find_vault(args.vault)
    subject = git(vault, "log", "-1", "--format=%s", check=False).strip()
    if not subject.startswith(COMMIT_PREFIX):
        die(f"HEAD is not a pumice commit, refusing to revert: {subject or '(no commits)'}")
    git(vault, "revert", "--no-edit", "HEAD")
    print(f"reverted: {subject}")


def cmd_uninstall(args):
    if args.all and args.path:
        die("--all takes no path")
    if args.all:
        prefix = "Description=Pumice sync for "
        vaults = [
            line[len(prefix):].replace("%%", "%")
            for f in sorted(unit_dir().glob("pumice-*-sync.service"))
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)
        ]
        files = remove_units("pumice-*")
    else:
        vault = Path(args.path).expanduser().resolve() if args.path else find_vault(args.vault)
        vaults = [str(vault)]
        files = remove_units(f"pumice-*-{vault_hash(vault)}-*")
    for v in vaults if files else []:
        print(f"removed units for {v}")
    if not files:
        print("no systemd units found; if you added a cron line, remove it with 'crontab -e'")
    elif args.all:
        print(f"to remove pumice itself: rm {os.path.abspath(sys.argv[0])}")


# ---- entry point ----


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--vault", metavar="PATH", default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="pumice", parents=[common], epilog="run 'pumice <command> --help' for a command's options")
    sub = p.add_subparsers(dest="command", title="commands")

    s = sub.add_parser("init", help="create a vault (--remote URL, --no-agents)")
    s.add_argument("path", nargs="?", default=".")
    s.add_argument("--remote", metavar="URL", help="git remote to sync with")
    s.add_argument("--no-agents", action="store_true", help="do not create AGENTS.md and CLAUDE.md")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("sync", parents=[common], help="fix links, commit, then pull and push")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("status", parents=[common], help="show pending changes and sync state")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("config", parents=[common], help="list, get or set vault settings")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("uninstall", parents=[common], help="remove systemd units (--all: every vault)")
    s.add_argument("path", nargs="?")
    s.add_argument("--all", action="store_true", help="remove units for every vault")
    s.set_defaults(func=cmd_uninstall)

    s = sub.add_parser("check", parents=[common], help="list dead links and orphan notes")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("backlinks", parents=[common], help="list the notes that link to NOTE")
    s.add_argument("note")
    s.set_defaults(func=cmd_backlinks)

    s = sub.add_parser("add", parents=[common], help="append to today's daily note (-f FILE)")
    s.add_argument("text", nargs="*", help="the entry (or pipe it in)")
    s.add_argument("-f", "--file", metavar="FILE", help="note to append to instead, relative to the vault root")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("doctor", parents=[common], help="check git identity, remote and timer setup")
    s.set_defaults(func=cmd_doctor)

    s = sub.add_parser("undo", parents=[common], help="revert the last pumice commit")
    s.set_defaults(func=cmd_undo)
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)
    args.vault = getattr(args, "vault", None)
    args.func(args)


if __name__ == "__main__":
    main()
