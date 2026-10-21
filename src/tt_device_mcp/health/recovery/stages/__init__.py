# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The recovery ladder's four rungs: ``bridge_reset``, ``smi_reset``, ``host_reboot``,
``power_cycle``. The modules here hold the real primitives ``GalaxyRecovery.escalate()`` calls
to fire them (the mesh-wide ``smi_reset`` is built in ``galaxy.py``). There is no BMC tray
re-power rung: it re-powered every tray, not one, and could take host storage down with it. There is no dispatching base class here: an
earlier one existed only for ``tests/test_stages.py`` and a metrics cross-check, with zero
production callers, and was cut. The four names themselves (severity order 0..3) live in
:mod:`tt_device_mcp.constants` as ``STAGE_NAMES`` — a leaf, not this package, so ``metrics.py``
can import them without running this package's own ``__init__.py`` (which pulls in
``health.recovery``, which imports ``metrics``) first.
"""

from __future__ import annotations
