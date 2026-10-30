"""Component weights of the quality score and the SCORING_WEIGHTS_JSON override.

The defaults are the documented 2.1.0 constants (reasoned, not calibrated:
docs/SURF_SCORING.md section 3). SCORING_WEIGHTS_JSON may override any of them, e.g.
``{"wind": 0.35, "tide": 0.1}``: keys must be component names and values finite numbers,
positive, or 0 to leave a component out of the score; components it does not name keep
their default. ``wave_size`` must stay positive: it is the one component every hour has, so
every hour keeps a weighted component. The score renormalises the weights over the
components whose inputs exist, so the weights need not sum to 1.

When the effective weights differ from the defaults, the stored algorithm version carries a
tag of them (``2.2.0+w1a2b3c4d``), so scores made with other weights are never mixed with
the default ones and a weights change rescores the served runs
(``predict.outdated_authoritative_runs``).

This module imports nothing from the app: the settings validate SCORING_WEIGHTS_JSON with
:func:`validate_weights` at start-up (an invalid value stops the process with a validation
error naming the setting).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

DEFAULT_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "swell_direction": 0.20,
        "swell_period": 0.20,
        "wave_size": 0.20,
        "wind": 0.30,
        "tide": 0.05,
        "consistency": 0.05,
    }
)
COMPONENT_NAMES = tuple(DEFAULT_WEIGHTS)
SETTING = "SCORING_WEIGHTS_JSON"


# The component every hour has (the breaking-height fit): its weight must stay positive.
ALWAYS_SCORED = "wave_size"


def validate_weights(raw: Mapping[str, Any]) -> dict[str, float]:
    """The effective weights for an override (defaults for the components it leaves out).
    ValueError when a key is not a component name, a value is not a finite number >= 0, or
    ``wave_size`` would be 0 (the message names the setting and the key)."""
    unknown = sorted(str(k) for k in raw if k not in DEFAULT_WEIGHTS)
    if unknown:
        raise ValueError(
            f"{SETTING} names unknown components {unknown}; the components are "
            f"{list(COMPONENT_NAMES)}"
        )
    out = dict(DEFAULT_WEIGHTS)
    for name, value in raw.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{SETTING}[{name!r}] must be a number >= 0")
        out[str(name)] = float(value)
    if out[ALWAYS_SCORED] <= 0:
        raise ValueError(
            f"{SETTING}[{ALWAYS_SCORED!r}] must stay above 0: every hour scores the wave size"
        )
    return out


def effective_weights(overrides: Mapping[str, float] | None) -> dict[str, float]:
    """The weights the score uses: the defaults, updated by ``overrides`` (the parsed
    SCORING_WEIGHTS_JSON, ``Settings.scoring_weights``; None = the defaults)."""
    return validate_weights(overrides) if overrides else dict(DEFAULT_WEIGHTS)


def weights_tag(weights: Mapping[str, float]) -> str | None:
    """None for the default weights, else ``w`` + 8 hex digits of their SHA-256 (the
    weights in component order, as JSON)."""
    if all(weights.get(k) == v for k, v in DEFAULT_WEIGHTS.items()) and set(weights) == set(
        DEFAULT_WEIGHTS
    ):
        return None
    canonical = json.dumps([[k, float(weights[k])] for k in COMPONENT_NAMES])
    return "w" + hashlib.sha256(canonical.encode()).hexdigest()[:8]


def tagged_version(version: str, weights: Mapping[str, float]) -> str:
    """``version`` with the weights tag (``2.2.0+w1a2b3c4d``) when they are not the
    defaults (at most 15 characters for a version like 2.2.0; the column holds 16)."""
    tag = weights_tag(weights)
    return version if tag is None else f"{version}+{tag}"
