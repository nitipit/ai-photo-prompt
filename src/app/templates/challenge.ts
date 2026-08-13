const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const continueButton = root.querySelector<HTMLButtonElement>(
  "#continue-challenge-button",
);

continueButton?.focus({ preventScroll: true });
