"""Constants for Cook Planner."""
from __future__ import annotations

import logging
from typing import Final

DOMAIN: Final = "cook_planner"
LOGGER = logging.getLogger(__package__)

CONF_PIT_ENTITY: Final = "pit_entity"
CONF_PROBE_1: Final = "probe_1_entity"
CONF_PROBE_2: Final = "probe_2_entity"

STORAGE_VERSION: Final = 1
UPDATE_INTERVAL_S: Final = 60
RATE_WINDOW_MIN: Final = 15
RATE_MIN_SPAN_MIN: Final = 5
SAMPLE_KEEP_MIN: Final = 30
LOG_KEEP: Final = 200

STAGE_IDLE: Final = "idle"
STAGE_PLANNED: Final = "planned"
STAGE_PREHEAT: Final = "preheat"
STAGE_COOKING: Final = "cooking"
STAGE_REST: Final = "rest"
# Keeping warm in the smoker: after a cook (the rest happens on the grill) or on its own.
STAGE_HOLD: Final = "hold"
STAGES: Final = [STAGE_IDLE, STAGE_PLANNED, STAGE_PREHEAT, STAGE_COOKING, STAGE_REST, STAGE_HOLD]

# Keep-warm setpoint. 65C is the lowest the GMG holds (150F); 70C gives a steady margin.
KEEP_WARM_DEFAULT_C: Final = 70.0
KEEP_WARM_MIN_C: Final = 65.0
KEEP_WARM_MAX_C: Final = 95.0
# Hot-holding: meat below 60C gets an alert, re-armed once it is back to 62C.
HOLD_SAFE_C: Final = 60.0
HOLD_REARM_C: Final = 62.0

EVENT_PROMPT: Final = "cook_planner_prompt"
# Legacy (non-recipe) advice that warrants a phone notification, keyed by prefix.
NOTIFY_PREFIXES: Final = {
    "Pit is u": "Smoker up to temp",
    "Flip now": "Flip it",
    "Wrap now": "Time to wrap",
    "Pull now": "Take it off",
    "Resting:": "Check carryover",
}
