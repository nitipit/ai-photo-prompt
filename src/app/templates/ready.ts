const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const startForm = root.querySelector<HTMLFormElement>("#start-form");
const displayNameInput = root.querySelector<HTMLInputElement>("#display-name");
const startButton = root.querySelector<HTMLButtonElement>("#start-button");

const updateStartButton = (): void => {
  if (!startButton || !displayNameInput) {
    return;
  }
  startButton.disabled = displayNameInput.value.trim().length === 0;
};

displayNameInput?.addEventListener("input", updateStartButton);
startForm?.addEventListener("submit", (event) => {
  if (!displayNameInput?.value.trim()) {
    event.preventDefault();
    updateStartButton();
    displayNameInput?.focus({ preventScroll: true });
  }
});

updateStartButton();
displayNameInput?.focus({ preventScroll: true });
