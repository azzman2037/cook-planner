# Cook Planner for Home Assistant

Plan, forecast and track low-and-slow cooks. Pick the meat, cut, weight and the
time you want to eat; Cook Planner builds a phase-by-phase schedule, works back
to a **start-by** time, then tracks the live probe against the planned curve and
tells you what to do next (wrap, raise or drop the pit, pull, rest).

It works with any smoker that exposes a pit temperature (climate or sensor) and
meat-probe temperature sensors. Built for a Green Mountain Grills Jim Bowie via
the [GMG integration](https://github.com/azzman2037/Green-Mountain-Grills), forked from HallyAus.

**Status: v0.2.** Plans, tracks and (optionally) sets the pit temperature at each
step. Ignition always stays with you.

## Cuts (starting profiles)

| Meat | Cuts |
|---|---|
| Chicken | Thigh (bone-in), Thigh fillet, Thigh fillet - quick (hot & fast, 30 min), Breast, Whole (butterflied), Whole, **Spatchcock herb-butter** (recipe 1.1), **Smoked & seared bone-in thighs** (1.2) |
| Beef brisket | Point end (deckle), Flat, Whole (packer) |
| Pork | Shoulder (bone-in), **Pulled pork, Boston butt** (2.1), **Spare ribs 3-2-1** (2.2), **Rolled roast with crackling** (2.3), **Rolled shoulder, pulled** (2.4) |
| Lamb | Shoulder (bone-in), **Rack, reverse-seared** (3.1), **8-hour pulled shoulder** (3.2) |
| Sausages | **Slow-smoked snags & brats** (4.1) |
| Vegetables | **Hasselback potatoes** (5.1), **Balsamic veg medley** (5.2) |

### Staged recipes (v0.2)
Recipes from the GMG Master Pitmaster Guide are profiles with a `stages` list. Each
stage holds a pit temperature until its trigger - probe 1 reaches a temperature
(`probe_c`), a timer runs out (`minutes`), the pit reaches heat (`pit_c`) or the
final pull (`pull`, probe 2 included when set). Stages carry an `enter` instruction
and optional `prompts` (`at_min`, `every_min` + `from_min`, `at_probe_c`). A
`timer_from_pit` stage starts its clock only once the grill is within 10C of the
set temperature. Timed recipes (`"probe": false`) need no probe; a probe recipe
never calls the pull on time alone.

A recipe can also give `time.ref_thickness_cm`. When a Thickness is entered, it
alone sets the time of the scaled steps, `(t / ref_t) ** 1.5` with the ratio held
to 0.5-2: a rolled roast cooks by the diameter of the roll, not its length. The
two rolled pork recipes use it (reference 12 cm); enter the tied roll's diameter.

Every step change and prompt fires a `cook_planner_prompt` event
(`title`, `message`, `kind`, `key`) for notifications. **Next step** moves a recipe
on by hand. With **Auto pit temperature** on, each step's pit is sent to the
grill when the step starts (never ignition, never mid-step, so manual tweaks stick).

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
