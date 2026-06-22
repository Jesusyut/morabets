---
name: Public LLM endpoint safety (Mora Bets)
description: Constraints for any public route that calls a paid LLM, and for rendering its output
---
Mora Bets is a LIVE, no-login, fully public app. Any public endpoint that calls a
paid LLM API (Anthropic/OpenAI) MUST have a per-IP rate limit and a server-side
request timeout, or it is a trivial cost-exhaustion/DoS vector.

LLM output is untrusted (and can be influenced by user-supplied context fields).
Render it text-only — `textContent` / `createElement`, never `innerHTML` /
template strings — or it is a DOM XSS sink.

Error responses must be generic; never echo raw model payloads or `str(e)` to the
client (log them server-side instead).

**Why:** code review flagged both an XSS sink and an unauthenticated paid-API
abuse path on the first pass of the Context Edge feature.
**How to apply:** whenever adding/editing a public route that hits an LLM, or any
JS that renders model output into the DOM.

Note: Anthropic model id `claude-sonnet-4-6` is NOT valid; use
`claude-sonnet-4-5-20250929` (Claude Sonnet 4.5).

## Context Edge entry point (June 2026)
Context Edge is a SINGLE floating chat bubble (bottom-right), NOT a button on
every card. **Why:** per-card buttons risked accidental paid Claude calls on a
public app; one intentional entry point = zero unintended fan-out. The
/api/context-edge route accepts both a free-text `user_message` (chat format) and
the legacy structured prop fields, plus a `validate_only` passcode-check that
skips the Claude call. Keep both formats working if editing the route.
