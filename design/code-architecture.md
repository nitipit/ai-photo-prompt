# Photo Prompt Code Architecture

This document is the implementation contract for the first functional skeleton.
It keeps code and documentation connected: public boundaries and non-obvious
behavior belong in code docstrings, while this file provides the system map that
agents use to coordinate ownership and integration.

Visual polish is intentionally out of scope for this skeleton.

## Application Shape

Photo Prompt is a server-rendered, multi-page FastAPI/Jinja2 kiosk. Each scene is
an HTML page. Browser modules progressively enhance the same server-owned flow;
they do not own game state or mutation authority.

The functional scene loop is:

```text
Ready
→ Level Selection
→ Challenge Reveal
→ Prompt Entry
→ Generating / Generated Reveal
→ Result / Feedback
→ Leaderboard
→ Photo Print
→ Ready navigation for the next new round
```

A completed or abandoned round remains a terminal historical record. Returning
the kiosk to Ready never changes that record back into a Ready state.

## Source Tree

```text
src/
  build/
    app.ts
    challenges.py
  app/
    __init__.py
    config.py
    server.py
    web.py
    domain/
      __init__.py
      models.py
      scoring.py
      state.py
    content/
      __init__.py
      importer.py
      repository.py
    ai/
      __init__.py
      protocols.py
      results.py
      pipeline.py
      fake.py
    persistence/
      __init__.py
      rounds.py
    services/
      __init__.py
      game_round.py
    templates/
      _base.html
      _base.ts
      _components/
      _tokens/
      _lib/
      ready.html / ready.ts
      level.html / level.ts
      challenge.html / challenge.ts
      prompt.html / prompt.ts
      generating.html / generating.ts
      result.html / result.ts
      leaderboard.html / leaderboard.ts
      photo_print.html / photo_print.ts
```

`src/app/templates/` is intentionally both the Jinja template root and the
browser-module source root. `_components`, `_tokens`, and `_lib` remain under
this directory so page modules and support modules share one import root. This is
an approved project-specific adaptation of the nearby Adapter reference.

Generated output mirrors this source root beneath `dist/templates/`. FastAPI
registers SSR routes first and mounts `dist/` as the root static fallback last.

## Domain Contracts

Boundary records use strict Dictify models. Persistence and provider boundaries
serialize with recursive `model.dict()` mappings and reconstruct and validate a
model on every read.

### ChallengeSpec

Required fields:

- `schema_version: int = 1`
- `id: str`
- `title: str`
- `level: LevelGroup`
- `status: approved`
- `target_asset_url: str`
- `concept: str`
- non-empty `core_anchors: list[str]`
- `optional_details: list[str]`
- `example_prompt: str`
- `evaluation_notes: str`
- `feedback_focus: str`

### RoundRecord

Required lifecycle fields:

- UUID string `id`
- `state: GameState`
- normalized `display_name`
- nullable `level`, `challenge_id`, and `prompt`
- nullable prompt submission reason: `manual` or `timeout`
- nullable generated artifact, evaluations, score, and pipeline failure
- `feedback: list[str]`
- terminal disposition: `completed`, `abandoned`, or `None`
- UTC ISO-8601 string timestamps and nullable deadlines

Provider execution ownership is represented separately by an attempt claim so a
provider call never holds a ShelfDB transaction open.

### Supporting Models

- `ImageArtifact`
- `PromptEvaluation`
- `ImageMatchEvaluation`
- `ScoreResult`
- `LeaderboardEntry`
- `FailureDetail`
- strict tagged success/error envelopes for every AI provider result
- `AttemptClaim` with an attempt token, owner instance, claim time, and lease
  expiry expressed as MessagePack-compatible primitives

Public models, provider protocols, repositories, scoring policy, state
reconstruction, and orchestration methods receive useful docstrings. Trivial
field declarations and obvious route wrappers do not need narration.

## State Machine Contract

`python-statemachine==3.2.0` owns only allowed events, guards, and state
reconstruction. It performs no I/O, timing, AI calls, or persistence.

```text
level_selection
  ─configure→ challenge_reveal
challenge_reveal
  ─continue_challenge→ prompt_entry
prompt_entry
  ├─submit_prompt→ generating
  └─abandon_blank_timeout→ abandoned (final)
generating
  ├─pipeline_succeeded→ generated_reveal
  ├─pipeline_failed→ generating
  └─abandon_generation→ abandoned (final)
generated_reveal
  ─reveal_elapsed→ result
result
  ─show_leaderboard→ leaderboard (final completed history)
```

Ready is a global scene, not a reusable state stored on a round. `POST /rounds`
creates a fresh round already in `level_selection`.

Service-owned guards validate the persisted facts supplied to each event:
selected approved challenge, authoritative deadlines, nonblank submission,
validated pipeline result, and terminal disposition. Invalid or stale events do
not change persistence.

## Timing and Selection Defaults

- Prompt countdown: 90 seconds for every level.
- Generated-image reveal: 5 seconds.
- Post-round leaderboard display: 15 seconds, then navigate to Ready. The
  read-only leaderboard opened from Ready has no deadline and remains until the
  visitor navigates back.
- Challenge: independently random from the five approved challenges in the
  selected level. The selector is injected so tests are deterministic.
- Display name: trim surrounding whitespace, empty becomes `นิรนาม`, maximum 30
  characters.
- Prompt: maximum 1,000 characters.
- Manual blank prompt is rejected.
- Timeout with nonblank current text auto-submits.
- Timeout with blank text abandons the round and returns to Ready.

## Persistence Contract

Use exact initial pins:

```text
shelfdb==3.0.1
dictify==5.0.2
python-statemachine==3.2.0
```

The one-process skeleton uses one long-lived local ShelfDB environment managed by
FastAPI lifespan. Complete synchronous repository operations run through
`asyncio.to_thread()`. A transaction opens, reads/checks/writes, and closes in one
worker thread and never crosses `await`.

Named shelves:

- `challenges`: validated materialized `ChallengeSpec` mappings
- `rounds`: complete `RoundRecord` mappings keyed by round ID
- `attempt_claims`: transient generation claims keyed by round ID

Provider calls occur outside transactions. Generation claims use an atomic
read/check/write transaction and contain a unique attempt token and expiring
lease. Only the matching live token may renew, fail, or finalize. A concurrent
request receives an already-running result instead of invoking a second provider.
Expired claims may be replaced atomically; stale tokens cannot commit. Short fake
calls keep a timeout below one lease. Long-running Pi calls use a supervised,
token-matching heartbeat; losing renewal cancels the provider and forbids
finalization.

Provider failure records a safe bounded failure, releases the claim, preserves
the original prompt and challenge, and leaves the round in `generating`. Retry is
unbounded. Abandoning generation atomically marks the round terminal without a
score and invalidates any late provider completion.

Completed and abandoned rounds remain durable. Leaderboard data is a projection
of completed ranked rounds, not a duplicated store.

## Challenge Materialization

`src/build/challenges.py` discovers `design/challenges/*/challenge.md` and parses:

- YAML front matter
- `Concept`
- `Core scoring anchors`
- `Optional details`
- `Example short prompt`
- `Evaluation notes`
- `Feedback focus`

It rejects unknown schemas, duplicate IDs, unsupported levels or statuses,
missing WebP targets, malformed required sections, and empty core anchors. It
writes a deterministic ID-sorted catalog and copies targets into generated static
assets through atomic replacement.

The build must yield exactly 20 approved challenges and five per level. At
application startup the catalog is Dictify-validated and synchronized into the
`challenges` ShelfDB shelf. Runtime round services read the challenge repository;
they never parse Markdown or read `design/`.

## AI and Scoring Contracts

Bounded async protocols:

- `ImageGenerator.generate(...)`
- `PromptEvaluator.evaluate(...)`
- `ImageMatcher.evaluate(...)`
- `FeedbackComposer.compose(...)`

Each receives explicit typed inputs and a timeout and returns a strict tagged
success/error result. Raw provider exceptions never cross the pipeline boundary.
Deterministic fake providers remain the test and explicit local-development mode;
the kiosk selects the Pi provider explicitly and never silently falls back to fake.

Scoring remains pure:

```text
prompt = 40% clarity
       + 30% specificity
       + 20% relationship
       + 10% consistency

image  = 70% core concept
       + 20% supporting details
       + 10% scene coherence

total  = 50% prompt + 50% image
```

Inputs are bounded to `0..100`; only the final visible score is rounded. Feedback
contains exactly two or three safe, concise, child-facing strings.

A provider failure produces no score and never advances to Result. The Generating
scene offers retry and a clearly separated exit-to-Ready action.

### Production Pi provider

Pi is a bounded provider, never a game controller. Each pipeline attempt uses two
fresh `pi --mode rpc --no-session` subprocesses: one image-generation process and
one combined evaluation process. Processes never share sessions or conversation
state. The image process loads only the configured Codex bridge and exposes only
`codex_imagegen`; the evaluator has no extensions or tools. Context files, skills,
prompt templates, and built-in file or shell tools are disabled.

The RPC client uses LF-delimited UTF-8 JSONL, command IDs, tool-call IDs, bounded
stdout/stderr, a hard deadline, and process-group termination. Malformed records,
bad correlation, unexpected tools, duplicate completion, early exit, or missing
settlement fail the attempt safely. Assistant prose is never an authoritative
image result. Image generation accepts exactly one successful
`codex_imagegen` `tool_execution_end` and reads only
`result.details.outputPath`.

Submitting the round's generation action authorizes one image-generation attempt.
A headless RPC client answers the bridge's correlated confirmation request only
while that exact attempt authorization is live. A denied, cancelled, duplicate,
late, or mismatched confirmation fails without success. Cancellation terminates
and awaits the child before token-matching claim cleanup; a second cancellation
cannot skip cleanup.

Each attempt stages output in a server-derived private workspace beneath
`data/pi-rpc/`. The provider cannot choose a public path. The artifact store
rejects traversal, symlinks, overwrite, empty or non-PNG data, and configured
size or dimension violations. It atomically publishes a validated file beneath
`data/generated/<round-id>/<attempt-token>.png` and derives its browser URL from
those server-owned identifiers. FastAPI mounts only `data/generated/` at
`/generated/`; it never exposes the data root or persists a raw filesystem path.
A fenced or failed round commit removes its token-scoped unreferenced artifact.

The evaluator receives the materialized challenge, player prompt, target image,
and generated image as host-supplied inputs. One strict JSON object contains
`schema_version: 1`, the four prompt dimensions, three image-match dimensions,
and exactly two or three bounded Thai feedback strings. Missing, unknown,
wrong-typed, non-finite, out-of-range, fenced, or trailing output is rejected.
Provider-supplied scores, state, URLs, and persistence fields are forbidden. The
application reconstructs strict domain models and computes the only score through
`score_total`.

Pi timeouts, model, provider, thinking level, bridge path, private and published
roots, process output limits, and claim heartbeat/lease values are explicit
configuration. Provider choice is only `fake` or `pi`; missing Pi prerequisites
in Pi mode fail visibly and never invoke fake. One image attempt and one
evaluation attempt may run concurrently across the process, with bounded
admission before claim acquisition so rounds do not hold leases while waiting in
an unbounded provider queue.

## Leaderboard Contract

Leaderboard entries include rank, name, score, generated image, and full prompt
and are filtered to the selected level group.

Equal scores share the same competition rank, for example `1, 2, 2, 4`.
Generation latency and prompt completion time never break ties. The post-round
view emits at most four rows while preserving the current round and its global
rank, then advances to the Photo Print projection after its existing 15-second
window. The read-only public view emits the Top 4 for the selected level, has no
current-player highlight, and can be opened directly from Ready without creating
or completing a round. Photo Print is a read-only projection of a completed
leaderboard round and does not add a persisted state or print count.

## Route and HTML-First Contract

GET renders; POST mutates and redirects with `303` where appropriate. Unknown
rounds return `404`, malformed input `422`, and stale or invalid events `409`.

Required flow boundaries:

- `GET /` renders Ready.
- `GET /leaderboard` renders the persistent read-only Top 4 for a validated
  level query.
- `POST /rounds` creates a fresh round and redirects to level selection.
- Round-scoped GET/POST routes handle level, challenge, prompt, generating,
  result, leaderboard, and the direct `/rounds/{round_id}/photo-print` scene.
- Photo Print accepts only a completed leaderboard round, renders a server-owned
  A5 landscape projection, and remains until the player navigates to Ready.
- Generation has ordinary POST actions for start/retry and exit so JavaScript is
  not the only owner.
- Status polling is read-only and pipeline start/retry is idempotent through the
  attempt claim.
- Reveal and leaderboard timeout transitions are authorized by the server clock.
- Returning to `/` is navigation and never mutates completed history.

Without JavaScript, explicit forms keep the flow functional. With JavaScript,
timers auto-submit the same forms, generation starts automatically, status is
polled, the five-second reveal advances, and leaderboard returns after 15
seconds.

## Frontend Contract

- Jinja HTML and same-named TypeScript files are scene pairs.
- Adapter Web Components and Adapter CSS-in-JS are the primary presentation
  boundary.
- Native-like components retain native forms, buttons, labels, and input
  semantics.
- Arrow JS is limited to local reactive component behavior such as countdown
  rendering.
- Swup enhances scene navigation only and never owns mutation or game state.
- Source imports use browser-shaped `.js` specifiers; Deno resolves TypeScript.
- Page modules export a bounded mount/cleanup interface.
- Dependency bridges under `_lib` bundle; application page and component modules
  remain explicit ESM.
- No SPA router, global reactive store, global CSS framework, or client-owned
  session state.
- Approved arcade WAV files are published locally under `/audio/`. Sound is
  default-on progressive enhancement with one persisted mute control; blocked
  playback never blocks navigation, timing, persistence, or game state.
- Countdown and scene cues are owned by their existing browser modules. No
  background music or duplicate leaderboard fanfare is played.
- Photo Print uses a small browser module for user-initiated `print()` only;
  repeated requests are allowed after the browser's `afterprint` event and no
  physical print success is inferred.

## Implementation Ownership

Manager/default is the sole integration and shared-contract owner. Implementers
work from accepted commits in isolated worktrees and return a commit plus concise
validation evidence.

1. `implement-domain-state-content`
   - Python manifest and lockfile
   - domain models, state machine, shared test fixtures
   - challenge materializer/repository and focused tests
2. `implement-frontend-system`
   - JavaScript/Deno manifests and lockfiles
   - frontend build, template-root support modules, base shell and checks
3. `implement-game-persistence-web`
   - ShelfDB repository, attempt claims, game service, FastAPI routes and tests
4. `implement-ai-scoring`
   - AI result envelopes/protocols, fake providers, pure scoring and tests
5. `implement-scenes`
   - scene HTML/TS pairs connected to accepted web and frontend contracts

Ownership transfers between waves only after manager integration. Workers do not
edit another package's files, change shared contracts, or resolve conflicts by
guessing.

## Acceptance Evidence

The skeleton is complete only when evidence shows:

1. Exactly 20 challenge bundles materialize and sync into ShelfDB.
2. Dictify rejects missing, unknown, mistyped, and nested-invalid fields.
3. Every allowed transition passes and every other pair leaves persistence
   unchanged.
4. ShelfDB survives reconstruction; transactions roll back on failure.
5. Concurrent generation claims start one provider call; stale tokens cannot
   commit.
6. Failure permits unlimited retry; abandon rejects late completion and never
   creates a score.
7. Prompt deadlines enforce auto-submit or blank abandonment correctly.
8. Fake AI success produces exact weighted scores and two or three feedback
   lines.
9. Equal scores receive shared rank and the current round is highlighted.
10. A FastAPI test client reaches every scene and returns to global Ready.
11. No-JavaScript fallback forms and enhanced kiosk automation use the same
    server-authorized events.
12. Python tests and Deno build, formatting, linting, checking, and tests pass.
13. Generated paths mirror `templates/`, and runtime code never parses challenge
    Markdown.
14. AI providers and browser modules never write game state directly.
15. Pi RPC accepts one authorized image tool completion, validates and atomically
    publishes its raster, and rejects non-authoritative paths or duplicate tools.
16. Pi evaluation cannot set a score; strict dimensions and feedback are validated
    before the application computes and atomically persists a complete result.
17. Long-running claim heartbeats prevent duplicate live work, while renewal loss,
    cancellation, expiry, abandon, and shutdown cannot produce a late write.
18. Explicit Pi startup, mocked RPC failure/retry, server restart, and a bounded
    real-image Chrome round pass without silent fake fallback.
