"""Tests pin the system's reduced-motion answer (ui.opening_mark.REDUCED_MOTION_ENV): a CI
runner's macOS asks for less motion. It goes through the environment, so a helper process
spawned meanwhile is pinned too."""

import os
from unittest import mock

from ui.opening_mark import REDUCED_MOTION_ENV


def pin_motion(test, reduced=False):
    """For the rest of `test`: the system asks for less motion when `reduced`, else not."""
    test.enterContext(mock.patch.dict(os.environ, {REDUCED_MOTION_ENV: "1" if reduced else "0"}))
