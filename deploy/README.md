# Photo Prompt deployment

This directory contains the tracked deployment description only. The active
TOML file and staff secret are supplied by the operator and are not tracked.
The application listens on port `8000` inside its Podman pod; the pod publishes
no host port and there is no Caddy container here.

## One-time manual preparation

1. Configure DNS for `photo-prompt.umlab.me` and attach the existing/shared
   Caddy network to `photo-prompt.network`.
2. Configure the shared Caddy site with whole-site Basic Auth user `kiosk`.
   Keep that Caddy configuration external; this repository does not modify it.
3. Copy `conf/app.sample.toml` to the host's active
   `~/photo-prompt/app.toml`, then set its absolute container paths:
   `/var/lib/photo-prompt/state/photo-prompt.shelfdb`,
   `/var/lib/photo-prompt/generated`, `/run/photo-prompt/pi-rpc`,
   `/app/dist`, and `/app/deploy/codex-bridge.ts`.
4. Staff search is disabled when the PIN is missing. To opt in manually, create
   the named Podman secret `photo-prompt-staff-pin` containing a valid six-digit
   PIN, then add this line to the operator's untracked installed container
   Quadlet (for example, `~/.config/containers/systemd/photo-prompt.container`)
   before reloading the user units:

   ```ini
   Secret=photo-prompt-staff-pin,type=env,target=PHOTO_PROMPT_STAFF_PIN
   ```

   The PIN stays outside TOML, tracked files, and logs. Without this opt-in
   line, the gameplay container starts normally and staff search remains off.
5. Sign the Pi device-code account in the persisted `photo-prompt-pi-home`
   volume once, so `auth.json` and settings survive restarts.
6. On kiosk clients, use silent Chrome `--kiosk-printing` and configure the one
   shared network printer. The server does not run CUPS.

The `photo-prompt.network`, pod, container, and three named volumes are native
Quadlets. The pod attaches to `photo-prompt.network`; Caddy remains external to
this repository. The container mounts state, generated images, and Pi home
separately; private RPC workspaces are ephemeral. The container has one Uvicorn
worker and uses the non-sensitive `/health` projection (`ready` and active
generation count only).

## Normal deployment

Run the committed script locally from a clean, up-to-date x86_64 `main`:

```bash
uv run --script deploy/deploy.py deploy --host kiosk-host
```

The preflight requires `HEAD == origin/main`, builds frontend assets and the
image locally, tags the image by the exact commit SHA, transfers only the OCI
image archive, verifies its SHA-256 after transfer, and loads the SHA-tagged
image remotely. Before changing the service, it validates the host's active
`~/photo-prompt/app.toml` by mounting it read-only into the new image, then
aborts when the non-sensitive health projection reports an active generation.
It tags the new image as `:deploy`, restarts the service, polls readiness for a
bounded deadline, and removes the transferred archive. A failed health check
returns an error without automatic recovery; retain the SHA-tagged image for
AI-assisted manual recovery requiring separate explicit remote approval. It
never runs a global prune.

The only script commands are `status` and `deploy`:

```bash
uv run --script deploy/deploy.py status --host kiosk-host
```

## Event close and post-event cleanup

Close the event manually after the final booth round. Afterward, archive the
state and generated volumes according to the school's retention policy, then
remove old generated images and unused volumes manually. No scheduled cleanup
or automated event-close action is provided.
