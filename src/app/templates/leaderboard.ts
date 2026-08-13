const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const countdown = root.querySelector<HTMLElement>(
  "[data-leaderboard-countdown]",
);

let navigationStarted = false;
let secondsRemaining = Number(countdown?.dataset.leaderboardCountdown ?? "15");

function navigateToReady(): void {
  if (navigationStarted) {
    return;
  }
  navigationStarted = true;
  location.assign("/");
}

globalThis.setInterval(() => {
  secondsRemaining = Math.max(0, secondsRemaining - 1);
  if (countdown) {
    countdown.textContent = String(secondsRemaining);
  }
  if (secondsRemaining === 0) {
    navigateToReady();
  }
}, 1000);

globalThis.setTimeout(navigateToReady, 15_000);
