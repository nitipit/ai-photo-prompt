const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const form = root.querySelector<HTMLFormElement>("#prompt-form");
const countdown = root.querySelector<HTMLElement>("#prompt-countdown");
const reason = root.querySelector<HTMLInputElement>("#submission-reason");

if (form && countdown && reason) {
  let remaining = Number.parseInt(countdown.dataset.seconds ?? "90", 10);
  let submitted = false;

  const renderCountdown = (): void => {
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

  renderCountdown();
  const timer = globalThis.setInterval(() => {
    remaining -= 1;
    renderCountdown();
    if (remaining <= 0) {
      globalThis.clearInterval(timer);
      submitTimeout();
    }
  }, 1000);

  form.addEventListener("submit", () => {
    if (submitted) {
      return;
    }
    submitted = true;
    reason.value = "manual";
    globalThis.clearInterval(timer);
  });
}
