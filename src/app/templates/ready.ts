const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const startButton = root.querySelector<HTMLButtonElement>("#start-button");

startButton?.focus({ preventScroll: true });
