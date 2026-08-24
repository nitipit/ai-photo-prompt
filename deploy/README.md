# Photo Prompt deployment

This directory contains the tracked deployment description only. The active
TOML file and staff secret are supplied by the operator and are not tracked.
The application listens on port `8000` inside its Podman pod; the pod publishes
no host port and there is no Caddy container here.

## One-time manual preparation

Run these steps from the repository checkout on the x86_64 kiosk host. They
install every tracked Quadlet while leaving the gameplay service disabled until
the first image has been deployed.

1. Enable a persistent user manager and install the complete Quadlet set:

   ```bash
   loginctl enable-linger "$USER"
   install -d -m 0755 "$HOME/.config/containers/systemd"
   install -m 0644 deploy/*.container deploy/*.network deploy/*.pod deploy/*.volume \
     "$HOME/.config/containers/systemd/"
   systemctl --user daemon-reload
   systemctl --user enable --now \
     photo-prompt-network.service \
     photo-prompt-state-volume.service \
     photo-prompt-generated-volume.service \
     photo-prompt-pi-home-volume.service \
     photo-prompt-pod.service
   ```

2. Connect the existing Caddy container to the shared network as the Podman
   user that owns Caddy, then configure the external site with this exact
   upstream (never `127.0.0.1` or a host-published port):

   ```bash
   podman network connect photo-prompt.network <existing-caddy-container>
   ```

   ```caddyfile
   reverse_proxy photo-prompt:8000
   ```

   Configure DNS for `photo-prompt.umlab.me` and whole-site Basic Auth user
   `kiosk` in that external Caddy configuration. This repository does not
   modify Caddy.

3. Create the active configuration at the exact Quadlet path and make it
   readable by container UID/GID `10001:10001` using ordinary DAC permissions:

   ```bash
   install -d -m 0755 "$HOME/photo-prompt"
   install -m 0644 conf/app.sample.toml "$HOME/photo-prompt/app.toml"
   chmod 0755 "$HOME/photo-prompt"
   chmod 0644 "$HOME/photo-prompt/app.toml"
   ```

   Set the absolute container paths in `app.toml` to
   `/var/lib/photo-prompt/state/photo-prompt.shelfdb`,
   `/var/lib/photo-prompt/generated`, `/run/photo-prompt/pi-rpc`,
   `/app/dist`, and `/app/deploy/codex-bridge.ts`. The PIN is never placed in
   this file.

4. Pre-pull the exact x86_64 base image digest used by `Containerfile`; the
   deployment build intentionally uses `--pull=never`:

   ```bash
   podman pull --platform linux/amd64 \
     registry.fedoraproject.org/fedora:44@sha256:e82672761671216fdd87c14d90379ad4368ab0200f072f9dffa5bd0459302f33
   ```

5. Staff search is disabled when the PIN is missing. To opt in manually, create
   the named Podman secret `photo-prompt-staff-pin` containing a valid six-digit
   PIN, then add this line to the operator's untracked installed container
   Quadlet before reloading the user units:

   ```ini
   Secret=photo-prompt-staff-pin,type=env,target=PHOTO_PROMPT_STAFF_PIN
   ```

   The PIN stays outside TOML, tracked files, and logs. Without this opt-in
   line, the gameplay container starts normally and staff search remains off.

6. The `photo-prompt-pi-home` volume is created by the prerequisite command.
   Complete the Pi device-code sign-in once through the installed Pi runtime
   with that volume mounted at `/home/photo-prompt/.pi`; verify its `auth.json`
   and settings remain in the named volume across restarts.

7. On kiosk clients, use silent Chrome `--kiosk-printing` and configure the one
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
`~/photo-prompt/app.toml` by mounting it read-only into the new image. If the
existing app container is present, its non-sensitive health projection must be
valid and idle; an absent container is treated as the first deployment. It tags
the new image as `:deploy`, restarts the service, polls readiness for a bounded
deadline, and removes the transferred archive. A failed health check
returns an error without automatic recovery; retain the SHA-tagged image for
AI-assisted manual recovery requiring separate explicit remote approval. It
never runs a global prune. After the first successful deploy, enable the
application unit for future user-manager starts:

```bash
systemctl --user enable photo-prompt.service
```

The only script commands are `status` and `deploy`:

```bash
uv run --script deploy/deploy.py status --host kiosk-host
```

## Event close and post-event cleanup

Close the event manually after the final booth round. Afterward, archive the
state and generated volumes according to the school's retention policy, then
remove old generated images and unused volumes manually. No scheduled cleanup
or automated event-close action is provided.
