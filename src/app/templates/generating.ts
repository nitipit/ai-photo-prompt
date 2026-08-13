const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const scene = root.querySelector<HTMLElement>('[data-scene="generating"]');
const image = root.querySelector<HTMLImageElement>("#generating-image");
const loader = root.querySelector<HTMLElement>("#generating-loader");
const statusMessage = root.querySelector<HTMLElement>(
  "#generating-status-message",
);
const placeholderBadge = root.querySelector<HTMLElement>(
  "#generating-placeholder-badge",
);
const nextMessage = root.querySelector<HTMLElement>("#generating-next-message");
const continueForm = root.querySelector<HTMLFormElement>(
  "#generating-continue-form",
);
const continueButton = root.querySelector<HTMLButtonElement>(
  "#continue-generating-button",
);

if (
  scene?.dataset.generatingState === "success" &&
  image &&
  loader &&
  statusMessage &&
  placeholderBadge &&
  nextMessage &&
  continueForm &&
  continueButton
) {
  let started = false;
  let continued = false;
  let continueTimer: number | undefined;

  const revealGeneratedPlaceholder = (): void => {
    if (started) {
      return;
    }
    started = true;
    image.hidden = false;
    loader.hidden = true;
    placeholderBadge.hidden = false;
    statusMessage.textContent = "ภาพตัวอย่างพร้อมแล้ว";
    nextMessage.textContent = "กำลังพาไปต่อในอีกสักครู่";
    continueButton.hidden = false;

    continueTimer = globalThis.setTimeout(() => {
      if (continued) {
        return;
      }
      continued = true;
      continueForm.requestSubmit();
    }, 5000);
  };

  continueForm.addEventListener("submit", () => {
    if (continued) {
      return;
    }
    continued = true;
    if (continueTimer !== undefined) {
      globalThis.clearTimeout(continueTimer);
    }
  });

  // This local timer is the explicit fake generation start for the visible seam.
  globalThis.setTimeout(revealGeneratedPlaceholder, 1500);
}
