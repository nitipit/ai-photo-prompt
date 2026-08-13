const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const form = root.querySelector<HTMLFormElement>("#prompt-form");
const countdown = root.querySelector<HTMLElement>("#prompt-countdown");
const reason = root.querySelector<HTMLInputElement>("#submission-reason");

if (form && countdown && reason) {
  const MAX_PROMPT_SECONDS = 90;
  const deadline = Date.parse(countdown.dataset.deadline ?? "");
  let submitted = false;
  let timer: number | undefined;

  const secondsUntilDeadline = (): number =>
    Math.min(
      MAX_PROMPT_SECONDS,
      Math.max(0, Math.ceil((deadline - Date.now()) / 1000)),
    );

  const renderCountdown = (remaining: number): void => {
    const minutes = Math.floor(remaining / 60).toString().padStart(2, "0");
    const seconds = (remaining % 60).toString().padStart(2, "0");
    countdown.textContent = `${minutes}:${seconds}`;
  };

  const submitTimeout = (): void => {
    if (submitted) {
      return;
    }
    submitted = true;
    reason.value = "timeout";
    form.requestSubmit();
  };

  const updateCountdown = (): void => {
    const remaining = secondsUntilDeadline();
    renderCountdown(remaining);
    if (remaining === 0) {
      if (timer !== undefined) {
        globalThis.clearInterval(timer);
      }
      submitTimeout();
    }
  };

  form.addEventListener("submit", () => {
    if (submitted) {
      return;
    }
    submitted = true;
    reason.value = "manual";
    if (timer !== undefined) {
      globalThis.clearInterval(timer);
    }
  });

  if (Number.isFinite(deadline)) {
    updateCountdown();
    if (!submitted) {
      timer = globalThis.setInterval(updateCountdown, 1000);
    }
  }
}
