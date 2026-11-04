"""Synthetic end-to-end test for token_tuner. Run: python3 test_token_tuner.py"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "token_tuner.py"

tmp = Path(tempfile.mkdtemp(prefix="tt-test-"))
proj = tmp / "myproj"
proj.mkdir()
(proj / "CLAUDE.md").write_text("# My project\n\nBe concise.\n")
projects_dir = tmp / "claude-projects"
sess_dir = projects_dir / "-tmp-myproj"
sess_dir.mkdir(parents=True)

env = dict(os.environ,
           CLAUDE_PROJECTS_DIR=str(projects_dir),
           TOKEN_TUNER_DIR=str(tmp / "state"),
           TOKEN_TUNER_GLOBAL_MD=str(tmp / "global-CLAUDE.md"))

_id = [0]


def nid():
    _id[0] += 1
    return f"id{_id[0]}"


def assistant(tool=None, inp=None, cwd=str(proj)):
    mid, tid = nid(), nid()
    content = [{"type": "tool_use", "id": tid, "name": tool, "input": inp or {}}] if tool else [{"type": "text", "text": "ok"}]
    e = {"type": "assistant", "cwd": cwd,
         "message": {"id": mid, "usage": {"input_tokens": 100, "output_tokens": 50,
                                           "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 20000},
                     "content": content}}
    return e, tid


def result(tid, chars):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "x" * chars}]}}


def user(text):
    return {"type": "user", "message": {"content": text}}


def tool_pair(tool, inp, chars):
    a, tid = assistant(tool, inp)
    return [a, result(tid, chars)]


def make_session(n, with_instruction=True):
    es = []
    if with_instruction:
        es.append(user("always run the tests before committing changes"))
    # 1 legit read + 3 redundant re-reads of the same unchanged file (4000 chars = 1000 tokens each)
    for _ in range(4):
        es += tool_pair("Read", {"file_path": "/p/a.py"}, 4000)
    # a re-read after an edit is NOT redundant
    es += tool_pair("Read", {"file_path": "/p/b.py"}, 4000)
    es += tool_pair("Edit", {"file_path": "/p/b.py"}, 10)
    es += tool_pair("Read", {"file_path": "/p/b.py"}, 4000)
    # large whole-file reads (6000 tokens each)
    es += tool_pair("Read", {"file_path": f"/p/big{n}a.py"}, 24000)
    es += tool_pair("Read", {"file_path": f"/p/big{n}b.py"}, 24000)
    # a large read WITH offset/limit is fine
    es += tool_pair("Read", {"file_path": "/p/c.py", "offset": 10, "limit": 100}, 24000)
    # long shell output (4000 tokens each)
    es += tool_pair("Bash", {"command": "pytest -v"}, 16000)
    es += tool_pair("Bash", {"command": "git log"}, 16000)
    # rewrite of an already-read file with Write (3000 chars -> ~750 tokens)
    es += tool_pair("Write", {"file_path": "/p/a.py", "content": "y" * 3000}, 10)
    es += tool_pair("Write", {"file_path": "/p/big%da.py" % n, "content": "y" * 3000}, 10)
    # subagent spawns
    for _ in range(3):
        es += tool_pair("Task", {"prompt": "find stuff"}, 200)
    return es


for n in range(1, 4):
    with (sess_dir / f"s{n}.jsonl").open("w") as fh:
        for e in make_session(n):
            fh.write(json.dumps(e) + "\n")

# ---- unit-level assertions
sys.path.insert(0, str(HERE))
os.environ.update({k: env[k] for k in ("CLAUDE_PROJECTS_DIR", "TOKEN_TUNER_DIR", "TOKEN_TUNER_GLOBAL_MD")})
import token_tuner as tt  # noqa: E402

s = tt.analyze_session(tt.load_entries(sess_dir / "s1.jsonl"))
assert len(s["reread"]) == 3, s["reread"]          # only unchanged re-reads
assert all(w == 1000 for w in s["reread"])
assert len(s["bigread"]) == 2, s["bigread"]        # offset/limit read excluded
assert len(s["bigbash"]) == 2
assert len(s["write"]) == 2
assert len(s["agent"]) == 3
assert s["turns"] > 10 and s["usage"]["output_tokens"] == 50 * s["turns"]
assert "always run the tests before committing changes" in s["user_msgs"]
print("unit assertions ok")


def run(*args, **kw):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, **kw)
    return r.returncode, r.stdout + r.stderr


rc, out = run("report", "--include-repeated")
print(out)
assert rc == 0 and "YES" in out and "repeated instruction" in out

before = (proj / "CLAUDE.md").read_text()
rc, out = run("apply", "--dry-run", "--include-repeated")
assert (proj / "CLAUDE.md").read_text() == before, "dry run must not write"
assert "(dry run: nothing written)" in out
print("dry run ok")

rc, out = run("apply", "--yes", "--include-repeated")
print(out)
after = (proj / "CLAUDE.md").read_text()
assert after.startswith(before) and "token-tuner:start" in after and "Be concise." in after
assert "Always run the tests" in after and "Grep" in after

rc, out = run("apply", "--yes", "--include-repeated")
assert "nothing new to add" in out, out
assert (proj / "CLAUDE.md").read_text() == after, "second run must be idempotent"
print("idempotent ok")

rc, out = run("status")
assert "[reread]" in out
rc, out = run("rollback")
assert (proj / "CLAUDE.md").read_text() == before, "rollback must restore original"
print("rollback ok")

rc, out = run("apply", "--yes")   # without --include-repeated: no user-instruction rule
assert "Always run the tests" not in (proj / "CLAUDE.md").read_text()
# ---- context-size section in the report
rc, out = run("report")
assert "context per turn: avg" in out and "models (turns):" in out, out
print("report context section ok")

# ---- nudge hook
import datetime as _dt  # noqa: E402


def iso(minutes_ago=0):
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return t.isoformat().replace("+00:00", "Z")


def hook_transcript(name, ctx, idle_min=0, explore=0, bash_repeat=0, folder=None):
    es = [{"type": "user", "uuid": f"{name}-p1", "timestamp": iso(idle_min + 5),
           "message": {"content": "please do the thing"}}]
    for i in range(explore):
        a, tid = assistant("Read", {"file_path": f"/secret/path{i}.py"})
        a["timestamp"] = iso(idle_min)
        es += [a, result(tid, 100)]
    for _ in range(bash_repeat):
        a, tid = assistant("Bash", {"command": "make secret-target"})
        a["timestamp"] = iso(idle_min)
        es += [a, result(tid, 100)]
    last, _tid = assistant()
    last["timestamp"] = iso(idle_min)
    last["message"]["usage"] = {"input_tokens": 10, "output_tokens": 5,
                                "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx}
    es.append(last)
    path = (folder or sess_dir) / f"{name}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in es))
    return path


def run_hook(path, sid=None, stdin=None, *extra):
    payload = stdin if stdin is not None else json.dumps(
        {"hook_event_name": "UserPromptSubmit", "session_id": sid or path.stem, "transcript_path": str(path)})
    r = subprocess.run([sys.executable, str(SCRIPT), "hook", *extra], input=payload, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def note(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


# 1-3: context level escalates once per level, then goes quiet
p = hook_transcript("ctxA", 300_000)
n = note(run_hook(p))
assert "re-read on every turn" in n and "/clear" in n and "Recommend /compact" not in n, n
assert run_hook(p) == "", "same level must not repeat"
p = hook_transcript("ctxA", 600_000)
assert "Recommend /compact" in note(run_hook(p)), "urgent level should fire once"

# 4: small context stays silent
assert run_hook(hook_transcript("small", 50_000)) == ""

# 5: long idle gap with a big-enough context -> cache note
n = note(run_hook(hook_transcript("idle", 150_000, idle_min=120)))
assert "prompt cache" in n and "about 12" in n, n

# 6-7: read-only streak and repeated shell command, numbers only (no paths/commands leaked)
out = run_hook(hook_transcript("streak", 50_000, explore=9))
assert "9 read-only calls" in note(out) and "/secret/path" not in out
out = run_hook(hook_transcript("retry", 50_000, bash_repeat=4))
assert "same shell command 4 times" in note(out) and "secret-target" not in out

# 8: transcript outside the Claude projects dir is never read
outside = tmp / "outside"
outside.mkdir()
assert run_hook(hook_transcript("evil", 900_000, folder=outside)) == ""

# 9: garbage / empty / wrong-event input -> silent, exit code 0
assert run_hook(p, stdin="not json") == ""
assert run_hook(p, stdin="") == ""
assert run_hook(p, stdin=json.dumps({"hook_event_name": "Stop", "transcript_path": str(p)})) == ""

# 10: --dry-run prints but saves nothing
p = hook_transcript("dry", 700_000)
printed = subprocess.run([sys.executable, str(SCRIPT), "hook", "--dry-run", "--transcript", str(p)],
                         env=env, capture_output=True, text=True).stdout
assert "Recommend /compact" in printed
assert "dry" not in json.loads((tmp / "state" / "nudges.json").read_text())

# 10b: --block holds an expensive prompt once, then the resend goes through
def blocked(out):
    d = json.loads(out)
    assert d["decision"] == "block", d
    return d["reason"]

p = hook_transcript("coldblk", 400_000, idle_min=120)
r = blocked(run_hook(p, None, None, "--block"))
assert "/clear" in r and "/compact" in r and "Send your message again" in r and "about 2 h" in r, r
assert run_hook(p, None, None, "--block") == "", "resend after a hold must go through"
p = hook_transcript("bigblk", 600_000)
assert "about 600.0K" in blocked(run_hook(p, None, None, "--block"))
assert run_hook(p, None, None, "--block") == ""
# warn level, small context, and the default (no --block) never hold a prompt
assert "decision" not in run_hook(hook_transcript("warnblk", 300_000), None, None, "--block")
assert run_hook(hook_transcript("smallblk", 50_000, idle_min=120), None, None, "--block") == ""
assert "decision" not in run_hook(hook_transcript("noblk", 600_000))
printed = subprocess.run([sys.executable, str(SCRIPT), "hook", "--dry-run", "--block", "--transcript",
                          str(hook_transcript("dryblk", 700_000))], env=env, capture_output=True, text=True).stdout
assert "would block" in printed

# 11: hook-config prints a valid settings snippet and does not touch settings files
rc, out = run("hook-config")
snippet = json.loads(out.split("\n\nMerge")[0])
assert snippet["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"].endswith("token_tuner.py hook")
print("nudge hook ok")
print("ALL TESTS PASSED")
