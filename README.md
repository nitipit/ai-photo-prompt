# Photo Prompt

## Run the visible kiosk checkpoint

From the repository root:

```bash
deno task app:build
deno task app:serve
```

Open <http://127.0.0.1:8000/>. Press **เริ่มเล่น** to follow the temporary
`/rounds/demo/level` seam to Level Selection.

Focused checks (build first so generated challenge assets are available):

```bash
deno task app:build
deno task check
deno task lint
uv run pytest tests/integration/test_web_smoke.py
```
