const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const continueButton = root.querySelector<HTMLButtonElement>(
  "#continue-result-button",
);

continueButton?.focus({ preventScroll: true });
