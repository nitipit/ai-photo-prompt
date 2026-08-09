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

### Prompt Quality

Every level uses the same internal prompt-quality weighting:

- **40% clarity:** the subject and action are understandable.
- **30% specificity:** concrete words provide useful visual control.
- **20% relationship:** important subject, action, or scene relationships are
  communicated.
- **10% consistency:** the instructions do not contradict each other.

Prompt length is not a scoring criterion. A short, clear prompt can score very
highly, especially for ป.1–ป.3. Older levels use more complex challenge anchors
and relationships rather than a different prompt-quality formula.

### Generated-image Match

Every level uses the same internal image-match weighting:

- **70% core concept:** the challenge’s main characters, actions, and
  relationships.
- **20% supporting details:** optional colors, props, setting details, or
  atmosphere.
- **10% scene coherence:** the generated image remains one readable scene with
  a compatible overall mood and composition.

Each modular challenge defines its core anchors and optional details in its
`challenge.md`. Core anchors matter most; optional details add precision but do
not replace the main concept.

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

The following remain open until scoring implementation continues:

- How challenge brief anchors are transformed into validated runtime scoring
  inputs
- Empty-prompt and incomplete-result handling
- Tie-breaking rules
- Image-generation failure behavior
