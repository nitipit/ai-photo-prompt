# Photo Prompt

## Run the kiosk

From the repository root:

```bash
deno task app:build
deno task app:serve
```

`app:serve` selects the real Pi provider. The machine must have `pi` available,
a signed-in Codex account, and
`~/.pi/agent/extensions/codex-bridge.ts`. No separate API key is required.
Generated images are retained under `data/generated/`.

Open <http://127.0.0.1:8000/>. Press **เริ่มเล่น** to create a persisted
round through `POST /rounds`; the server redirects its UUID to Level Selection.
Image generation and evaluation may take about one minute. A bounded provider
failure remains on Generating and can be retried or abandoned.

Use deterministic fake AI explicitly for local tests or UI work:

```bash
PHOTO_PROMPT_AI_PROVIDER=fake uv run uvicorn app.server:app --app-dir src --reload
```

Focused checks (build first so generated challenge assets are available):

```bash
deno task app:build
deno task check
deno task lint
uv run pytest tests/integration/test_web_smoke.py
```
