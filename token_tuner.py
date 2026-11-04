#!/usr/bin/env python3
"""token-tuner: learn from your Claude Code sessions and keep CLAUDE.md lean-making.

Reads local transcripts (~/.claude/projects/*/*.jsonl), finds patterns that waste
tokens, and writes a small managed block of rules into CLAUDE.md.

Safe by default: `report` and `apply --dry-run` change nothing. `apply` shows a
diff and asks first, backs up the file, and can be undone with `rollback`.
Nothing leaves your machine; there are no network calls and no dependencies.
"""
import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shlex
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
PROJECTS_DIR = Path(os.environ.get("CLAUDE_PROJECTS_DIR", HOME / ".claude" / "projects"))
STATE_DIR = Path(os.environ.get("TOKEN_TUNER_DIR", HOME / ".claude" / "token-tuner"))
GLOBAL_CLAUDE_MD = Path(os.environ.get("TOKEN_TUNER_GLOBAL_MD", HOME / ".claude" / "CLAUDE.md"))

START = "<!-- token-tuner:start (managed block; use `token_tuner.py rollback` to undo) -->"
END = "<!-- token-tuner:end -->"
HEADER = "## Token efficiency"
MAX_RULES = 8  # CLAUDE.md is re-read every turn, so keep the managed block small

# id -> thresholds + the rule text that gets written to CLAUDE.md
RULE_DEFS = {
    "reread": dict(
        title="Re-reading unchanged files",
        text="Don't re-read a file you already read this session unless it was edited since; reuse what is already in context.",
        min_sessions=2, min_count=3, min_waste=3000,
    ),
    "bigread": dict(
        title="Whole-file reads of large files",
        text="For large files, locate the part you need with Grep first, then Read with offset/limit instead of reading the whole file.",
        min_sessions=2, min_count=3, min_waste=5000,
    ),
    "bigbash": dict(
        title="Very long shell output",
        text="Keep command output short: pipe long Bash output through head, tail, or grep (for example `| tail -50`).",
        min_sessions=2, min_count=3, min_waste=5000,
    ),
    "write": dict(
        title="Rewriting existing files with Write",
        text="To change an existing file, use Edit (a small diff) rather than rewriting the whole file with Write.",
        min_sessions=2, min_count=3, min_waste=1500,
    ),
    "agent": dict(
        title="Frequent subagent spawns",
        text="For simple lookups use Grep or Glob directly; spawn a subagent only for broad, multi-step searches.",
        min_sessions=2, min_count=8, min_waste=0,
    ),
}

BIG_READ_TOKENS = 4000     # a full Read result above this counts as "large"
BIG_BASH_TOKENS = 2500     # a Bash result above this counts as "long"
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Write"}
AGENT_TOOLS = {"Task", "Agent"}
REPEAT_MIN_SESSIONS = 3    # an instruction repeated in this many sessions is a CLAUDE.md candidate

# context-size reporting and the opt-in nudge hook (all heuristics; tune with the hook flags)
CTX_WARN = 250_000         # context tokens at which a turn/session counts as "large"
CTX_URGENT = 500_000
LONG_SESSION_TURNS = 200
HOOK_CACHE_TTL_MIN = 60    # idle minutes after which the prompt cache has probably expired
HOOK_CACHE_MIN_CTX = 100_000
HOOK_STREAK = 8            # consecutive read-only tool calls that trigger the "use a subagent" nudge
HOOK_REPEAT = 3            # same shell command this many times in one turn = "retry loop" nudge
TAIL_BYTES = 2_000_000     # the hook only reads this much from the end of a transcript
EXPLORE_TOOLS = {"Read", "Grep", "Glob"}


# ---------------------------------------------------------------- parsing

def est_tokens(chars):
    return max(1, int(chars) // 4)


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    parts.append(text_of(b.get("content", "")))
        return "\n".join(parts)
    return ""


def load_entries(path):
    entries = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return entries


def iter_session_files(days):
    cutoff = time.time() - days * 86400
    if not PROJECTS_DIR.is_dir():
        return
    for f in sorted(PROJECTS_DIR.glob("*/*.jsonl")):
        try:
            if f.stat().st_mtime >= cutoff:
                yield f
        except OSError:
            continue


def normalize_instruction(txt):
    t = re.sub(r"\s+", " ", txt.strip().lower())
    return t


def analyze_session(entries):
    s = {
        "cwd": None, "turns": 0, "usage": Counter(),
        "reread": [], "bigread": [], "bigbash": [], "write": [], "agent": [],
        "user_msgs": set(), "ctx": [], "models": Counter(), "id": "",
    }
    pending = {}          # tool_use_id -> (name, input)
    reads_seen = {}       # path -> set of (offset, limit) read since last edit
    ever_read = set()
    seen_msg_ids = set()

    for e in entries:
        if s["cwd"] is None and e.get("cwd"):
            s["cwd"] = e["cwd"]
        kind = e.get("type")
        msg = e.get("message") or {}
        content = msg.get("content")

        if kind == "assistant":
            mid = msg.get("id")
            if mid and mid not in seen_msg_ids:
                seen_msg_ids.add(mid)
                u = msg.get("usage") or {}
                for k in ("input_tokens", "output_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                    s["usage"][k] += int(u.get(k) or 0)
                s["turns"] += 1
                s["ctx"].append(sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")))
                if msg.get("model"):
                    s["models"][str(msg["model"])] += 1
            for b in content if isinstance(content, list) else []:
                if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                pending[b.get("id")] = (name, inp)
                path = inp.get("file_path") or inp.get("notebook_path")
                if name in AGENT_TOOLS:
                    s["agent"].append(0)
                if name in EDIT_TOOLS and path:
                    if name == "Write" and path in ever_read:
                        body = inp.get("content") or ""
                        s["write"].append(max(0, est_tokens(len(body)) - 200))
                    reads_seen.pop(path, None)

        elif kind == "user":
            blocks = content if isinstance(content, list) else []
            has_result = False
            for b in blocks:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                has_result = True
                name, inp = pending.get(b.get("tool_use_id"), (None, {}))
                size = est_tokens(len(text_of(b.get("content", ""))))
                if name == "Read":
                    path = inp.get("file_path")
                    key = (inp.get("offset"), inp.get("limit"))
                    if path:
                        if key in reads_seen.get(path, set()):
                            s["reread"].append(size)
                        else:
                            reads_seen.setdefault(path, set()).add(key)
                        ever_read.add(path)
                        if key == (None, None) and size > BIG_READ_TOKENS:
                            s["bigread"].append(size - 1500)
                elif name == "Bash" and size > BIG_BASH_TOKENS:
                    s["bigbash"].append(size - 500)
            if not has_result and not e.get("isMeta"):
                txt = text_of(content).strip()
                if 20 <= len(txt) <= 200 and not txt.startswith(("/", "<", "[", "!")) and "\n" not in txt:
                    s["user_msgs"].add(normalize_instruction(txt))
    return s


def collect(days, project_filter, global_mode):
    groups = defaultdict(list)
    for f in iter_session_files(days):
        s = analyze_session(load_entries(f))
        if not s["turns"]:
            continue
        label = s["cwd"] or f.parent.name
        if project_filter and project_filter not in str(label):
            continue
        s["id"] = f.stem
        groups["ALL PROJECTS" if global_mode else label].append(s)
    return groups


# ---------------------------------------------------------------- findings

def rule_cost_tokens(text):
    return est_tokens(len(text) + 4)


def build_findings(sessions, include_repeated):
    findings = []
    for rid, d in RULE_DEFS.items():
        affected = sum(1 for s in sessions if s[rid])
        count = sum(len(s[rid]) for s in sessions)
        waste = sum(sum(s[rid]) for s in sessions)
        ok = affected >= d["min_sessions"] and count >= d["min_count"] and waste >= d["min_waste"]
        findings.append(dict(
            id=rid, kind="token", title=d["title"], text=d["text"], sessions=affected,
            count=count, waste=waste, trigger=ok, cost=rule_cost_tokens(d["text"]),
        ))
    # instructions the user keeps repeating across sessions
    seen = Counter()
    for s in sessions:
        for m in s["user_msgs"]:
            seen[m] += 1
    for msg, n in seen.most_common(10):
        if n >= REPEAT_MIN_SESSIONS:
            rid = "rep-" + hashlib.sha1(msg.encode()).hexdigest()[:6]
            text = msg[0].upper() + msg[1:]
            findings.append(dict(
                id=rid, kind="repeat", title="Instruction repeated across sessions", text=text,
                sessions=n, count=n, waste=0, trigger=include_repeated,
                cost=rule_cost_tokens(text),
            ))
    return findings


# ---------------------------------------------------------------- state / files

def load_state():
    p = STATE_DIR / "state.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            pass
    return {"files": {}, "backups": []}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "state.json").write_text(json.dumps(state, indent=2))


def target_for(key, global_mode):
    if global_mode:
        return GLOBAL_CLAUDE_MD
    p = Path(key)
    return p / "CLAUDE.md" if p.is_dir() else None


def render_block(rules):
    lines = [START, HEADER] + [f"- {t}" for t in rules.values()] + [END]
    return "\n".join(lines)


def splice(existing, block):
    pat = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if pat.search(existing):
        return pat.sub(lambda _m: block, existing)
    sep = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
    if existing and not existing.endswith("\n\n") and sep == "\n":
        sep = "\n"
    return existing + sep + block + "\n"


def merged_rules(state, target, new_findings):
    """Applied rules persist (a working rule makes its own evidence vanish); new ones append up to the cap."""
    rules = dict(state["files"].get(str(target), {}).get("rules", {}))
    added = []
    for f in new_findings:
        if f["id"] in rules or len(rules) >= MAX_RULES:
            continue
        rules[f["id"]] = f["text"]
        added.append(f["id"])
    return rules, added


# ---------------------------------------------------------------- commands

def fmt(n):
    return f"{n/1000:.1f}K" if n >= 1000 else str(n)


def print_context_section(sessions):
    """Context size is often the biggest cost driver: every turn re-reads the whole context."""
    all_ctx = [c for s in sessions for c in s["ctx"]]
    if not all_ctx:
        return
    models = Counter()
    for s in sessions:
        models.update(s["models"])
    mix = ", ".join(f"{m} x{n}" for m, n in models.most_common(4)) or "unknown"
    print(f"context per turn: avg {fmt(sum(all_ctx) // len(all_ctx))}, peak {fmt(max(all_ctx))}   "
          f"models (turns): {mix}")
    flagged = []
    for s in sessions:
        if not s["ctx"]:
            continue
        avg = sum(s["ctx"]) // len(s["ctx"])
        if avg >= CTX_WARN or s["turns"] >= LONG_SESSION_TURNS:
            flagged.append((avg * s["turns"], s, avg))
    for _, s, avg in sorted(flagged, key=lambda t: -t[0])[:5]:
        print(f"  large/long session {s['id'][:8]}: {s['turns']} turns, avg context {fmt(avg)}, "
              f"peak {fmt(max(s['ctx']))}")
    if flagged:
        print("  -> every turn re-reads the whole context. /compact earlier, or start a fresh session"
              " when the task changes.")


def cmd_report(args):
    groups = collect(args.days, args.project, args.global_mode)
    if not groups:
        print(f"No sessions found under {PROJECTS_DIR} in the last {args.days} days.")
        return 1
    for key, sessions in groups.items():
        u = Counter()
        for s in sessions:
            u.update(s["usage"])
        print(f"\n== {key} ==")
        print(f"sessions: {len(sessions)}   turns: {sum(s['turns'] for s in sessions)}   "
              f"in: {fmt(u['input_tokens'])}  out: {fmt(u['output_tokens'])}  "
              f"cache-write: {fmt(u['cache_creation_input_tokens'])}  cache-read: {fmt(u['cache_read_input_tokens'])}")
        print(f"{'pattern':36} {'sessions':>8} {'count':>6} {'est. waste':>11}  rule cost/turn  suggest")
        for f in build_findings(sessions, args.include_repeated):
            if f["kind"] == "repeat":
                print(f"  repeated instruction ({f['sessions']} sessions): {f['text']!r}")
                continue
            waste = fmt(f["waste"]) if f["waste"] else "n/a"
            print(f"{f['title']:36} {f['sessions']:>8} {f['count']:>6} {waste:>11}  "
                  f"{'~'+str(f['cost'])+' tok':>14}  {'YES' if f['trigger'] else '-'}")
        print_context_section(sessions)
    print("\nWaste figures are rough estimates (chars/4). A rule only helps if Claude follows it;"
          " re-run later to see whether the pattern shrinks.")
    return 0


def cmd_apply(args):
    groups = collect(args.days, args.project, args.global_mode)
    if not groups:
        print(f"No sessions found under {PROJECTS_DIR} in the last {args.days} days.")
        return 1
    state = load_state()
    changed = 0
    for key, sessions in groups.items():
        findings = [f for f in build_findings(sessions, args.include_repeated) if f["trigger"]]
        target = target_for(key, args.global_mode)
        if target is None:
            print(f"skip {key}: project directory not found on this machine")
            continue
        rules, added = merged_rules(state, target, findings)
        if not added:
            print(f"{target}: nothing new to add")
            continue
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        new_text = splice(existing, render_block(rules))
        diff = "".join(difflib.unified_diff(
            existing.splitlines(True), new_text.splitlines(True),
            fromfile=f"{target} (current)", tofile=f"{target} (proposed)"))
        print(f"\n{diff}")
        if args.dry_run:
            print("(dry run: nothing written)")
            continue
        if not args.yes:
            if not sys.stdin.isatty():
                print("Not a terminal; re-run with --yes to apply.")
                continue
            if input(f"Apply to {target}? [y/N] ").strip().lower() != "y":
                print("skipped")
                continue
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        backup = None
        if target.exists():
            bdir = STATE_DIR / "backups"
            bdir.mkdir(exist_ok=True)
            backup = str(bdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{hashlib.sha1(str(target).encode()).hexdigest()[:8]}.md")
            Path(backup).write_text(existing, encoding="utf-8")
        state["backups"].append(dict(
            path=str(target), backup=backup, time=time.time(),
            rules_before=state["files"].get(str(target), {}).get("rules", {})))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_text, encoding="utf-8")
        state["files"][str(target)] = {"rules": rules}
        save_state(state)
        changed += 1
        print(f"updated {target} (+{len(added)} rule(s); backup: {backup or 'none, file was new'})")
    return 0


def cmd_rollback(args):
    state = load_state()
    cands = [b for b in state["backups"] if not args.file or b["path"] == str(Path(args.file).resolve())]
    if not cands:
        print("Nothing to roll back.")
        return 1
    b = cands[-1]
    target = Path(b["path"])
    if b["backup"]:
        target.write_text(Path(b["backup"]).read_text(encoding="utf-8"), encoding="utf-8")
    elif target.exists():
        target.unlink()
    state["backups"].remove(b)
    if b["rules_before"]:
        state["files"][b["path"]] = {"rules": b["rules_before"]}
    else:
        state["files"].pop(b["path"], None)
    save_state(state)
    print(f"restored {target}")
    return 0


def cmd_status(_args):
    state = load_state()
    if not state["files"]:
        print("No rules applied yet.")
        return 0
    for path, d in state["files"].items():
        print(path)
        for rid, text in d["rules"].items():
            print(f"  [{rid}] {text}")
    return 0


# ---------------------------------------------------------------- nudge hook (opt-in)
# Runs as a Claude Code UserPromptSubmit hook. It only READS the current transcript's tail and
# prints a short note (numbers only, never transcript text). It never edits settings or CLAUDE.md.

def _is_real_user_prompt(e):
    if e.get("type") != "user" or e.get("isMeta"):
        return False
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        kinds = {b.get("type") for b in c if isinstance(b, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def read_tail_entries(path):
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(size - TAIL_BYTES)
            fh.readline()  # drop the partial first line
        data = fh.read().decode("utf-8", errors="replace")
    out = []
    for line in data.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def last_turn_tools(entries):
    """(tool calls of the most recent turn that used tools, oldest first; key of that turn's prompt)."""
    tools = []
    for e in reversed(entries):
        if _is_real_user_prompt(e):
            if tools:
                tools.reverse()
                return tools, (e.get("uuid") or e.get("timestamp") or "turn")
            continue
        if e.get("type") == "assistant":
            blocks = (e.get("message") or {}).get("content")
            for b in reversed(blocks if isinstance(blocks, list) else []):
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools.append((b.get("name"), (b.get("input") or {}).get("command")))
    return [], None


def compute_nudges(entries, cfg, now, st):
    """Return (messages, new_state, big). Messages contain numbers only. `st` is this session's saved state.
    `big` is None, or {"ctx": tokens, "idle": minutes or None} when the next turn would be expensive:
    the context just crossed the urgent level, or the prompt cache has probably expired."""
    msgs, new, big = [], dict(st), None
    ctx = 0
    for e in reversed(entries):
        u = (e.get("message") or {}).get("usage") if e.get("type") == "assistant" else None
        if u:
            ctx = sum(int(u.get(k) or 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            break

    # 1. large context (escalates once per level; resets after the context shrinks)
    level = 2 if ctx >= cfg["ctx_urgent"] else 1 if ctx >= cfg["ctx_warn"] else 0
    if level > st.get("ctx_level", 0):
        if level == 2:
            big = {"ctx": ctx, "idle": None}
            msgs.append(f"The context is about {fmt(ctx)} tokens and is re-read on every turn. "
                        "Recommend /compact or a fresh session now.")
        else:
            msgs.append(f"The context is about {fmt(ctx)} tokens and is re-read on every turn. "
                        "If the user is switching tasks suggest /clear, otherwise suggest /compact soon.")
    new["ctx_level"] = level

    # 2. idle gap: the prompt cache has probably expired
    ts_raw = None
    for e in reversed(entries):
        if e.get("timestamp"):
            ts_raw = str(e["timestamp"])
            break
    if ts_raw:
        try:
            ts = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            idle = (now - ts).total_seconds() / 60
            if ctx >= cfg["cache_min_ctx"] and idle >= cfg["cache_ttl_min"] and st.get("cache_ts") != ts_raw:
                msgs.append(f"The user was away about {int(idle)} min, so the prompt cache has probably "
                            f"expired and the next turn may re-process the whole ~{fmt(ctx)} context at "
                            "full price. Suggest /compact or /clear first if the old context isn't needed.")
                new["cache_ts"] = ts_raw
                big = {"ctx": ctx, "idle": int(idle)}
        except ValueError:
            pass

    # 3. last turn: long read-only streak, or the same shell command repeated
    tools, key = last_turn_tools(entries)
    if key is not None and key != st.get("turn_key"):
        run = 0
        for name, _ in reversed(tools):
            if name in EXPLORE_TOOLS:
                run += 1
            else:
                break
        if run >= cfg["streak"]:
            msgs.append(f"The last turn made {run} read-only calls (Read/Grep/Glob) in a row in the main "
                        "context. For broad searches, suggest delegating to a subagent next time.")
        worst = max(Counter(c for n, c in tools if n == "Bash" and c).values(), default=0)
        if worst >= cfg["repeat"]:
            msgs.append(f"The last turn ran the same shell command {worst} times. "
                        "Ask the user whether to change approach instead of retrying.")
        new["turn_key"] = key
    return msgs, new, big


def block_reason(big):
    """Text shown to the user (not Claude) when --block holds an expensive prompt."""
    ctx = fmt(big["ctx"])
    if big["idle"] is not None:
        m = big["idle"]
        away = f"{m} min" if m < 120 else f"{m // 60} h" if m < 2880 else f"{m // 1440} days"
        why = (f"You were away about {away}, so the prompt cache has probably expired. "
               f"Sending now re-processes the whole ~{ctx} context at full price.")
    else:
        why = f"The context is about {ctx} tokens, and every message re-reads all of it."
    return ("[token-tuner] Held this message before it was sent. " + why + "\n"
            "Cheapest first:\n"
            "  1. /clear  - drop the old context if you don't need it (almost free). You can point the new\n"
            "     context at the files or notes you need instead of carrying the whole history.\n"
            f"  2. /compact <what to keep>  - one pass over the ~{ctx} now, then every later turn starts small.\n"
            f"  3. Send your message again to go ahead as-is (~{ctx} now and on every later turn).\n"
            "Your message was not sent; type or paste it again after choosing.")


def load_nudge_state():
    try:
        d = json.loads((STATE_DIR / "nudges.json").read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_nudge_state(d):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if len(d) > 50:
        d = dict(list(d.items())[-50:])
    (STATE_DIR / "nudges.json").write_text(json.dumps(d))


def cmd_hook(args):
    """Never fails the user's prompt: any problem means silence and exit code 0."""
    try:
        sid = None
        if args.transcript:
            tp = args.transcript
        else:
            data = json.loads(sys.stdin.read() or "{}")
            if data.get("hook_event_name") not in (None, "UserPromptSubmit"):
                return 0
            tp, sid = data.get("transcript_path"), data.get("session_id")
        if not tp:
            return 0
        path = Path(tp).expanduser().resolve()
        # only ever read Claude Code's own transcript files
        if path.suffix != ".jsonl" or PROJECTS_DIR.resolve() not in path.parents or not path.is_file():
            return 0
        if not (isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", sid)):
            sid = path.stem
        cfg = dict(ctx_warn=args.ctx_warn, ctx_urgent=args.ctx_urgent, cache_ttl_min=args.cache_ttl_min,
                   cache_min_ctx=HOOK_CACHE_MIN_CTX, streak=args.streak, repeat=args.repeat)
        state = load_nudge_state()
        msgs, new, big = compute_nudges(read_tail_entries(path), cfg,
                                        datetime.datetime.now(datetime.timezone.utc), state.get(sid, {}))
        hold = bool(args.block and big)
        if args.dry_run:
            if hold:
                print("(would block this prompt)\n" + block_reason(big))
            else:
                print("\n".join(msgs) if msgs else "(no nudge right now)")
            return 0
        if new != state.get(sid, {}):
            state.pop(sid, None)
            state[sid] = new
            save_nudge_state(state)
        # the state above is saved first, so the same prompt sent again goes through
        if hold:
            print(json.dumps({"decision": "block", "reason": block_reason(big)}))
        elif msgs:
            note = ("[token-tuner] Before answering, tell the user in one short sentence: "
                    + " ".join(msgs) + " Then continue with their request.")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                     "additionalContext": note}}))
    except Exception:
        pass
    return 0


def cmd_hook_config(_args):
    cmd = f"python3 {shlex.quote(str(Path(__file__).resolve()))} hook"
    snippet = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": cmd}]}]}}
    print(json.dumps(snippet, indent=2))
    print('\nMerge the "hooks" entry into ~/.claude/settings.json yourself; this tool never edits it.\n'
          "Try it first without installing:\n"
          "  python3 token_tuner.py hook --dry-run --transcript <a .jsonl under ~/.claude/projects>"
          "\nAdd --block to the command to hold an expensive prompt once and show cheaper options.")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="token_tuner", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("report", cmd_report), ("apply", cmd_apply)):
        sp = sub.add_parser(name)
        sp.add_argument("--days", type=int, default=30)
        sp.add_argument("--project", help="only projects whose path contains this text")
        sp.add_argument("--global", dest="global_mode", action="store_true",
                        help="aggregate all projects and target ~/.claude/CLAUDE.md")
        sp.add_argument("--include-repeated", action="store_true",
                        help="also propose instructions you repeat across sessions")
        if name == "apply":
            sp.add_argument("--dry-run", action="store_true")
            sp.add_argument("--yes", action="store_true", help="skip the confirmation prompt (for cron/hooks)")
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("rollback")
    sp.add_argument("--file", help="CLAUDE.md path to restore (default: most recent change)")
    sp.set_defaults(fn=cmd_rollback)
    sp = sub.add_parser("status")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("hook", help="UserPromptSubmit nudge hook (reads JSON on stdin)")
    sp.add_argument("--transcript", help="test against this transcript instead of reading stdin")
    sp.add_argument("--dry-run", action="store_true", help="print what it would say; save nothing")
    sp.add_argument("--ctx-warn", type=int, default=CTX_WARN)
    sp.add_argument("--ctx-urgent", type=int, default=CTX_URGENT)
    sp.add_argument("--cache-ttl-min", type=int, default=HOOK_CACHE_TTL_MIN)
    sp.add_argument("--streak", type=int, default=HOOK_STREAK)
    sp.add_argument("--repeat", type=int, default=HOOK_REPEAT)
    sp.add_argument("--block", action="store_true",
                    help="hold an expensive prompt once (cold cache or urgent context) and show options")
    sp.set_defaults(fn=cmd_hook)
    sp = sub.add_parser("hook-config", help="print the settings.json snippet that installs the hook")
    sp.set_defaults(fn=cmd_hook_config)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
