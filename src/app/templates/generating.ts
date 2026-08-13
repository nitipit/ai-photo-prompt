const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const scene = root.querySelector<HTMLElement>('[data-scene="generating"]');
const runForm = root.querySelector<HTMLFormElement>("#generating-run-form");
const continueForm = root.querySelector<HTMLFormElement>(
  "#generating-continue-form",
);

if (scene?.dataset.generatingState === "waiting" && runForm) {
  let started = false;

  const submitRunOnce = (): void => {
    if (started) {
      return;
    }
    started = true;
    runForm.requestSubmit();
  };

  runForm.addEventListener("submit", () => {
    started = true;
  });
  submitRunOnce();
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
