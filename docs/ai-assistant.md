# AI Assistant

The assistant is a chat panel docked at the bottom of the window (see
[editor-ui.md](editor-ui.md#menu-bar)) backed by either a local Ollama
model (a separate container, `ai-assistant` in `docker/docker-compose.yml`) or
a user-configured external API (Anthropic- or OpenAI-compatible).
Either way, it can explain LaTeX problems, suggest improvements, and
propose file changes -- but it never writes to a file on its own.

## The propose/apply safety model

Every assistant action (chat, the 15-second auto-scan, fixing a
compile error) only ever *returns* a proposal: a described, previewable
change (write a file, create/delete/rename an entry). Nothing is
written to disk until the user explicitly clicks **Accept** on that
proposal's card in the chat log. Clicking **Reject** just dismisses it.

Applying an accepted proposal goes through
`services/project_files.py` (the same path/content code the file
browser and REST API use) and then immediately records a git commit
(`services/git_history.py`) -- the same mechanism the History panel's
"Save Version" uses. This means every change the assistant ever makes
is recoverable through the History panel's revert flow, even in the
case where the change itself was wrong: nothing the assistant does can
make an earlier version of the project disappear.

## Provider configuration

Open the Assistant panel's **Settings** tab to choose:

- **Local (Ollama)** -- talks to the `ai-assistant` container
  (`config.OLLAMA_BASE_URL`). The default model is `gemma4:31b-cloud`
  (`assistant.DEFAULT_LOCAL_MODEL`), an Ollama cloud model: it runs on
  ollama.com, so nothing large is downloaded, but the Ollama server
  must be signed in. Per the
  [Ollama cloud docs](https://docs.ollama.com/cloud), cloud models need
  a signed-in Ollama; run `docker compose -f docker/docker-compose.yml
  exec ai-assistant ollama signin` once and follow the printed link.
  Prompts and responses are processed by Ollama's cloud, so this is not
  an offline setup. Whether the sign-in survives a container rebuild
  has not been verified. To run fully locally, paste a tag exactly as
  shown on [ollama.com/library](https://ollama.com/library) (e.g.
  `qwen2.5-coder:3b`) and click **Pull model**. A progress bar shows
  Ollama's own download status plus a byte-count/ETA, and a toast
  confirms the moment it's ready. Clearing the model field makes chat
  return a "no local model is configured yet" error.
- **External API** -- Anthropic's native Messages API, or any
  OpenAI-compatible Chat Completions API, plus your own model name and
  API key. The "Provider" dropdown offers convenience presets
  (DeepSeek, Kimi/Moonshot AI, Qwen/DashScope, Gemini, NVIDIA NIM, or
  "Custom") that auto-fill the right base URL -- these all speak the
  same OpenAI-compatible protocol already implemented, so a preset is
  just a base URL shortcut, not a separate integration. The key is
  stored server-side only (`assistant_config/settings.json`, a bind
  mount -- see
  [architecture.md](architecture.md#every-piece-of-state-lives-in-a-bind-mounted-volume))
  and is never echoed back to the browser.
  - Perplexity's preset ships with its base URL left blank: three
    separate checks of Perplexity's own docs returned three different,
    mutually contradictory endpoints, so a guess wasn't shipped as a
    default -- confirm the current one at
    [docs.perplexity.ai](https://docs.perplexity.ai) and paste it in.
  - **Fallback providers**: add one or more additional external
    provider configs in the "Fallback providers" section. If the
    active provider comes back with an HTTP 429 specifically (not a
    bad key or bad model name -- those are configuration problems you
    need to see and fix, not something another provider resolves),
    each configured fallback is tried in order; the first one that
    works is promoted to be the new active provider (persisted), and
    the rate-limited one is kept as a fallback rather than discarded,
    so it's retried automatically once everything else is also
    exhausted. A toast announces a switch like this when it happens.

### Reliability: chat and compile-error fixing never hit a hard timeout

A provider call -- local or external -- can legitimately take longer
than any fixed timeout short enough to still be reasonable to block an
HTTP request on. This was a real, live-reproduced failure, not a
hypothetical: a realistic `fix_compile_error` prompt (a full file plus
a compile log, which can trigger a second sequential call via its own
retry) exceeded a 60-second timeout against a CPU-only local model
under load, and separately exceeded even 180 seconds against a slow
external provider, both ending in a `503 ... read operation timed out`
the user actually saw.

Chat and compile-error fixing now start a background job
(`POST .../assistant/chat/start`, `POST .../assistant/fix-compile-error/start`)
and the frontend polls for the result
(`GET /api/assistant/jobs/chat/{job_id}`, `.../jobs/fix-compile-error/{job_id}`)
instead of waiting on a single request. The "Thinking…" indicator in
the chat log now just keeps showing for as long as it actually takes
-- verified live recovering a real answer at 82 seconds, past the old
ceiling that used to kill the exact same request. The original
synchronous endpoints (`POST .../assistant/chat`,
`.../assistant/fix-compile-error`) still exist, for direct API/curl
use, and still carry a (now more generous, 180s) timeout of their own.

### A note on model capability

The propose/apply protocol asks the model to reply with a JSON object
for structure (reply text, action, path, description) and, separately,
plain out-of-band text blocks for any file content. This split exists
specifically because LaTeX is full of backslash commands, which are
not valid inside a JSON string unless doubled -- a mistake small models
make constantly, which used to silently corrupt or entirely discard
proposals.

Even with that fixed, how often a model produces a *useful* proposal
(versus a well-formed but hallucinated one, or none at all) scales with
model size and how code/instruction-tuned it is. Measured live, fixing
the same injected compile error, repeated several times each:

| Model | Size | Usable fix produced |
|---|---|---|
| `qwen2.5:0.5b` | 0.5B | effectively never |
| `gemma3:1b` | 1B | effectively never |
| `qwen2.5-coder:3b` | 3B, code-tuned | roughly 2 out of 3 attempts |

This is why `qwen2.5-coder:3b` is worth pulling over the smallest
things Ollama offers if compile-error fixing matters to you -- still
small/fast enough to run without a GPU, but the difference in actually
getting a working fix is large. No model is pulled automatically (see
above), so this is a recommendation to type into Settings, not
something that happens on its own. `fix_compile_error` also retries
once, with a more insistent
reminder, if the first attempt comes back without usable content; this
alone recovers a meaningful fraction of otherwise-failed attempts,
since the same prompt can get a different result from one sampling run
to the next.

When a model-produced "fix" is nonsensical rather than merely absent --
well-formed, passes the content checks above, but doesn't actually
address the error, or isn't valid LaTeX at all -- that currently isn't
caught before being shown to the user as a proposal; it's just as
reviewable, and just as recoverable through history if accepted, as
any other proposal. Actually compiling each candidate fix in an
isolated copy of the project before surfacing it (discarding ones that
still fail) would close this gap, at the cost of a second compile per
attempt; that's future work, not yet built.

If reliable compile-error fixing matters more than local/offline use,
a modern external API model (GPT-4o-class or better) follows this
protocol far more consistently than any size of local model tested so
far. This is a model-capability ceiling, not a configuration problem:
the same request, the same prompt, a more capable model.

## Writing style

Settings → AI Assistant also accepts one or more writing samples. Submitting
them asks the model to synthesize a style profile (tone, vocabulary,
sentence structure), which is persisted in the same config file and
included in every future prompt -- so the assistant's own phrasing (and
its judgment about what counts as a "worthwhile" suggestion during the
auto-scan) tracks the user's own voice rather than a generic one.

## Auto-scan

While a project and file are open, the panel checks every 15 seconds
whether the file's content has changed since the last check. If it
has, it asks the assistant for a single targeted improvement; if the
model proposes one, it's appended to the chat log exactly like a
manual turn, marked "Auto-suggested" -- never applied automatically. A
scan that fails (provider not configured, not reachable, rate-limited)
fails silently: it must never surface a toast every 15 seconds just
because a provider isn't set up yet.

**Rate-limit backoff.** A free-tier external provider's daily request
quota can be startlingly low -- confirmed live, Gemini's free tier for
`gemini-3.8-flash` allows **20 requests per day**, which a 15-second
auto-scan loop exhausts in five minutes of normal editing, after which
every retry for the rest of the day is guaranteed to fail the same
way. When an auto-scan fails with a rate-limit/quota-flavored error,
it now backs off for 10 minutes instead of retrying every 15 seconds
into a quota that's already spent -- see `assistant-panel.js`'s
`autoScanCooldownUntil`. This is a real, practical reason to prefer a
local model (or a provider with a generous quota) as the *active*
provider while leaving a free-tier external one configured only as a
fallback, rather than the other way around.

## Compile-error fixing

When a compile fails, the preview pane's error view shows an
**"Ask the assistant to fix this"** button. This sends the compile log
and the failing file's content to the assistant and shows its
explanation plus (if offered) one proposed fix.

Because the file being fixed is already known with certainty here (it's
whichever file just failed to compile), the proposal's path is
corrected to that file regardless of what the model wrote -- a
hallucinated path is a real, observed failure mode for small models.
A proposal is also dropped entirely, rather than surfaced, if its
content looks like it deletes most of the file instead of making a
targeted fix, or contains no LaTeX commands at all when the original
file clearly does (a sign the model echoed its own instructions back
instead of producing a document) -- this is where the
"non-destructive editing" requirement is actually enforced, rather
than only asked for in the prompt.
