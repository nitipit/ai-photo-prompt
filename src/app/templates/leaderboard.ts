const root = document.querySelector("component-shell")?.shadowRoot ?? document;
const scene = root.querySelector<HTMLElement>('[data-scene="leaderboard"]');
const countdown = root.querySelector<HTMLElement>(
  "[data-leaderboard-countdown]",
);
const MAX_LEADERBOARD_SECONDS = 15;
const deadline = Date.parse(scene?.dataset.leaderboardDeadline ?? "");
const currentRoundId = scene?.dataset.currentRound;
const photoPrintUrl = currentRoundId
  ? `/rounds/${currentRoundId}/photo-print`
  : undefined;

if (scene && countdown && Number.isFinite(deadline)) {
  let navigationStarted = false;
  let intervalId: number | undefined;
  let timeoutId: number | undefined;

  const secondsUntilDeadline = (): number =>
    Math.min(
      MAX_LEADERBOARD_SECONDS,
      Math.max(0, Math.ceil((deadline - Date.now()) / 1000)),
    );

  const navigateToReady = (): void => {
    if (navigationStarted) {
      return;
    }
    navigationStarted = true;
    if (intervalId !== undefined) {
      globalThis.clearInterval(intervalId);
    }
    if (timeoutId !== undefined) {
      globalThis.clearTimeout(timeoutId);
    }
    location.assign(photoPrintUrl ?? "/");
  };

  const updateCountdown = (): void => {
    const secondsRemaining = secondsUntilDeadline();
    countdown.textContent = String(secondsRemaining);
    if (secondsRemaining === 0) {
      navigateToReady();
    }
  };

  updateCountdown();
  if (!navigationStarted) {
    intervalId = globalThis.setInterval(updateCountdown, 1000);
    timeoutId = globalThis.setTimeout(
      navigateToReady,
      Math.max(0, deadline - Date.now()),
    );
  }
}
