# Photo Prompt Technical Stack

This document records the confirmed technical direction for Photo Prompt. It
sets architectural boundaries without defining implementation details.

## Application Shape

Photo Prompt is a server-rendered, HTML-first multi-page web application. Each
game scene is a separate page and the visible scene content uses a 16:9
viewport. Swup provides smooth transitions between pages as a progressive
enhancement; it does not own game state.

## Backend and Rendering

- **FastAPI** serves the application and owns server-side game flow.
- **Jinja2** renders the HTML pages and scene composition.
- The application should remain a focused kiosk game, not a SPA or a large
  management platform.

## UI Runtime

- **Web Components** provide focused, reusable UI units.
- **Adapter** provides isolated component styling and CSS authored in
  TypeScript/JavaScript.
- **Arrow JS** provides lightweight reactive behavior inside components where
  stateful browser interaction is needed.
- The reference pattern is the local `adapter-frontend-system` repository.

Pages compose components; a page is not required to become one large custom
element.

## Data and Object Boundaries

- **ShelfDB** is the persistence layer. It provides a simple Python-friendly
  key-value model with transactions and asyncio-compatible access.
- **Dictify** defines and validates mapping-shaped application objects and JSON-
  like documents.
- Dictify controls object shape at application boundaries; ShelfDB stores the
  resulting game data.

## Build Direction

Follow the reference frontend-system pattern for browser delivery:

- **Deno** owns the development workflow and tasks.
- **esbuild** bundles explicit browser dependency bridges.
- Browser page modules remain explicit ES modules.

## Architectural Boundaries

- FastAPI and Jinja2 own server-rendered page delivery.
- Swup owns navigation transitions, not persistence or game state.
- Web Components own focused UI behavior and presentation.
- Adapter owns component styling; Arrow JS owns local reactive behavior.
- Dictify owns schema and object validation; ShelfDB owns persistence.
- Avoid introducing a SPA framework, global styling system, account system, or
  broad dashboard unless the product goals later require it.

## Open Decisions

The following remain intentionally open until implementation planning:

- Exact routes and session/state transitions
- ShelfDB key and transaction layout
- Dictify model definitions
- Scoring and feedback calculation
- Image-generation provider and failure handling
- Component catalog and page module boundaries
- Exact timer durations and transition animation choices
