import { Adapter } from "../_lib/adapter.bundle.js";
import { typography } from "../_tokens/mod.js";

/** Native button wrapper; the inner button keeps browser form semantics. */
export class KioskButton extends Adapter {
  static {
    this.css = `
      display: inline-block;

      button {
        min-height: 3.5rem;
        border: 0;
        border-radius: 999px;
        padding: 0.85rem 1.45rem;
        background: var(--coral);
        color: var(--navy-950);
        font-family: ${typography.family};
        font-size: 1.15rem;
        font-weight: ${typography.weightMedium};
        letter-spacing: 0.01em;
        cursor: pointer;
        box-shadow: 0 0.75rem 1.5rem rgba(245, 112, 97, 0.2);
        transition: transform 160ms ease, background 160ms ease;
      }

      button:hover {
        background: var(--gold);
        transform: translateY(-2px);
      }

      button:focus-visible {
        outline: 3px solid var(--gold);
        outline-offset: 4px;
      }
    `;
  }
}

KioskButton.define("kiosk-button");
