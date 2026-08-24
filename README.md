# Photo Prompt

## Run the kiosk

From the repository root:

```bash
deno task app:build
deno task app:serve
```

`app:serve` loads the complete `conf/app.toml`; startup always constructs the
configured Pi provider and never falls back to fake AI. Copy `conf/app.sample.toml`
to the ignored active path for deployment. The machine must have the pinned `pi`
runtime and a signed-in Codex account. No separate API key is required.
Generated images are retained under `data/generated/`.

Open <http://127.0.0.1:8000/>. Press **เริ่มเล่น** to create a persisted
round through `POST /rounds`; the server redirects its UUID to Level Selection.
Image generation and evaluation may take about one minute. A bounded provider
failure remains on Generating and can be retried or abandoned.

Deterministic fake AI is available only by explicit `app.state.ai_pipeline`
dependency injection in tests and controlled local browser verification.

Focused checks (build first so generated challenge assets are available):

```bash
deno task app:build
deno task check
deno task lint
uv run pytest tests/integration/test_web_smoke.py
```
