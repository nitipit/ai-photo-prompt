import { playSound } from "./_sound.js";

type GenerationState =
  | "waiting"
  | "running"
  | "failure"
  | "generated"
  | "conflict";

const POLL_INTERVAL_MS = 250;
const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const scene = root.querySelector<HTMLElement>('[data-scene="generating"]');
const runForm = root.querySelector<HTMLFormElement>("#generating-run-form");
const retryForm = root.querySelector<HTMLFormElement>(
  "#generating-retry-form",
);
const continueForm = root.querySelector<HTMLFormElement>(
  "#generating-continue-form",
);

let nativeFallback = false;
let generationRequestActive = false;
let polling = false;
let pollTimer: number | undefined;
let activeForm: HTMLFormElement | null = null;

const setFormBusy = (form: HTMLFormElement, busy: boolean): void => {
  const button = form.querySelector<HTMLButtonElement>('button[type="submit"]');
  if (button) {
    button.disabled = busy;
  }
};

const stopPolling = (): void => {
  polling = false;
  if (pollTimer !== undefined) {
    globalThis.clearTimeout(pollTimer);
    pollTimer = undefined;
  }
};

const showNativeFallback = (message: string): void => {
  stopPolling();
  generationRequestActive = false;
  nativeFallback = true;
  if (activeForm) {
    setFormBusy(activeForm, false);
  }
  const statusMessage = scene?.querySelector<HTMLElement>(
    ".generating-status-message",
  );
  if (statusMessage) {
    statusMessage.textContent = message;
  }
};

const reloadGeneratingScene = (): void => {
  if (!scene) {
    return;
  }
  globalThis.location.replace(
    scene.dataset.generatingUrl ?? globalThis.location.pathname,
  );
};

const scheduleStatusPoll = (): void => {
  if (!polling || pollTimer !== undefined) {
    return;
  }
  pollTimer = globalThis.setTimeout(() => {
    pollTimer = undefined;
    void readGenerationStatus();
  }, POLL_INTERVAL_MS);
};

const readGenerationStatus = async (): Promise<void> => {
  if (!polling || !scene) {
    return;
  }
  const statusUrl = scene.dataset.statusUrl;
  if (!statusUrl) {
    showNativeFallback("ระบบยังไม่พร้อม กดปุ่มอีกครั้งเพื่อเริ่มใหม่");
    return;
  }

  try {
    const response = await fetch(statusUrl, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (response.status === 409) {
      stopPolling();
      reloadGeneratingScene();
      return;
    }
    if (!response.ok) {
      showNativeFallback("เชื่อมต่อระบบไม่ได้ กดปุ่มอีกครั้งเพื่อเริ่มใหม่");
      return;
    }

    const payload: unknown = await response.json();
    const state: GenerationState | null =
      typeof payload === "object" && payload !== null && "state" in payload &&
        typeof payload.state === "string"
        ? payload.state as GenerationState
        : null;
    if (state === "generated" || state === "failure") {
      stopPolling();
      reloadGeneratingScene();
      return;
    }
    if (state === "waiting" || state === "running") {
      scheduleStatusPoll();
      return;
    }
    showNativeFallback("ระบบส่งสถานะที่ไม่ถูกต้อง กดปุ่มอีกครั้งเพื่อเริ่มใหม่");
  } catch (_error) {
    showNativeFallback("เชื่อมต่อระบบไม่ได้ กดปุ่มอีกครั้งเพื่อเริ่มใหม่");
  }
};

const startStatusPolling = (): void => {
  if (polling) {
    return;
  }
  polling = true;
  void readGenerationStatus();
};

const postGeneration = async (form: HTMLFormElement): Promise<void> => {
  activeForm = form;
  generationRequestActive = true;
  setFormBusy(form, true);
  startStatusPolling();

  try {
    const response = await fetch(form.action, {
      body: new FormData(form),
      method: "POST",
    });
    if (!response.ok) {
      if (response.status === 409) {
        stopPolling();
        reloadGeneratingScene();
        return;
      }
      showNativeFallback("เริ่มสร้างภาพไม่สำเร็จ กดปุ่มอีกครั้งเพื่อลองใหม่");
      return;
    }
    await readGenerationStatus();
  } catch (_error) {
    showNativeFallback("เชื่อมต่อระบบไม่ได้ กดปุ่มอีกครั้งเพื่อเริ่มใหม่");
  }
};

const interceptGenerationForm = (form: HTMLFormElement): void => {
  form.addEventListener("submit", (event: SubmitEvent) => {
    if (nativeFallback) {
      return;
    }
    event.preventDefault();
    if (generationRequestActive) {
      return;
    }
    void postGeneration(form);
  });
};

if (scene) {
  const state = scene.dataset.generatingState as GenerationState | undefined;
  if (state === "waiting") {
    void playSound("prompt-submit");
  } else if (state === "generated") {
    void playSound("generation-complete");
  } else if (state === "failure") {
    void playSound("generation-error");
  }

  if (state === "waiting" && runForm) {
    interceptGenerationForm(runForm);
    runForm.requestSubmit();
  } else if (state === "failure" && retryForm) {
    interceptGenerationForm(retryForm);
  } else if (state === "running") {
    activeForm = retryForm ?? runForm;
    if (activeForm) {
      generationRequestActive = true;
      interceptGenerationForm(activeForm);
      setFormBusy(activeForm, true);
    }
    startStatusPolling();
  }
}

if (scene?.dataset.generatingState === "generated" && continueForm) {
  let continued = false;
  const revealDeadline = scene.dataset.revealDeadline;
  const parsedDeadline = revealDeadline
    ? Date.parse(revealDeadline)
    : Number.NaN;
  const delay = Number.isFinite(parsedDeadline)
    ? Math.min(5000, Math.max(0, parsedDeadline - Date.now()))
    : 5000;

  const continueOnce = (): void => {
    if (continued) {
      return;
    }
    continued = true;
    continueForm.requestSubmit();
  };

  continueForm.addEventListener("submit", () => {
    continued = true;
  });
  globalThis.setTimeout(continueOnce, delay);
}
