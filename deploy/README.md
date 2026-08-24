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
4. Create the Podman secret named `photo-prompt-staff-pin` containing a valid
   six-digit PIN. The PIN is never placed in TOML or logs.
5. Sign the Pi device-code account in the persisted `photo-prompt-pi-home`
   volume once, so `auth.json` and settings survive restarts.
6. On kiosk clients, use silent Chrome `--kiosk-printing` and configure the one
   shared network printer. The server does not run CUPS.

The `photo-prompt.network`, pod, container, and three named volumes are native
Quadlets. The container mounts state, generated images, and Pi home separately;
private RPC workspaces are ephemeral. The container has one Uvicorn worker and
uses the non-sensitive `/health` projection (`ready` and active generation
count only).

## Normal deployment

Run the committed script locally from a clean, up-to-date x86_64 `main`:

```bash
uv run --script deploy/deploy.py deploy --host kiosk-host
```

The preflight requires `HEAD == origin/main`, validates the active TOML, builds
frontend assets and the image archive locally, tags the image by the exact
commit SHA, transfers the archive and image, verifies both SHA-256 values after
transfer, and loads the SHA-tagged image remotely. It retains only `:deploy`
and `:rollback` image tags and two small pre-deploy state-volume backups. It
aborts before changing anything when `/health` reports an active generation.
After restart it polls readiness; a failure restores the previous image and
state volume automatically. It never runs a global prune.

The only script commands are `status` and `deploy`:

```bash
uv run --script deploy/deploy.py status --host kiosk-host
```

## Event close and post-event cleanup

Close the event manually after the final booth round. Afterward, archive the
state and generated volumes according to the school's retention policy, then
remove old generated images and unused volumes manually. No scheduled cleanup
or automated event-close action is provided.
