# token-tuner

Reads your local Claude Code transcripts (`~/.claude/projects/*/*.jsonl`), finds patterns that waste
tokens, and keeps a small managed block of rules in `CLAUDE.md`. Python 3.8+, standard library only,
no network calls.

## Security disclaimer

This tool reads session transcripts, which can contain code, secrets, and private conversation.
**We do our best to keep it safe; do your own security review before
using it.** It is provided as-is, without warranty. Read `token_tuner.py` first (it is short), run
`python3 -I audit_check.py` to re-check that it makes no network or subprocess calls and only touches
the files listed in [SECURITY.md](SECURITY.md), and note the known limits there, including that this
tool and its audit were written by an AI assistant and are not independently reviewed.

## Install

As a Claude Code plugin (adds a `token-tuner` skill that runs the script for you):

```bash
claude plugin marketplace add osimjee/token-tuner
claude plugin install token-tuner@token-tuner
```

Or inside a session: `/plugin install token-tuner --marketplace osimjee/token-tuner`. The plugin ships
only the skill; the nudge hook is never installed automatically (see below). Or just clone the repo
and run the script directly, as in Usage.

## Usage

```bash
python3 token_tuner.py report                      # what it found, nothing changes
python3 token_tuner.py apply --dry-run             # show the CLAUDE.md diff, nothing written
python3 token_tuner.py apply                       # show diff, ask, back up, write
python3 token_tuner.py rollback                    # undo the most recent change
python3 token_tuner.py status                      # rules currently applied
```

Useful flags: `--days 30`, `--project <text in path>`, `--global` (aggregate everything and write
`~/.claude/CLAUDE.md`), `--include-repeated` (also propose instructions you typed in 3+ sessions).

## What it detects

| Pattern | Rule added |
|---|---|
| Re-reading a file that was not edited in between | reuse what is already in context |
| Whole-file Reads of large files | Grep first, then Read with offset/limit |
| Very long Bash output | pipe through head/tail/grep |
| Write over a file already read | use Edit instead |
| Many subagent spawns | use Grep/Glob for simple lookups |

A rule is only proposed when it shows up in 2+ sessions and clears a minimum size (thresholds are in
`RULE_DEFS` at the top of the script).

## Context size and nudges

`report` also shows per-turn context size and the model mix, and flags long or large-context
sessions. Every turn re-reads the whole context, so this is often the biggest cost.

An opt-in hook can nudge you while you work. It runs on each prompt, reads only the end of the
current transcript, and asks Claude to tell you one sentence (numbers only) when:

- the context passes ~250K tokens (and again at ~500K): consider `/compact` or a fresh session
- you were away long enough that the prompt cache has probably expired (default 60 min)
- the last turn made 8+ read-only calls in a row (consider a subagent) or repeated one shell command 3+ times

```bash
python3 token_tuner.py hook-config      # prints a settings.json snippet; you merge it yourself
python3 token_tuner.py hook --dry-run --transcript <a .jsonl under ~/.claude/projects>   # try it first
```

These are nudges, not automation: as far as we know a hook cannot run `/compact` for you, and Claude
is only asked to relay the note. Thresholds are heuristics; tune them with flags on the hook command
(`--ctx-warn`, `--ctx-urgent`, `--cache-ttl-min`, `--streak`, `--repeat`).

## Design choices

- **Propose, then apply.** Nothing is written without a diff and a confirmation (or `--yes`).
- **One managed block.** Everything lives between `token-tuner:start/end` markers; the rest of your
  CLAUDE.md is untouched. Capped at 8 rules because CLAUDE.md is re-read every turn.
- **Rules persist.** A working rule makes its own evidence disappear, so applied rules stay until you
  remove them (delete the block, or `rollback`).
- **Backups.** Each apply saves the previous file to `~/.claude/token-tuner/backups/`.

## Automating it (opt-in)

Nothing runs on its own. If you want it to, a weekly cron entry is the simplest:

```
0 9 * * 1  python3 /path/to/token_tuner.py apply --yes --global
```

Run `report` and `apply --dry-run` by hand a few times first, so you trust what it would write.

## Limits

- Waste numbers are rough estimates (characters / 4), not billing data.
- A rule only helps if Claude follows it. Re-run `report` after a few weeks to see whether the pattern shrank.
- File edits made through Bash (`sed -i`, etc.) are not seen, so a few re-reads may be over-counted.
- Tested against synthetic transcripts that follow the Claude Code JSONL layout, not yet against real
  ones. If `report` finds nothing, check that `~/.claude/projects/` has recent `.jsonl` files.

Run the tests with `python3 test_token_tuner.py`.

Licensed under the [MIT License](LICENSE).
