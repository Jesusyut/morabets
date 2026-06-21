---
name: Soccer Best Lines (3-way market) rendering
description: Durable rules for mapping soccer game lines into the shared 2-way Best Lines card model
---

# Soccer Best Lines — 3-way market into a 2-way card model

The Best Lines board's shared card renderer was designed for 2-way MLB/NHL
markets. Soccer h2h is **3-way** (home / draw / away). Adapt soccer data
client-side; do NOT fork or modify the shared renderer (paying-subscriber
MLB/NHL views depend on it being untouched).

Two durable rules:
- **Favored side = max of all three outcomes (home/draw/away)**, including the
  draw. Do not reuse a backend "favored team" that only compares home vs away —
  it can disagree with the true highest outcome.
- The shared card's line-shop row renders two prices and breaks (prints "null")
  if either is missing. When feeding a 3-way pick into it, pair the favored
  outcome's price against the opposing side's price per book and include only
  books that have both.

**Why:** Keeps one card style and zero risk to the live MLB/NHL Best Lines.

**How to apply:** Build soccer picks shaped like the existing MLB picks and feed
them through the shared board; never special-case the renderers.
