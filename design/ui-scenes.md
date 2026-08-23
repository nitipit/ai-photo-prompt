# Photo Prompt UI Scenes

This document records the confirmed product and UI concept for one fast kiosk
round. It is a design alignment document, not an implementation plan.

## Experience Model

Photo Prompt is a single-round, scene-based kiosk game. Each scene has one
clear purpose and presents only the information needed for the current moment.
The experience uses separate pages with smooth transitions between scenes.
The visible content viewport for every scene is 16:9, suitable for a laptop,
projector, or kiosk display.

## Visual Direction

The kiosk is designed for a 50-inch 4K TV and should feel like an immersive,
playful science booth. Each page uses a full-viewport composition with the
target or generated image as the visual hero. Instructions and actions live in
a dedicated bottom action band rather than overlaying the image.

The visual language is Thai-first and uses Noto Sans Thai Looped. The stage is
dark navy with teal, coral, and gold accents, plus minimal science motifs such
as sparks, stars, or camera/AI symbols. Scenes remain visually independent;
there is no shared progress indicator.

The core game loop is:

```text
Ready
→ Level Selection
→ Challenge Reveal
→ Write Prompt
→ Generating
→ Result / Feedback
→ Leaderboard
→ Ready
```

## Scenes

### 1. Ready

A separate attract screen for the next player. It invites the player to start
and offers a secondary `ดูอันดับ` action that opens the read-only leaderboard
without creating or completing a round.

### 2. Level Selection

The player or booth staff enters a display name and chooses the school-level
group. The name is optional; an empty name becomes `นิรนาม`.

### 3. Challenge Reveal

The target image is the focus. The scene shows the image with a short
instruction and no descriptive clues, category, title, or checklist.

Challenge Reveal and Write Prompt remain separate scenes.

### 4. Write Prompt

The player writes a free-form prompt. A small reference version of the target
image remains visible.

Every player receives the same fixed writing countdown. When the countdown
reaches zero, the current prompt is submitted automatically.

### 5. Generating

This is a passive waiting scene with no player controls. When the image is
ready, the generated image is revealed by itself for five seconds, then the
experience moves automatically to Result / Feedback.

The generated-image reveal is a state within this scene, not an additional
scene.

### 6. Result / Feedback

The target and generated images appear side by side. The scene shows one clear
numeric score and two or three concise strengths or gaps from the prompt.

The player's completion celebration, such as `เก่งมาก!`, belongs in this scene.
There is no separate Round Complete scene.

### 7. Leaderboard

The leaderboard shows only players in the selected school-level group. Each
entry includes the rank, name, score, generated image, and full prompt text.

The post-round leaderboard highlights the current player and returns to Ready
after 15 seconds. The read-only leaderboard opened from Ready shows the Top 4,
lets the visitor switch among all four level groups, and remains visible until
the visitor chooses to return.

Result / Feedback and Leaderboard remain separate scenes.

## Interaction Rules

- Each scene has one clear next action where a player action is needed.
- Passive scenes may advance automatically with a visible timeout.
- Meaningful player input is submitted explicitly, except when the fixed prompt
  countdown expires; then the current text is submitted automatically.
- The round should not require an account, profile, or extra form.
- The flow should remain usable as a kiosk experience without page scrolling.
- Short arcade effects reinforce interaction, countdown, completion, score, and
  recoverable failure; the game has no background music.
- Sound starts enabled with a persistent speaker control on every scene. Audio
  never carries information that is absent from the visible interface.

## Current Boundaries

- The scene model and high-level visual direction are agreed; detailed visual
  polish and component design are not yet decided.
- The current storyboard reference is `ui-scenes-storyboard-v3.svg` and its PNG
  preview. The earlier storyboard files remain historical drafts.
- This document intentionally does not define routes, data schemas, component
  APIs, scoring formulas, or implementation details.
