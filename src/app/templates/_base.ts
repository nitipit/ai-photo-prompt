import "./_components/button.js";
import { installSoundControls } from "./_sound.js";

/** Own the one lightweight Shadow DOM boundary used by the kiosk shell. */
class ComponentShell extends HTMLElement {
  connectedCallback(): void {
    if (this.shadowRoot) {
      return;
    }

    const root = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent =
      ":host { display: grid; min-height: 100vh; overflow: hidden; }";
    root.append(style);

    while (this.firstChild) {
      root.append(this.firstChild);
    }
    installSoundControls(root);
  }
}

customElements.define("component-shell", ComponentShell);
