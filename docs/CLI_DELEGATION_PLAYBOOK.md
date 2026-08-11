# CLI Agent Delegation Playbook

> Verified on 2026-08-11 in `D:\RamboStar\idea\radar-sim`.
> The primary agent owns product scope, architecture decisions, integration,
> deployment, and acceptance. CLI agents receive bounded tasks and must leave
> evidence; they do not redefine the product contract.

## Preferred order

1. **Pi (`pi`) is the default delegation CLI.** Use it for repository research,
   test analysis, bounded implementation, documentation, and independent review.
2. OpenCode is the first fallback when Pi is unavailable or its provider fails.
3. Claude Code is reserved for a genuinely complex, cross-module implementation
   after the primary agent has fixed the scope and acceptance criteria.
4. Gemini is primarily an independent reviewer or a fallback implementation CLI.

Never run two editing agents over the same files. Every editing task must name
owned files, forbidden files, acceptance tests, and the required handoff output.

## Verified installations

| CLI | Version | Non-interactive entry | Model selection |
| --- | --- | --- | --- |
| Pi | `0.83.0` | `pi --print --no-session` | `--model provider/model` |
| OpenCode | `1.18.16` | `opencode run` | `--model provider/model` |
| Claude Code | `2.1.220` | `claude --print` | `--model model` and `--effort level` |
| Gemini CLI | `0.54.0` | `gemini --prompt` | `--model model` |

## Pi model routing

Use the cheapest local/Bosch model that can safely finish the bounded task:

- Default: `bosch-qwen3_5/Qwen3.5-27B-FP16` for code search, test triage,
  documentation, small isolated changes, and first-pass review.
- Larger coding task: `bosch-coder/Qwen3.6-35B-A3B` or
  `bosch-aigc/qwen3-coder-plus`, still with narrow file ownership.
- Independent second opinion: `bosch-aigc/deepseek-v4-flash`.
- Cloud frontier models are escalation-only. Do not select them merely because
  they are available; the primary agent must first explain why the local route
  is insufficient.

Verified Pi model catalog entries also include Bosch Qwen 3.7, GLM, Kimi, and
cloud GPT models. Catalog availability can drift, so run `pi --list-models
<pattern>` before selecting an infrequently used model.

## Reusable Pi commands

Read-only review:

```powershell
pi --model bosch-qwen3_5/Qwen3.5-27B-FP16 --print --no-session `
  "Inspect the named files read-only. Do not edit. Return findings with file and test evidence."
```

Bounded implementation:

```powershell
pi --model bosch-coder/Qwen3.6-35B-A3B --print --no-session `
  "Own only <files>. Preserve other worktree changes. Implement <contract>. Run <tests>. Write a handoff with changed files, test results, and remaining risks."
```

Independent regression review:

```powershell
pi --model bosch-aigc/deepseek-v4-flash --print --no-session `
  "Review the current diff without editing. Find contract regressions and missing multi-user/path/transfer cases."
```

## Required task envelope

Every delegated task must state:

- product outcome and explicit non-goals;
- exact file/module ownership;
- multi-user, Web/SDK parity, path, and data-plane constraints that apply;
- tests or real acceptance evidence required;
- instruction to preserve unrelated dirty-worktree changes;
- a handoff path and a rule not to claim success without evidence.

The primary agent must review the diff, run integration/release gates, deploy,
and perform production black-box checks. A CLI agent saying "done" is never the
release decision.
