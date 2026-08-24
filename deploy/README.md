# Photo Prompt deployment

This directory contains the tracked deployment description only. The active
TOML file and staff secret are supplied by the operator and are not tracked.
The application listens on port `8000` inside its Podman pod; the pod publishes
no host port and there is no Caddy container here.

## Remote kiosk first-time preparation

Run the remote commands as the same unprivileged rootless user that owns both
the Caddy and Photo Prompt containers and their Podman storage. Do not mix a
rootful Caddy container or a different rootless `graphroot` with this setup.

1. From the local build host, enable linger for that remote user, install only
   the network and named-volume Quadlet sources, reload, and start those
   prerequisites. Do not enable or start the pod or application at this stage:

   ```bash
   # Run the privileged linger step separately; replace the placeholder user.
   ROOTLESS_USER=photo-prompt-operator
   ssh -t kiosk-host sudo loginctl enable-linger "$ROOTLESS_USER"
   ssh kiosk-host 'loginctl show-user "$ROOTLESS_USER" -p Linger --value | grep -qx yes'
   ssh kiosk-host 'install -d -m 0755 "$HOME/.config/containers/systemd"'
   rsync -a \
     deploy/photo-prompt.network \
     deploy/photo-prompt-state.volume \
     deploy/photo-prompt-generated.volume \
     deploy/photo-prompt-pi-home.volume \
     kiosk-host:.config/containers/systemd/
   ssh kiosk-host systemctl --user daemon-reload
   ssh kiosk-host systemctl --user start \
     photo-prompt-network.service \
     photo-prompt-state-volume.service \
     photo-prompt-generated-volume.service \
     photo-prompt-pi-home-volume.service
   ```

2. Copy the active configuration to the remote kiosk at the exact Quadlet path,
   edit its absolute container paths, and make it readable by container
   UID/GID `10001:10001` using ordinary DAC permissions:

   ```bash
   ssh kiosk-host 'install -d -m 0755 "$HOME/photo-prompt"'
   scp conf/app.sample.toml kiosk-host:photo-prompt/app.toml
   # Edit kiosk-host:~/photo-prompt/app.toml with the paths below.
   ssh kiosk-host 'chmod 0755 "$HOME/photo-prompt" && chmod 0644 "$HOME/photo-prompt/app.toml"'
   ```

   Set the absolute container paths in `app.toml` to
   `/var/lib/photo-prompt/state/photo-prompt.shelfdb`,
   `/var/lib/photo-prompt/generated`, `/run/photo-prompt/pi-rpc`,
   `/app/dist`, and `/app/deploy/codex-bridge.ts`. The PIN is never placed in
   this file.

3. Staff search is disabled when the PIN is missing. To opt in manually, read
   and validate the PIN locally without echo, then send exactly six digits
   without a trailing newline through non-PTY SSH. Persist the opt-in in an
   operator-owned Quadlet source drop-in without editing the tracked base:

   ```bash
   (
     trap 'unset STAFF_PIN' EXIT
     read -r -s -p 'Staff PIN: ' STAFF_PIN
     printf '\n' >&2
     if [[ ! "$STAFF_PIN" =~ ^[0-9]{6}$ ]]; then
       printf 'PIN must be exactly six digits.\n' >&2
       exit 1
     fi
     printf %s "$STAFF_PIN" | ssh kiosk-host podman secret create photo-prompt-staff-pin -
   ) && \
   ssh kiosk-host 'install -d -m 0755 "$HOME/.config/containers/systemd/photo-prompt.container.d"' && \
   ssh kiosk-host 'cat > "$HOME/.config/containers/systemd/photo-prompt.container.d/10-staff-secret.conf"' <<'EOF' &&
   [Container]
   Secret=photo-prompt-staff-pin,type=env,target=PHOTO_PROMPT_STAFF_PIN
   EOF
   ssh kiosk-host systemctl --user daemon-reload
   ```

   The PIN is entered only on the local no-echo prompt and stays outside TOML,
   tracked files, and logs. Without this drop-in, the gameplay container starts
   normally and staff search remains off.

4. On kiosk clients, use silent Chrome `--kiosk-printing` and configure the one
   shared network printer. The server does not run CUPS.

## Local build host and release

The local build host is x86_64 and holds the clean, pushed `main` checkout. The
exact-digest base-image pre-pull belongs here, not on the remote kiosk host;
the deployment build intentionally uses `--pull=never`:

```bash
podman pull --platform linux/amd64 \
  registry.fedoraproject.org/fedora:44@sha256:e82672761671216fdd87c14d90379ad4368ab0200f072f9dffa5bd0459302f33
```

Before every release, sync all tracked Quadlet source files to the same
rootless remote user and reload that user's manager. This does not start the
pod or application; the first app start is `deploy.py`'s restart after the
image tag is switched:

```bash
rsync -a deploy/*.container deploy/*.network deploy/*.pod deploy/*.volume \
  kiosk-host:.config/containers/systemd/
ssh kiosk-host systemctl --user daemon-reload
uv run --script deploy/deploy.py deploy --host kiosk-host
```

## After app and authentication readiness

Only after the deploy command's readiness check succeeds, run the exact
one-time Pi device login as the same rootless user. The command explicitly
mounts the persisted volume; at the Pi prompt enter `/login`, select the
OpenAI Codex provider, and complete the device-code flow:

```bash
ssh -t kiosk-host podman run --rm -it \
  --user 10001:10001 \
  --volume photo-prompt-pi-home:/home/photo-prompt/.pi:rw \
  --workdir /app \
  --entrypoint /opt/pi/node_modules/.bin/pi \
  localhost/photo-prompt:deploy
```

Verify persistence without printing the credential contents:

```bash
ssh kiosk-host podman run --rm --user 10001:10001 \
  --volume photo-prompt-pi-home:/home/photo-prompt/.pi:ro \
  --entrypoint /usr/bin/test \
  localhost/photo-prompt:deploy -s /home/photo-prompt/.pi/agent/auth.json
```

If the staff-secret drop-in is enabled, verify the injected PIN without
printing it. This is a non-PTY, nonprinting check after app readiness:

```bash
ssh kiosk-host podman exec photo-prompt /opt/venv/bin/python -c \
  'import os,re,sys; raise SystemExit(0 if re.fullmatch(r"[0-9]{6}", os.environ.get("PHOTO_PROMPT_STAFF_PIN", "")) else 1)'
```

After app readiness, Pi authentication, and the optional staff check succeed,
create the persistent app boot drop-in. The tracked app and pod sources have no
`[Install]` section; this app-only drop-in creates the default-target link and
its dependency graph pulls the pod when the app starts:

```bash
ssh kiosk-host 'install -d -m 0755 "$HOME/.config/containers/systemd/photo-prompt.container.d"'
ssh kiosk-host 'cat > "$HOME/.config/containers/systemd/photo-prompt.container.d/20-boot.conf"' <<'EOF'
[Install]
WantedBy=default.target
EOF
ssh kiosk-host systemctl --user daemon-reload
ssh kiosk-host 'runtime="${XDG_RUNTIME_DIR:-/run/user/$UID}"; wants="$runtime/systemd/generator/default.target.wants"; test -L "$wants/photo-prompt.service"; test "$(readlink "$wants/photo-prompt.service")" = "../photo-prompt.service"; test ! -e "$wants/photo-prompt-pod.service"'
```

After both app readiness and the `auth.json` check succeed, add the durable
network membership to the external Caddy Quadlet's existing `[Container]`
section as its second `Network=` line; do not attach it ephemerally:

```ini
Network=<existing-caddy-network>
Network=photo-prompt.network
```

Then reload and restart Caddy as the same rootless user, and use this exact
upstream on the shared network:

```bash
systemctl --user daemon-reload
systemctl --user restart caddy.service
```

```caddyfile
reverse_proxy photo-prompt:8000
```

Configure DNS for `photo-prompt.umlab.me` and whole-site Basic Auth user
`kiosk` in that external Caddy configuration. This repository does not modify
Caddy.

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
never runs a global prune. Quadlet units are started by dependency and explicit
operator start/restart only; this runbook never enables generated units.

The only script commands are `status` and `deploy`:

```bash
uv run --script deploy/deploy.py status --host kiosk-host
```

## Event close and post-event cleanup

Close the event manually after the final booth round. Afterward, archive the
state and generated volumes according to the school's retention policy, then
remove old generated images and unused volumes manually. No scheduled cleanup
or automated event-close action is provided.
