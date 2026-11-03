#!/usr/bin/env python3
"""audit_check.py: best-effort self-audit of token_tuner.py. See SECURITY.md.

1. Static scan: every import and call in token_tuner.py is checked against an allowlist
   (standard library only; no network, subprocess, eval/exec, or process-control calls).
2. Dynamic check: runs token_tuner in-process on SYNTHETIC data inside a temp directory, with
   Python audit hooks recording every socket/subprocess/exec event and every file it opens.
   It fails if anything touches the network, spawns a process, or opens a path outside the
   temp directory (other than the Python interpreter's own library files and this repo).

Run:  python3 -I audit_check.py        (exit code 0 = no findings)

This is a tripwire, not a proof. It cannot see a compromised Python install, a modified
copy of this file, or code paths the synthetic data does not exercise.
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import sysconfig
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "token_tuner.py"

ALLOWED_IMPORTS = {"argparse", "datetime", "difflib", "hashlib", "json", "os", "re", "shlex", "sys",
                   "time", "collections", "pathlib"}
DENY_MODULES = {"socket", "ssl", "http", "urllib", "ftplib", "smtplib", "telnetlib", "xmlrpc",
                "socketserver", "asyncio", "subprocess", "multiprocessing", "ctypes", "pickle",
                "marshal", "shelve", "webbrowser", "requests", "httpx", "aiohttp", "paramiko",
                "pty", "importlib", "imaplib", "poplib", "select", "selectors"}
DENY_CALL_NAMES = {"eval", "exec", "compile", "__import__"}
DENY_ATTR_CALLS = {("os", n) for n in (
    "system", "popen", "execv", "execve", "execvp", "execl", "execle", "execlp", "spawnv",
    "spawnl", "fork", "forkpty", "kill", "killpg", "chmod", "chown", "symlink", "link")}
FORBIDDEN_EVENT_PREFIXES = (
    "socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
    "os.forkpty", "os.kill", "ctypes.", "urllib.", "http.", "ftplib.", "smtplib.", "webbrowser.",
    "pty.", "os.remove", "os.rmdir", "os.rename", "os.symlink", "os.link", "shutil.")


# ---------------------------------------------------------------- 1. static scan

def static_scan():
    src = TARGET.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fails, notes = [], []
    imported = set()
    for n in ast.walk(tree):
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            mods = [(n.module or "").split(".")[0]]
        for m in mods:
            imported.add(m)
            if m not in ALLOWED_IMPORTS:
                fails.append(f"line {n.lineno}: import of '{m}' is not on the allowlist")
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id in DENY_CALL_NAMES:
                fails.append(f"line {n.lineno}: call to {f.id}()")
            if isinstance(f, ast.Attribute):
                if isinstance(f.value, ast.Name) and (f.value.id, f.attr) in DENY_ATTR_CALLS:
                    fails.append(f"line {n.lineno}: call to {f.value.id}.{f.attr}()")
                if f.attr in ("unlink", "rmdir"):
                    notes.append(f"line {n.lineno}: .{f.attr}() deletes a file "
                                 "(expected: rollback of a CLAUDE.md the tool itself created)")
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and re.search(r"(https?|ftp|wss?)://", n.value):
            notes.append(f"line {n.lineno}: string literal contains a URL")
    return sorted(imported), fails, notes


# ---------------------------------------------------------------- 2. dynamic check

EVENTS = []   # forbidden events
OPENS = []    # (path, is_write)
_WRITE_FLAGS = (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC)


def _hook(event, args):
    if event.startswith(FORBIDDEN_EVENT_PREFIXES):
        EVENTS.append((event, repr(args)[:100]))
    elif event == "import" and str(args[0]).split(".")[0] in DENY_MODULES:
        EVENTS.append((event, str(args[0])))
    elif event == "open":
        path, mode, flags = args[0], args[1], args[2]
        try:
            p = os.fspath(path)
        except TypeError:
            return
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if isinstance(p, int):
            return
        is_write = bool(isinstance(flags, int) and flags & _WRITE_FLAGS) or \
            (isinstance(mode, str) and any(c in mode for c in "wax+"))
        OPENS.append((os.path.realpath(p), is_write))


def make_fixture(root):
    proj = root / "proj"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# demo\n")
    sdir = root / "claude-projects" / "-demo"
    sdir.mkdir(parents=True)
    n = [0]

    def nid():
        n[0] += 1
        return f"id{n[0]}"

    def pair(tool, inp, chars):
        tid = nid()
        return [
            {"type": "assistant", "cwd": str(proj), "message": {
                "id": nid(), "usage": {"input_tokens": 1, "output_tokens": 1,
                                       "cache_creation_input_tokens": 1, "cache_read_input_tokens": 1},
                "content": [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": "x" * chars}]}},
        ]

    for i in range(3):
        es = [{"type": "user", "message": {"content": "always run the tests before committing"}}]
        for _ in range(4):
            es += pair("Read", {"file_path": "/p/a.py"}, 4000)
        for k in range(2):
            es += pair("Read", {"file_path": f"/p/big{i}{k}.py"}, 24000)
            es += pair("Bash", {"command": "ls"}, 16000)
            es += pair("Write", {"file_path": f"/p/big{i}{k}.py", "content": "y" * 3000}, 5)
        for _ in range(3):
            es += pair("Task", {"prompt": "x"}, 100)
        with (sdir / f"s{i}.jsonl").open("w") as fh:
            for e in es:
                fh.write(json.dumps(e) + "\n")
    # transcripts for the hook: one inside the projects dir, one outside it
    line = json.dumps({"type": "assistant", "timestamp": "2020-01-01T00:00:00Z", "cwd": str(proj),
                       "message": {"id": "hm", "usage": {"input_tokens": 1, "output_tokens": 1,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 300000},
                                   "content": [{"type": "text", "text": "ok"}]}}) + "\n"
    hook_t = sdir / "hooksess.jsonl"
    hook_t.write_text(line)
    outside = root / "outside.jsonl"
    outside.write_text(line)
    return proj, root / "claude-projects", hook_t, outside


def _lib_roots():
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}
    for k in ("stdlib", "platstdlib", "purelib", "platlib"):
        v = sysconfig.get_paths().get(k)
        if v:
            roots.add(v)
    return {os.path.realpath(r) for r in roots if r}


def _under(p, root):
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def dynamic_check():
    problems, summary = [], {}
    with tempfile.TemporaryDirectory(prefix="tt-audit-") as td:
        tmp = Path(td).resolve()
        proj, projects_dir, hook_t, outside = make_fixture(tmp)
        os.environ.update(HOME=str(tmp), CLAUDE_PROJECTS_DIR=str(projects_dir),
                          TOKEN_TUNER_DIR=str(tmp / "state"),
                          TOKEN_TUNER_GLOBAL_MD=str(tmp / "global.md"))
        sys.addaudithook(_hook)

        # self-check: prove the hook really records a forbidden event, then discard it
        sys.audit("socket.connect", None, ("203.0.113.1", 443))
        if not EVENTS:
            return ["audit hook self-check failed: forbidden event was not recorded"], {}
        EVENTS.clear()
        OPENS.clear()

        sys.dont_write_bytecode = True  # keep the interpreter's .pyc cache writes out of the audit
        spec = importlib.util.spec_from_file_location("token_tuner", TARGET)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for argv in (["report", "--include-repeated"],
                     ["apply", "--dry-run", "--include-repeated"],
                     ["apply", "--yes", "--include-repeated"],
                     ["status"],
                     ["rollback"]):
            with contextlib.redirect_stdout(io.StringIO()):
                mod.main(argv)
        # the nudge hook: a real transcript, then paths it must refuse to read
        real_stdin = sys.stdin
        try:
            for tp in (hook_t, outside, Path("/etc/passwd")):
                sys.stdin = io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit",
                                                    "session_id": "audit", "transcript_path": str(tp)}))
                with contextlib.redirect_stdout(io.StringIO()):
                    mod.main(["hook"])
        finally:
            sys.stdin = real_stdin

        opened = {p for p, _ in OPENS}
        if os.path.realpath(str(hook_t)) not in opened:
            problems.append("hook did not read the in-tree transcript (audit scenario not exercised)")
        for bad in (outside, Path("/etc/passwd")):
            if os.path.realpath(str(bad)) in opened:
                problems.append(f"hook opened a transcript outside the Claude projects dir: {bad}")

        libs = _lib_roots()
        here = os.path.realpath(str(HERE))
        tmp_s = os.path.realpath(str(tmp))
        reads_in = writes_in = 0
        for p, is_write in OPENS:
            if _under(p, tmp_s):
                writes_in += is_write
                reads_in += not is_write
            elif is_write:
                if not _under(p, os.path.join(here, "__pycache__")):
                    problems.append(f"WROTE outside temp dir: {p}")
            elif not (any(_under(p, r) for r in libs) or _under(p, here)
                      or p in ("/dev/null", "/dev/urandom", "/dev/random")):
                problems.append(f"READ outside temp dir: {p}")
        for ev, a in EVENTS:
            problems.append(f"forbidden event: {ev} {a}")
        summary = dict(files_read_in_tmp=reads_in, files_written_in_tmp=writes_in,
                       forbidden_events=len(EVENTS))
    return problems, summary


def main():
    print(f"auditing {TARGET}\n")
    imported, fails, notes = static_scan()
    print(f"[static] imports: {', '.join(imported)}")
    for n in notes:
        print(f"[static] note: {n}")
    for f in fails:
        print(f"[static] FAIL: {f}")
    problems, summary = dynamic_check()
    print(f"[dynamic] {summary}")
    for p in problems:
        print(f"[dynamic] FAIL: {p}")
    ok = not fails and not problems
    print("\nRESULT:", "PASS: no findings" if ok else "FAIL: see above")
    print("Reminder: a passing result is a tripwire, not a guarantee. See SECURITY.md.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
