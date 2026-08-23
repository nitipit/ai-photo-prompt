const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const printButton = root.querySelector<HTMLButtonElement>(
  "#print-photo-button",
);

if (printButton) {
  let printInFlight = false;

  const finishPrint = (): void => {
    printInFlight = false;
    printButton.disabled = false;
    printButton.removeAttribute("aria-busy");
    printButton.innerHTML = 'พิมพ์อีกครั้ง <span aria-hidden="true">↗</span>';
  };

  const startPrint = (): void => {
    if (printInFlight) {
      return;
    }
    printInFlight = true;
    printButton.disabled = true;
    printButton.setAttribute("aria-busy", "true");
    printButton.textContent = "กำลังเตรียมพิมพ์…";
    globalThis.addEventListener("afterprint", finishPrint, { once: true });
    globalThis.print();
  };

  printButton.addEventListener("click", startPrint);
}
