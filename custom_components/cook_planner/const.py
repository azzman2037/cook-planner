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
STAGES: Final = [STAGE_IDLE, STAGE_PLANNED, STAGE_PREHEAT, STAGE_COOKING, STAGE_REST]
