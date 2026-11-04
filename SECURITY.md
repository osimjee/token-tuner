# Security notes and disclaimer

## Disclaimer

token-tuner reads your Claude Code session transcripts, which can contain source code, file paths,
secrets you pasted, and private conversation. **We do our best to keep this tool safe, but nothing
here is guaranteed. Do your own security review before running it.** It is provided as-is, without
warranty of any kind. You are responsible for what you run on your machine.

It is only ~670 lines of standard-library Python. Read `token_tuner.py` yourself, or have someone you
trust read it, before the first run.

## What it accesses

| Action | Where |
|---|---|
| Reads | `~/.claude/projects/*/*.jsonl` (session transcripts) and the `CLAUDE.md` it is about to edit |
| Writes | the target `CLAUDE.md` (one managed block between `token-tuner:start/end` markers) |
| Writes | `~/.claude/token-tuner/state.json` and `~/.claude/token-tuner/backups/` |
| Reads (hook, opt-in) | the last ~2 MB of the current transcript, and only if the path Claude Code passes is a `.jsonl` file under `~/.claude/projects` |
| Writes (hook, opt-in) | `~/.claude/token-tuner/nudges.json` (per-session nudge levels and timestamps; no transcript text) |
| Deletes | only in `rollback`, and only a `CLAUDE.md` the tool itself created |
| Network, subprocesses, `eval`/`exec` | none |

## What we checked (and how to re-run it)

```bash
python3 -I audit_check.py        # exit code 0 = no findings
```

1. **Static scan.** Every import and call in `token_tuner.py` is checked against an allowlist: standard
   library only, with no network, subprocess, `eval`/`exec`, or process-control calls.
2. **Dynamic check.** The tool is run end to end (`report`, `apply --dry-run`, `apply`, `status`,
   `rollback`) on synthetic data in a temp directory, under Python audit hooks that record every
   socket, subprocess, and exec event and every file opened. The check fails if anything touches the
   network, spawns a process, or opens a path outside the temp directory (other than Python's own
   library files and this repo).
3. **Hook path guard.** The audit also feeds the hook a transcript inside the projects folder, one
   outside it, and `/etc/passwd`, and fails if the hook opens either of the latter.
4. **The check can fail.** We confirmed it by auditing deliberately tampered copies (an added `socket`
   import plus an out-of-sandbox file read; and the hook's path guard removed); both reported FAIL.

## Known limits and residual risks

- **Not independent.** This tool and its audit script were written by an AI assistant (Claude) in the
  same session. A check written by the author catches mistakes, not bad intent or blind spots. A human
  or separate reviewer should read the code.
- **A passing audit is a tripwire, not a proof.** It cannot see a compromised Python install, a
  modified `audit_check.py`, supply-chain problems, or code paths the synthetic data does not reach.
  Make sure the files you run are the files you reviewed (compare `sha256sum` output).
- **`--include-repeated` copies your own words.** It writes the text of messages you typed in 3+
  sessions into `CLAUDE.md` and `state.json`. If your `CLAUDE.md` is committed to a public repository,
  that text becomes public. It is off by default; read the diff before confirming. Those messages are
  also text that Claude will treat as instructions in future sessions.
- **Hook output goes into Claude's context.** The nudge hook prints a short note containing numbers
  only (our tests check that no file paths or shell commands leak into it) and asks Claude to relay it.
  Claude is not guaranteed to follow that, and each nudge costs a few dozen tokens.
- **The hook runs on every prompt once installed.** It is written to stay silent and exit 0 on any
  problem, so it never blocks a prompt unless you add `--block` (then it holds at most one prompt
  per long break or context level, and sending it again goes through), but remove it from `settings.json` to disable it. This
  tool only prints the snippet (`hook-config`) and never edits your settings.
- **The hook depends on Claude Code's hook input format** (field names such as `transcript_path`,
  `session_id`, and transcript `timestamp`). If that changes, the hook goes silent rather than failing.
- **Backups hold full copies** of your `CLAUDE.md` in `~/.claude/token-tuner/backups/`.
- **State is trusted.** `rollback` restores to paths recorded in `state.json`. Anyone who can already
  write to your `~/.claude` folder can do worse directly, but keep it private.
- **Environment overrides.** `CLAUDE_PROJECTS_DIR`, `TOKEN_TUNER_DIR` and `TOKEN_TUNER_GLOBAL_MD`
  redirect where it reads and writes. Only set them yourself.
- **Running it through an AI assistant sends its output to that assistant.** If you ask Claude (or any
  assistant) to run the tool for you, the report output enters that conversation. Run it directly in
  a terminal if you want the output to stay on your machine.
- **Transcripts may contain secrets.** The tool does not copy transcript content except the
  `--include-repeated` feature above, but the files it reads are sensitive; keep that in mind before
  sharing any output.

## Plugin install

The plugin form adds `.claude-plugin/` manifests and `skills/token-tuner/SKILL.md`, which tells Claude
how to run the same script. It registers no hooks and edits no settings. Installing a plugin copies
this repository into Claude Code's plugin cache, so the same review advice applies: read the code, and
compare it with the commit you reviewed. The skill instructs Claude to preview changes and not to
apply them without your confirmation, but Claude is not guaranteed to follow that; the script's own
diff-and-confirm step is the real guard.

## Found a problem?

Fix it or open an issue with the maintainer, and please re-run `audit_check.py` after any change.
