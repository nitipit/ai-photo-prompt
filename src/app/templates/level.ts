import { playSound } from "./_sound.js";

const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const choices = [
  ...root.querySelectorAll<HTMLInputElement>('input[name="level"]'),
];

function updateSelectedChoice(): void {
  const selected = choices.find((choice) => choice.checked)?.value;
  for (const card of root.querySelectorAll<HTMLElement>("[data-level-card]")) {
    card.toggleAttribute("data-selected", card.dataset.level === selected);
  }
}

for (const choice of choices) {
  choice.addEventListener("change", () => {
    updateSelectedChoice();
    void playSound("ui-click");
  });
}
updateSelectedChoice();
