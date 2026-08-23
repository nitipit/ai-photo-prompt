export type SoundCue =
  | "ui-click"
  | "prompt-submit"
  | "countdown-tick"
  | "generation-complete"
  | "score-reveal"
  | "generation-error";

const STORAGE_KEY = "photo-prompt:sound-muted";
const AUDIO_VOLUME = 0.72;
const soundFiles: Record<SoundCue, string> = {
  "ui-click": "/audio/ui-click.wav",
  "prompt-submit": "/audio/prompt-submit.wav",
  "countdown-tick": "/audio/countdown-tick.wav",
  "generation-complete": "/audio/generation-complete.wav",
  "score-reveal": "/audio/score-reveal.wav",
  "generation-error": "/audio/generation-error.wav",
};
const players = new Map<SoundCue, HTMLAudioElement>();

const readMuted = (): boolean => {
  try {
    return globalThis.localStorage.getItem(STORAGE_KEY) === "true";
  } catch (_error) {
    return false;
  }
};

let muted = readMuted();

const playerFor = (cue: SoundCue): HTMLAudioElement => {
  const existing = players.get(cue);
  if (existing) {
    return existing;
  }
  const player = new Audio(soundFiles[cue]);
  player.preload = "auto";
  player.volume = AUDIO_VOLUME;
  players.set(cue, player);
  return player;
};

const stopPlayers = (): void => {
  for (const player of players.values()) {
    player.pause();
    player.currentTime = 0;
  }
};

const persistMuted = (): void => {
  try {
    globalThis.localStorage.setItem(STORAGE_KEY, String(muted));
  } catch (_error) {
    // Sound remains usable when storage is unavailable.
  }
};

export const isSoundMuted = (): boolean => muted;

export const setSoundMuted = (nextMuted: boolean): void => {
  muted = nextMuted;
  if (muted) {
    stopPlayers();
  }
  persistMuted();
  globalThis.dispatchEvent(
    new CustomEvent("photo-prompt:sound-change", { detail: { muted } }),
  );
};

export const playSound = async (cue: SoundCue): Promise<boolean> => {
  if (muted) {
    return false;
  }
  stopPlayers();
  const player = playerFor(cue);
  try {
    await player.play();
    return true;
  } catch (_error) {
    // Browser autoplay policy or a missing device must never block gameplay.
    return false;
  }
};

const soundCueFromPath = (path: EventTarget[]): SoundCue | null => {
  for (const item of path) {
    if (!(item instanceof HTMLElement)) {
      continue;
    }
    if (item.hasAttribute("data-sound-toggle")) {
      return null;
    }
    const cue = item.dataset.soundCue;
    if (cue && cue in soundFiles) {
      return cue as SoundCue;
    }
    if (item.matches("button, a")) {
      return "ui-click";
    }
  }
  return null;
};

const installInteractionSounds = (root: ShadowRoot): void => {
  root.addEventListener(
    "pointerdown",
    (event: Event) => {
      const cue = soundCueFromPath(event.composedPath());
      if (cue) {
        void playSound(cue);
      }
    },
    { capture: true },
  );

  root.addEventListener(
    "keydown",
    (event: Event) => {
      if (
        !(event instanceof KeyboardEvent) || !["Enter", " "].includes(event.key)
      ) {
        return;
      }
      const cue = soundCueFromPath(event.composedPath());
      if (cue) {
        void playSound(cue);
      }
    },
    { capture: true },
  );
};

const speakerIcon = (): SVGSVGElement => {
  const namespace = "http://www.w3.org/2000/svg";
  const icon = document.createElementNS(namespace, "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = `
    <path class="sound-speaker" d="M4 9v6h4l5 4V5L8 9H4Z" />
    <path class="sound-waves" d="M16 8.5a5 5 0 0 1 0 7M18.5 6a8.5 8.5 0 0 1 0 12" />
    <path class="sound-slash" d="m5 5 14 14" />
  `;
  return icon;
};

const updateToggle = (button: HTMLButtonElement): void => {
  button.dataset.muted = String(muted);
  button.setAttribute("aria-pressed", String(muted));
  button.setAttribute("aria-label", muted ? "เปิดเสียง" : "ปิดเสียง");
  button.title = muted ? "เปิดเสียง" : "ปิดเสียง";
};

export const installSoundControls = (root: ShadowRoot): void => {
  const topbar = root.querySelector<HTMLElement>(".scene-topbar");
  if (!topbar || topbar.querySelector("[data-sound-toggle]")) {
    return;
  }

  const sceneTag = topbar.querySelector<HTMLElement>(".scene-tag");
  const controls = document.createElement("div");
  controls.className = "scene-controls";
  if (sceneTag) {
    controls.append(sceneTag);
  }

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "sound-toggle";
  toggle.dataset.soundToggle = "true";
  toggle.append(speakerIcon());
  updateToggle(toggle);
  controls.append(toggle);
  topbar.append(controls);

  toggle.addEventListener("click", () => {
    setSoundMuted(!muted);
    if (!muted) {
      void playSound("ui-click");
    }
    updateToggle(toggle);
  });

  globalThis.addEventListener(
    "photo-prompt:sound-change",
    () => updateToggle(toggle),
  );
  installInteractionSounds(root);
};
