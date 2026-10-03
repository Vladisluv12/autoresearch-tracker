#!/usr/bin/env python3
"""A dependency-free task tracker shared through origin's coordination branch.

Every write uses an explicit Git compare-and-swap lease. All Git objects and
temporary refs live in a temporary bare repository, never in your checkout.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator
import uuid


BRANCH = "coordination"
REF = f"refs/heads/{BRANCH}"
FETCH_REF = f"refs/remotes/origin/{BRANCH}"
SCHEMA_VERSION = 1
MAX_ATTEMPTS = 8
GIT_TIMEOUT = 45
STALE_MINUTES = 30
ACTIVE = {"doing", "blocked", "review"}
STATUSES = ACTIVE | {"todo", "done"}
AGENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
ID_PATTERN = re.compile(r"T[0-9]{3,}\Z")
HYPOTHESIS_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


class CoordError(Exception):
    """A concise, actionable error for the CLI."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CoordError("Invalid coordination state: timestamp must be a string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoordError(f"Invalid coordination timestamp: {value!r}.") from exc
    if parsed.tzinfo is None:
        raise CoordError("Invalid coordination state: timestamps need a timezone.")
    return parsed


def nonempty(value: str, label: str, limit: int = 10000) -> str:
    value = value.strip()
    if not value or len(value) > limit or "\x00" in value:
        raise CoordError(f"{label} must contain 1–{limit} characters and no NUL bytes.")
    return value


def agent_id(value: str) -> str:
    if not AGENT_PATTERN.fullmatch(value):
        raise CoordError("Agent ID must be 1–64 ASCII letters, digits, dots, underscores or hyphens; start with a letter or digit.")
    return value


def role_name(value: str) -> str:
    value = nonempty(value, "Role", 80)
    if any(ord(char) < 32 for char in value):
        raise CoordError("Role must be a single line without control characters.")
    return value


def hypothesis_slug(value: str) -> str:
    if not HYPOTHESIS_PATTERN.fullmatch(value):
        raise CoordError("Hypothesis must be a slug of 1–64 lowercase ASCII letters, digits or hyphens, for example lr-warmup.")
    return value


def task_id(value: str) -> str:
    value = value.upper()
    if not ID_PATTERN.fullmatch(value) or int(value[1:]) < 1:
        raise CoordError("Task ID must look like T001.")
    if value != f"T{int(value[1:]):03d}":
        raise CoordError("Use the canonical task ID, for example T001.")
    return value


def scope_path(value: str) -> str:
    value = value.strip()
    if value in {"*", ".", "./"}:
        return "*"
    if not value or any(ord(char) < 32 for char in value):
        raise CoordError("Scope must be a nonempty repository-relative path or '*'.")
    if "\\" in value or any(char in value for char in "*?[]"):
        raise CoordError("Use forward-slash scope paths; only the whole-repository '*' wildcard is supported.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] == ".git":
        raise CoordError("Scope must stay inside the repository and outside .git.")
    if re.match(r"^[A-Za-z]:", value):
        raise CoordError("Scope must be relative, not a drive path.")
    return str(path)


def scopes_overlap(left: str, right: str) -> bool:
    return left == "*" or right == "*" or left == right or left.startswith(right + "/") or right.startswith(left + "/")


def redact(value: str) -> str:
    return re.sub(r"(https?://)[^/\s@]+@", r"\1<redacted>@", value)


def git(cwd: str, *args: str, input_text: str | None = None, check: bool = True, isolated_config: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Inherited worktree/index variables must never redirect plumbing into the
    # user's checkout. Authentication and transport environment remain intact.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(key, None)
    if isolated_config:
        # Relevant effective transport/auth config has already been copied from
        # the real checkout. Avoid applying global multi-valued headers twice.
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        for key in list(env):
            if key in {"GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} or re.match(r"GIT_CONFIG_(KEY|VALUE)_[0-9]+\Z", key):
                env.pop(key, None)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "LC_ALL": "C"})
    try:
        raw_result = subprocess.run(
            ["git", "-C", cwd, *args],
            input=input_text.encode("utf-8") if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            timeout=GIT_TIMEOUT, check=False,
        )
    except FileNotFoundError as exc:
        raise CoordError("Git is not installed or is not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        extra = " The push outcome is unknown; inspect the board before repeating the command." if args and args[0] == "push" else ""
        raise CoordError(f"Git timed out after {GIT_TIMEOUT} seconds.{extra}") from exc
    # Git's plumbing is a byte protocol. Text-mode pipes translate LF to CRLF
    # on Windows (including mktree paths), and use the machine's locale for
    # Unicode. Keep UTF-8 bytes intact on every OS, including NUL delimiters.
    try:
        stdout = raw_result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoordError("Git returned invalid UTF-8; refusing to corrupt coordination data.") from exc
    result = subprocess.CompletedProcess(
        raw_result.args, raw_result.returncode,
        stdout,
        raw_result.stderr.decode("utf-8", errors="replace"),
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CoordError(f"Git {args[0]} failed: {redact(detail)}")
    return result


def repo_root() -> str:
    result = git(os.getcwd(), "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise CoordError("Run this command inside your project's Git checkout.")
    return result.stdout.strip()


def config(root: str, key: str) -> str | None:
    result = git(root, "config", "--local", "--get", key, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def identity(root: str, required: bool = True) -> tuple[str | None, str | None]:
    agent = os.environ.get("COORD_AGENT", config(root, "coord.agent"))
    role = os.environ.get("COORD_ROLE", config(root, "coord.role"))
    if agent:
        agent = agent_id(agent)
    if role:
        role = role_name(role)
    if required and (not agent or not role):
        raise CoordError("Set your identity first: python3 scripts/coord.py identity AGENT_ID --role ROLE (or set COORD_AGENT and COORD_ROLE).")
    return agent, role


def fresh_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "next_task": 1, "tasks": {}, "agents": {}, "updated_at": now()}


def validate_state(state: Any) -> None:
    if not isinstance(state, dict) or type(state.get("schema_version")) is not int or state["schema_version"] != SCHEMA_VERSION:
        raise CoordError("Unsupported coordination state schema; expected schema_version 1.")
    tasks, agents = state.get("tasks"), state.get("agents")
    if not isinstance(tasks, dict) or not isinstance(agents, dict):
        raise CoordError("Invalid coordination state: tasks and agents must be objects.")
    sequence = state.get("next_task")
    if type(sequence) is not int or sequence < 1:
        raise CoordError("Invalid coordination state: next_task must be a positive integer.")
    timestamp(state.get("updated_at"))
    for key, entry in agents.items():
        agent_id(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("role"), str):
            raise CoordError(f"Invalid agent record: {key}.")
        role_name(entry["role"])
        timestamp(entry.get("updated_at"))
    for key, task in tasks.items():
        task_id(key)
        if int(key[1:]) >= sequence or not isinstance(task, dict) or task.get("id") != key:
            raise CoordError(f"Invalid coordination task record: {key}.")
        for field in ("title", "description", "acceptance"):
            if not isinstance(task.get(field), str):
                raise CoordError(f"Invalid {field} in {key}.")
        nonempty(task["title"], "Title", 240)
        if not isinstance(task.get("status"), str) or task["status"] not in STATUSES:
            raise CoordError(f"Invalid task status in {key}.")
        scopes = task.get("scopes")
        if not isinstance(scopes, list) or not scopes or any(not isinstance(path, str) or scope_path(path) != path for path in scopes):
            raise CoordError(f"Invalid scopes in {key}.")
        dependencies = task.get("depends_on")
        if not isinstance(dependencies, list) or any(not isinstance(dep, str) or dep not in tasks or dep == key for dep in dependencies):
            raise CoordError(f"Invalid dependencies in {key}.")
        if task.get("role") is not None:
            if not isinstance(task["role"], str):
                raise CoordError(f"Invalid role in {key}.")
            role_name(task["role"])
        if task.get("hypothesis") is not None:
            if not isinstance(task["hypothesis"], str):
                raise CoordError(f"Invalid hypothesis in {key}.")
            hypothesis_slug(task["hypothesis"])
        owner = task.get("owner")
        if task["status"] in ACTIVE or task["status"] == "done":
            if not isinstance(owner, str):
                raise CoordError(f"Missing owner in {key}.")
            agent_id(owner)
            if not isinstance(task.get("owner_role"), str):
                raise CoordError(f"Missing owner role in {key}.")
            role_name(task["owner_role"])
            timestamp(task.get("heartbeat_at"))
        elif owner is not None or task.get("owner_role") is not None or task.get("heartbeat_at") is not None:
            raise CoordError(f"Todo task {key} must be unowned.")
        timestamp(task.get("created_at"))
        timestamp(task.get("updated_at"))
        for field in ("summary", "pr"):
            if task.get(field) is not None and not isinstance(task[field], str):
                raise CoordError(f"Invalid {field} in {key}.")
        if not isinstance(task.get("history"), list):
            raise CoordError(f"Missing task history in {key}.")
        for event in task["history"]:
            if not isinstance(event, dict) or not all(isinstance(event.get(field), str) for field in ("agent", "action", "note", "operation_id")):
                raise CoordError(f"Invalid history event in {key}.")
            timestamp(event.get("at"))


def markdown(value: Any) -> str:
    return str(value if value is not None else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", " ").replace("\r", " ")


def render_board(state: dict[str, Any]) -> str:
    lines = [
        "# Team coordination", "", f"Updated: {state['updated_at']} (UTC).", "",
        "Generated by `scripts/coord.py`. Change tasks through the CLI; do not edit this branch manually.", "",
        "Active tasks (`doing`, `blocked`, `review`) reserve their scopes. Owners should send a heartbeat every 10 minutes; takeover is available after 30 minutes without one.", "",
        "| Task | Status | Owner | Target role | Hypothesis | Scope | Dependencies | Updated | Title |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for task in sorted(state["tasks"].values(), key=lambda item: int(item["id"][1:])):
        row = [task["id"], task["status"], task["owner"], task["role"], task.get("hypothesis"), ", ".join(task["scopes"]), ", ".join(task["depends_on"]) or "—", task["updated_at"], task["title"]]
        lines.append("| " + " | ".join(markdown(cell) for cell in row) + " |")
    if not state["tasks"]:
        lines.extend(["", "No tasks yet."])
    for task in sorted(state["tasks"].values(), key=lambda item: int(item["id"][1:])):
        lines.extend(["", f"## {task['id']}: {markdown(task['title'])}", "", f"**Description:** {markdown(task['description'])}", "", f"**Acceptance:** {markdown(task['acceptance'])}", "", f"**Last heartbeat:** {markdown(task['heartbeat_at'])}"])
        if task.get("summary"):
            lines.extend(["", f"**Summary:** {markdown(task['summary'])}"])
        if task.get("pr"):
            lines.extend(["", f"**Pull request:** {markdown(task['pr'])}"])
        if task["history"]:
            last = task["history"][-1]
            lines.extend(["", f"**Latest event:** {markdown(last['action'])} by {markdown(last['agent'])} at {last['at']}: {markdown(last['note'])}"])
    lines.extend(["", "## Agents", "", "| Agent | Role | Last activity |", "| --- | --- | --- |"])
    for agent, data in sorted(state["agents"].items()):
        lines.append(f"| {markdown(agent)} | {markdown(data['role'])} | {data['updated_at']} |")
    return "\n".join(lines) + "\n"


class Remote:
    def __init__(self, directory: str):
        self.directory = directory

    def command(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return git(self.directory, *args, isolated_config=True, **kwargs)

    def read(self) -> tuple[str | None, dict[str, Any] | None]:
        probe = self.command("ls-remote", "--exit-code", "origin", REF, check=False)
        if probe.returncode == 2:
            return None, None
        if probe.returncode:
            raise CoordError(f"Cannot read origin: {redact((probe.stderr or probe.stdout).strip())}")
        self.command("fetch", "--quiet", "--no-tags", "--depth=1", "origin", f"+{REF}:{FETCH_REF}")
        oid = self.command("rev-parse", FETCH_REF).stdout.strip()
        result = self.command("show", f"{oid}:state.json", check=False)
        if result.returncode:
            raise CoordError(f"Origin's {BRANCH} branch has no state.json; refusing to overwrite it.")
        try:
            state = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CoordError("Invalid JSON on the coordination branch; refusing to overwrite it.") from exc
        validate_state(state)
        return oid, state

    def initial_parent(self) -> str | None:
        result = self.command("ls-remote", "origin", "HEAD")
        if not result.stdout.strip():
            return None
        self.command("fetch", "--quiet", "--no-tags", "--depth=1", "origin", "HEAD")
        return self.command("rev-parse", "FETCH_HEAD").stdout.strip()

    def commit(self, state: dict[str, Any], parent: str | None, message: str) -> str:
        files = {
            "BOARD.md": render_board(state),
            "state.json": json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        }
        entries = []
        for name, content in sorted(files.items()):
            oid = self.command("hash-object", "-w", "--stdin", input_text=content).stdout.strip()
            entries.append(f"100644 blob {oid}\t{name}\0")
        tree = self.command("mktree", "-z", input_text="".join(entries)).stdout.strip()
        args = ["-c", "user.name=Coordination Tracker", "-c", "user.email=coordination@localhost", "-c", "commit.gpgsign=false", "commit-tree", tree]
        if parent:
            args.extend(["-p", parent])
        return self.command(*args, input_text=message + "\n").stdout.strip()

    def update(self, operation: Callable[[dict[str, Any]], str], *, initialize: bool = False) -> str:
        for attempt in range(MAX_ATTEMPTS):
            old_oid, state = self.read()
            if state is None:
                if not initialize:
                    raise CoordError("Tracker is not initialized. Run: python3 scripts/coord.py init")
                state = fresh_state()
                parent = self.initial_parent()
            elif initialize:
                return f"Coordination branch '{BRANCH}' is already initialized."
            else:
                parent = old_oid
            message = operation(state)
            state["updated_at"] = now()
            validate_state(state)
            new_oid = self.commit(state, parent, message)
            pushed = self.command("push", "--porcelain", f"--force-with-lease={REF}:{old_oid or ''}", "origin", f"{new_oid}:{REF}", check=False)
            if pushed.returncode == 0:
                return message
            output = redact((pushed.stderr + "\n" + pushed.stdout).strip())
            # Only CAS rejections are safe to retry. Transport errors may happen
            # after a successful push; repeating create would duplicate work.
            conflicts = ("(stale info)", "(fetch first)", "(reference already exists)", "(incorrect old value provided)", "(failed to update ref)")
            if not any(reason in output for reason in conflicts):
                raise CoordError(f"Git push failed: {output}\nInspect the board before retrying if the connection was interrupted.")
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(min(0.05 * (attempt + 1), 0.3))
        raise CoordError("Coordination branch kept changing; retry the command in a moment.")


@contextmanager
def remote(root: str) -> Iterator[Remote]:
    push = git(root, "remote", "get-url", "--push", "origin", check=False)
    if push.returncode:
        raise CoordError("This checkout needs an 'origin' remote.")
    # Reads and writes must observe the same repository even when origin has a
    # separate push URL (for example, a fork paired with an upstream fetch URL).
    transport = git(root, "config", "--includes", "--null", "--get-regexp", r"^(credential\.|http\.|https\.|url\.|core\.sshcommand$|core\.gitproxy$|ssh\.|remote\.origin\.(proxy|proxyauthmethod)$)", check=False)
    if transport.returncode not in {0, 1}:
        raise CoordError("Cannot read the checkout's Git transport configuration.")
    # Relative filesystem remotes are interpreted relative to the real checkout,
    # not the temporary bare repository.
    def absolute_url(value: str) -> str:
        value = value.strip()
        if ":" not in value and not os.path.isabs(value):
            return os.path.abspath(os.path.join(root, value))
        return value

    with tempfile.TemporaryDirectory(prefix="coord-git-") as directory:
        git(directory, "init", "--bare", "--quiet", isolated_config=True)
        for record in transport.stdout.split("\x00"):
            if record:
                key, separator, value = record.partition("\n")
                if not separator:
                    value = "true"
                git(directory, "config", "--add", key, value, isolated_config=True)
        git(directory, "remote", "add", "origin", absolute_url(push.stdout), isolated_config=True)
        yield Remote(directory)


def get_task(state: dict[str, Any], identifier: str) -> dict[str, Any]:
    identifier = task_id(identifier)
    if identifier not in state["tasks"]:
        raise CoordError(f"Task {identifier} does not exist.")
    return state["tasks"][identifier]


def eligible(state: dict[str, Any], task: dict[str, Any], role: str) -> None:
    if task["role"] is not None and task["role"] != role:
        raise CoordError(f"{task['id']} requires role {task['role']!r}; your role is {role!r}.")
    pending = [dep for dep in task["depends_on"] if state["tasks"][dep]["status"] != "done"]
    if pending:
        raise CoordError(f"{task['id']} is waiting for dependencies: {', '.join(pending)}.")
    for other in state["tasks"].values():
        if other["id"] != task["id"] and other["status"] in ACTIVE:
            if any(scopes_overlap(left, right) for left in task["scopes"] for right in other["scopes"]):
                raise CoordError(f"Scope conflict with {other['id']} ({other['status']}, owner {other['owner']}): {', '.join(other['scopes'])}.")


def mutate_task(args: argparse.Namespace, state: dict[str, Any], agent: str, role: str, operation_id: str) -> str:
    command = args.command
    at = now()
    if command == "create":
        scopes = sorted(set(scope_path(scope) for scope in args.scope))
        dependencies = list(dict.fromkeys(task_id(dep) for dep in args.depends_on))
        for dep in dependencies:
            get_task(state, dep)
        identifier = f"T{state['next_task']:03d}"
        state["next_task"] += 1
        task = {
            "id": identifier, "title": nonempty(args.title, "Title", 240),
            "description": args.description.strip(), "acceptance": args.acceptance.strip(),
            "scopes": scopes, "depends_on": dependencies,
            "role": role_name(args.role) if args.role else None, "status": "todo",
            "hypothesis": hypothesis_slug(args.hypothesis) if args.hypothesis is not None else None,
            "owner": None, "owner_role": None, "heartbeat_at": None,
            "created_at": at, "updated_at": at, "history": [], "summary": None, "pr": None,
        }
        state["tasks"][identifier] = task
        note = task["title"]
        result = f"Created {identifier}: {task['title']}"
    else:
        task = get_task(state, args.id)
        identifier = task["id"]
        if command == "claim":
            if task["status"] != "todo":
                raise CoordError(f"{identifier} is {task['status']}, owned by {task['owner']}; only todo tasks can be claimed.")
            eligible(state, task, role)
            task.update(status="doing", owner=agent, owner_role=role, heartbeat_at=at)
            note = "Claimed task."
        elif command == "takeover":
            if task["status"] not in ACTIVE:
                raise CoordError(f"{identifier} is not an active task; use claim for todo tasks.")
            if task["owner"] == agent:
                raise CoordError(f"You already own {identifier}; use heartbeat or resume.")
            age_minutes = (timestamp(at) - timestamp(task["heartbeat_at"])).total_seconds() / 60
            threshold = args.stale_after_minutes
            if threshold < STALE_MINUTES:
                raise CoordError(f"Takeover threshold cannot be less than {STALE_MINUTES} minutes.")
            if age_minutes <= threshold:
                raise CoordError(f"{identifier} belongs to {task['owner']}; last heartbeat was {max(age_minutes, 0):.1f} minutes ago (must exceed {threshold}).")
            eligible(state, task, role)
            note = f"Previous owner {task['owner']}. " + nonempty(args.reason, "Reason")
            task.update(status="doing", owner=agent, owner_role=role, heartbeat_at=at)
        else:
            if task["owner"] != agent:
                raise CoordError(f"{identifier} belongs to {task['owner'] or 'nobody'}; only its owner can {command} it.")
            if task["status"] not in ACTIVE:
                raise CoordError(f"{identifier} is {task['status']}; only active tasks can be updated.")
            note = ""
            if command == "heartbeat":
                note = nonempty(args.note, "Note") if args.note is not None else "Heartbeat."
            elif command == "block":
                note = nonempty(args.reason, "Reason")
                task["status"] = "blocked"
            elif command == "resume":
                if task["status"] != "blocked":
                    raise CoordError(f"{identifier} must be blocked before resume.")
                task["status"] = "doing"
                note = "Resumed work."
            elif command == "handoff":
                if task["status"] not in {"doing", "review"}:
                    raise CoordError(f"{identifier} must be doing or review before handoff; resume blocked tasks first.")
                note = nonempty(args.summary, "Summary")
                if args.pr and not re.match(r"https?://[^\s]+\Z", args.pr):
                    raise CoordError("PR must be an http:// or https:// URL.")
                task.update(status="review", summary=note, pr=args.pr)
            elif command == "done":
                note = nonempty(args.summary, "Summary")
                task.update(status="done", summary=note)
            elif command == "release":
                note = nonempty(args.reason, "Reason")
                task.update(status="todo", owner=None, owner_role=None, heartbeat_at=None)
            if command != "release":
                task["heartbeat_at"] = at
        task["updated_at"] = at
        result = f"{identifier}: {command} → {task['status']} (owner: {task['owner'] or 'none'})"
    task["history"].append({"at": at, "agent": agent, "action": command, "note": note, "operation_id": operation_id})
    state["agents"][agent] = {"role": role, "updated_at": at}
    return result


def print_list(state: dict[str, Any], hypothesis: str | None = None) -> None:
    tasks = [task for task in state["tasks"].values() if hypothesis is None or task.get("hypothesis") == hypothesis]
    if not tasks:
        print("No tasks yet." if hypothesis is None else f"No tasks for hypothesis {hypothesis}.")
        return
    columns = ["ID", "STATUS", "OWNER", "ROLE", "HYPOTHESIS", "UPDATED (UTC)", "SCOPE", "TITLE"]
    rows = [[task["id"], task["status"], task["owner"] or "—", task["role"] or "any", task.get("hypothesis") or "—", task["updated_at"], ",".join(task["scopes"]), task["title"].replace("\n", " ")] for task in sorted(tasks, key=lambda item: int(item["id"][1:]))]
    widths = [max(len(row[index]) for row in [columns, *rows]) for index in range(len(columns))]
    for row in [columns, *rows]:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, epilog="Identity: COORD_AGENT / COORD_ROLE override local coord.agent / coord.role.\nWrites require origin push access. Read commands never initialize the tracker.\nUse a unique agent ID for each agent session. Quote --scope '*' in your shell.")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create origin/coordination if absent; safe to repeat")
    ident = commands.add_parser("identity", help="Save this agent's identity in local Git config")
    ident.add_argument("agent_id")
    ident.add_argument("--role", required=True)
    commands.add_parser("whoami", help="Show the effective identity and role")
    create = commands.add_parser("create", help="Create a todo task with one or more scope locks")
    create.add_argument("title")
    create.add_argument("--description", default="", help="Work to perform")
    create.add_argument("--acceptance", default="", help="How completion will be verified")
    create.add_argument("--scope", action="append", required=True, help="Repository-relative file/directory or '*'; repeatable")
    create.add_argument("--depends-on", action="append", default=[], metavar="ID", help="Task that must be done before claim; repeatable")
    create.add_argument("--role", help="Optional exact role required to claim or take over")
    create.add_argument("--hypothesis", metavar="SLUG", help="Hypothesis the task works on: research/hypotheses/<slug>.md")
    listing = commands.add_parser("list", help="Fetch and display current tasks")
    listing.add_argument("--hypothesis", metavar="SLUG", help="Show only tasks linked to this hypothesis")
    show = commands.add_parser("show", help="Fetch one task, including history, as JSON")
    show.add_argument("id")
    for name, help_text in {
        "claim": "Claim a todo task if dependencies and scopes permit",
        "heartbeat": "Refresh ownership and optionally record a progress note",
        "block": "Record a blocker while retaining ownership and scopes",
        "resume": "Resume your blocked task",
        "handoff": "Mark your task ready for review; retain ownership and scopes",
        "done": "Complete your task and release its scope locks",
        "release": "Return your task to todo, releasing ownership and scopes",
        "takeover": "Take a stale active task, with a reason recorded in history",
    }.items():
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument("id")
        if name in {"block", "release", "takeover"}:
            command.add_argument("--reason", required=True)
        if name in {"handoff", "done"}:
            command.add_argument("--summary", required=True)
        if name == "handoff":
            command.add_argument("--pr", help="Pull request URL")
        if name == "heartbeat":
            command.add_argument("--note", help="Progress, discoveries or a handoff note")
        if name == "takeover":
            command.add_argument("--stale-after-minutes", type=int, default=STALE_MINUTES, help="Minimum heartbeat age; defaults to 30 and cannot be lower")
    return root


def main(argv: list[str] | None = None) -> int:
    # Task text is UTF-8; Windows pipes otherwise default to the ANSI codepage
    # (e.g. cp1251), which cannot encode characters such as "→".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        root = repo_root()
        if args.command == "identity":
            agent, role = agent_id(args.agent_id), role_name(args.role)
            git(root, "config", "--local", "coord.agent", agent)
            git(root, "config", "--local", "coord.role", role)
            print(f"Identity saved: {agent} (role: {role})")
            if "COORD_AGENT" in os.environ or "COORD_ROLE" in os.environ:
                print("Note: COORD_AGENT / COORD_ROLE environment variables override saved identity.")
            return 0
        if args.command == "whoami":
            agent, role = identity(root, required=False)
            print(f"Agent: {agent or '(not set)'}\nRole: {role or '(not set)'}")
            return 0
        actor = identity(root) if args.command not in {"init", "list", "show"} else None
        with remote(root) as tracker:
            if args.command == "init":
                print(tracker.update(lambda _: f"Initialized coordination branch '{BRANCH}'.", initialize=True))
            elif args.command in {"list", "show"}:
                _, state = tracker.read()
                if state is None:
                    raise CoordError("Tracker is not initialized. Run: python3 scripts/coord.py init")
                if args.command == "list":
                    print_list(state, hypothesis_slug(args.hypothesis) if args.hypothesis is not None else None)
                else:
                    print(json.dumps(get_task(state, args.id), ensure_ascii=False, indent=2))
            else:
                assert actor is not None and actor[0] is not None and actor[1] is not None
                operation_id = uuid.uuid4().hex
                print(tracker.update(lambda state: mutate_task(args, state, actor[0], actor[1], operation_id)))
        return 0
    except (CoordError, OSError) as exc:
        print(f"coord: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("coord: Interrupted. If this happened during push, inspect the board before retrying.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
