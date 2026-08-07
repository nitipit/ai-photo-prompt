# Photo Prompt Scoring Concept

This document records the confirmed scoring direction for Photo Prompt. It is a
product concept, not a scoring implementation or algorithm specification.

## Score Purpose

The score should reward both parts of the game:

1. Observing the target and communicating clearly through the prompt.
2. Producing an image that matches the target challenge.

A strong result should not depend only on accidental image-generation quality,
and a well-written prompt should still matter even when the generated image is
imperfect.

## Confirmed Model

- The visible score is one numeric value from **0 to 100**.
- The score uses a literal **50/50 weighting**:
  - 50% prompt quality
  - 50% generated-image match
- Every level group uses the same 0–100 score formula.
- Level groups use different challenges and expected criteria rather than
different visible score scales or formulas.
- Leaderboards show only players in the current school-level group.

## Result Feedback

Result / Feedback shows the single numeric score with two or three concise
strengths or gaps. Feedback should help the player notice what the prompt did
well or what it missed without becoming a long lesson or detailed analysis.

## Product Boundaries

- Scoring should support prompt literacy, not just image similarity.
- The game should remain understandable to students from ป.1–ม.6.
- Level differences should affect challenge expectations while keeping the
  visible score comparable within each group.
- The leaderboard should reinforce comparison and shared learning without
  requiring accounts or a combined cross-level ranking.

## Open Decisions

The following remain open until scoring design continues:

- The exact prompt-quality criteria
- The exact image-match criteria
- How challenge-specific criteria are represented
- Empty-prompt and incomplete-result handling
- Tie-breaking rules
- Image-generation failure behavior
