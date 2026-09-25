# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""Classification table for the fabric traffic probe: which (exit code, output) pairs are a
healthy mesh, a resettable verdict, or a check that never measured anything."""

import pytest

from tt_device_mcp.health.monitors.fabric import classify


@pytest.mark.parametrize(
    "rc,output,expected_ok",
    [
        (0, "all links healthy", True),
        (1, "Encountered unhealthy ethernet connections, listed above", False),
        (1, "waiting for active ethernet core to be ready", False),  # wedge stalls init: resettable
        (1, "Try resetting the board", False),
        (1, "Workload execution timed out after 300 seconds", False),  # stalled traffic IS a measurement
        (77, "cannot check", None),
        (1, "filesystem error: cannot create directory", None),  # did-not-measure => skip
        (None, "partial output before hang", False),  # timeout == unhealthy
    ],
)
def test_fabric_classification(rc, output, expected_ok):
    ok, detail = classify(rc, output)
    assert ok is expected_ok
