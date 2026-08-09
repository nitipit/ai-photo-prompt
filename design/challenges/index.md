# Challenge Catalog

Each approved target is a modular challenge bundle. A bundle keeps its
human-reviewed brief beside its target image so the concept and evaluation
rules can be adjusted together.

The brief is an internal design record. The player sees only the target image;
working titles, anchors, and optional details are not shown during Challenge
Reveal.

| ID | Level | Working concept | Status |
| --- | --- | --- | --- |
| [p1-p3-01](p1-p3-01-rabbit-pancake/challenge.md) | ป.1–ป.3 | Rabbit chef and giant pancake | approved |
| [p1-p3-02](p1-p3-02-turtle-bus/challenge.md) | ป.1–ป.3 | Turtle driving a flying bus | approved |
| [p1-p3-03](p1-p3-03-upside-down-cat/challenge.md) | ป.1–ป.3 | Cat sleeping on the ceiling | approved |
| [p1-p3-04](p1-p3-04-flying-backpack/challenge.md) | ป.1–ป.3 | School backpack with wings | approved |
| [p1-p3-05](p1-p3-05-panda-banana-rocket/challenge.md) | ป.1–ป.3 | Panda on a banana rocket | approved |
| [p4-p6-01](p4-p6-01-dragon-goalkeeper/challenge.md) | ป.4–ป.6 | Dragon goalkeeper | approved |
| [p4-p6-02](p4-p6-02-robot-dinosaur/challenge.md) | ป.4–ป.6 | Robot brushing a dinosaur’s teeth | approved |
| [p4-p6-03](p4-p6-03-rain-to-balloons/challenge.md) | ป.4–ป.6 | Rain-to-balloon invention | approved |
| [p4-p6-04](p4-p6-04-duck-pirate-bathtub/challenge.md) | ป.4–ป.6 | Duck pirate bathtub adventure | approved |
| [p4-p6-05](p4-p6-05-flying-whale-amusement-park/challenge.md) | ป.4–ป.6 | Flying whale amusement park | approved |
| [m1-m3-01](m1-m3-01-escaping-shadow/challenge.md) | ม.1–ม.3 | Escaping shadow | approved |
| [m1-m3-02](m1-m3-02-weather-vending-machine/challenge.md) | ม.1–ม.3 | Weather vending machine | approved |
| [m1-m3-03](m1-m3-03-moon-repair-shop/challenge.md) | ม.1–ม.3 | Moon repair shop | approved |
| [m1-m3-04](m1-m3-04-painted-ocean-door/challenge.md) | ม.1–ม.3 | Painted ocean door | approved |
| [m1-m3-05](m1-m3-05-weather-dj/challenge.md) | ม.1–ม.3 | Weather DJ | approved |
| [m4-m6-01](m4-m6-01-escaping-ocean-painting/challenge.md) | ม.4–ม.6 | Escaping ocean painting | approved |
| [m4-m6-02](m4-m6-02-weather-fashion-runway/challenge.md) | ม.4–ม.6 | Weather fashion runway | approved |
| [m4-m6-03](m4-m6-03-sleeping-sun/challenge.md) | ม.4–ม.6 | Sleeping sun | approved |
| [m4-m6-04](m4-m6-04-gravity-on-trial/challenge.md) | ม.4–ม.6 | Gravity on trial | approved |
| [m4-m6-05](m4-m6-05-dream-hotel/challenge.md) | ม.4–ม.6 | Dream hotel | approved |

## Bundle structure

Each challenge bundle contains:

```text
challenge.md   # concept, anchors, prompt and feedback notes
target.webp   # optimized generated target image
```

Scoring implementation should later transform these briefs into structured
application records. Runtime code should not parse Markdown directly.
