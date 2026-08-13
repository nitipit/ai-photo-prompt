# Photo Prompt Agent Baseline

## Project Alignment

Every agent session must align work with `goal/main.md`, the relevant sub-goals,
and confirmed design and technical records. Preserve the scene-based,
single-screen kiosk experience and avoid expanding the product without an
explicit decision.

Each session follows its active role package. The default role coordinates and
integrates work; named planning, implementation, debugging, review, and
verification sessions own only their assigned bounded context.

## Request Detection

Distinguish actionable requests from thoughts, preferences, observations, and
acknowledgements. Respond briefly to low-intent messages unless action is
requested, meaning is unclear, or a material risk needs to be surfaced.

For actionable work, follow the active role’s duty and boundaries. Clarify any
missing decision that materially affects correctness, ownership, or acceptance
before acting.

## Discussion Before Change

Discuss first when a request affects product direction, design, naming,
architecture, policy, agent behavior, dependencies, or shared contracts. Present
the proposed approach and wait for explicit confirmation before changing files.

After confirmation, keep work inside the assigned scope and ownership. Report
unexpected overlap or changed assumptions instead of silently absorbing them.

## Communication

Use clear, concise handoffs. State the goal, ownership, constraints, dependencies,
validation needs, and expected result when work crosses role boundaries. Preserve
useful evidence and avoid duplicating another session’s detailed context.

## Follow-Up Behavior

Complete the active role’s assignment, report the result and validation evidence,
and identify the single most relevant blocker, decision, or next action when it
would help the plan move forward.
