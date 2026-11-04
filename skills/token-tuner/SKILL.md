---
name: token-tuner
description: Analyze the user's local Claude Code session transcripts for token-wasting patterns (re-reads, large whole-file reads, long shell output, big context) and propose CLAUDE.md rules. Use when the user asks to reduce token usage, review session cost, or tune CLAUDE.md for efficiency.
---

# Token Tuner

Runs the bundled script `${CLAUDE_PLUGIN_ROOT}/token_tuner.py` (Python 3.8+, standard library only, no network). It reads `~/.claude/projects/*/*.jsonl` and can edit one managed block in a `CLAUDE.md`.

## Rules for using it

1. **Start read-only.** Run `python3 "${CLAUDE_PLUGIN_ROOT}/token_tuner.py" report` (optionally `--days N`, `--project <text>`) and summarize the findings in a few lines: what pattern, how much estimated waste, and the context-size flags.
2. **Preview before writing.** Run `apply --dry-run` and show the user the diff. Never run `apply --yes` unless the user explicitly asks for it after seeing the diff.
3. **Ask before `--global` and `--include-repeated`.** `--global` writes `~/.claude/CLAUDE.md`. `--include-repeated` copies text the user typed into CLAUDE.md, which may become public if that file is committed.
4. **Undo:** `rollback` restores the previous file; `status` lists applied rules.
5. **Do not install the nudge hook.** `hook-config` only prints a `settings.json` snippet. Show it if the user asks, and let them merge it themselves. Never edit their settings.
6. Waste figures are rough estimates (characters / 4), not billing data. Say so when reporting numbers.

## Security

The script is auditable: `python3 -I "${CLAUDE_PLUGIN_ROOT}/audit_check.py"` re-checks that it makes no network or subprocess calls. Transcripts can contain secrets, so don't paste raw transcript content into the conversation; report counts and patterns only. See `SECURITY.md` in the plugin directory for the full list of what it reads and writes.
