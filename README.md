# Cook Planner for Home Assistant

Plan, forecast and track low-and-slow cooks. Pick the meat, cut, weight and the
time you want to eat; Cook Planner builds a phase-by-phase schedule, works back
to a **start-by** time, then tracks the live probe against the planned curve and
tells you what to do next (wrap, raise or drop the pit, pull, rest).

It works with any smoker that exposes a pit temperature (climate or sensor) and
meat-probe temperature sensors. Built for a Green Mountain Grills Jim Bowie via
the [GMG integration](https://github.com/azzman2037/Green-Mountain-Grills), forked from HallyAus.

**Status: v0.1 – plan and track only.** It recommends pit setpoints but does
not change anything on the smoker yet. Automatic setpoint control (never
ignition) comes in v0.2.

## Cuts (starting profiles)

| Meat | Cuts |
|---|---|
| Chicken | Thigh, Breast, Whole (butterflied), Whole |
| Beef brisket | Point end (deckle), Flat, Whole (packer) |
| Pork shoulder | Bone-in |
| Lamb shoulder | Bone-in |

Profiles live in `custom_components/cook_planner/profiles.json`: pit temp and
allowed adjustment band, wrap point, pull temperature, rest/hold, a reference
weight and time, and a planned probe curve. The numbers are starting points;
every finished cook is logged (`sensor.cook_planner_cook_log`) with planned vs
actual hours so the profiles can be re-fitted from your own cooks.

### Model
* Cook time = `ref_hours × (weight / ref_kg) ^ weight_exponent` (exponent < 1:
  thickness, not mass, drives low-and-slow time). An optional thickness is
  blended in.
* Wrapping compresses the post-wrap part of the curve (paper ×0.85, foil ×0.75).
* Live ETA blends pace against the planned curve with the measured rate of rise
  once past the stall. Probe readings above 290 °C are treated as unplugged.

## Entities
Inputs: `select` meat / cut / wrap, `number` weight / thickness, `datetime` serve by.
Buttons: Generate plan, Start cook, Meat on, Wrapped, Pulled, End cook.
Sensors: stage, plan (phases + planned curve in attributes), next action,
start by, planned pull, ETA (with low/high), behind schedule, progress,
recommended pit, planned probe, probe rate, cook log.

## Install
HACS → Custom repositories → this repo (Integration) → install → restart →
Settings → Devices & services → Add integration → **Cook Planner** → pick the
pit and probe entities.
