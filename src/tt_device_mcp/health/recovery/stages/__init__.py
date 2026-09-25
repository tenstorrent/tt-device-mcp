# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The recovery ladder's five rungs: ``bridge_reset``, ``smi_reset``, ``ubb_tray``,
``host_reboot``, ``power_cycle`` — one module each, holding the real primitive
``GalaxyRecovery.escalate()`` calls to fire it. There is no dispatching base class here: an
earlier one existed only for ``tests/test_stages.py`` and a metrics cross-check, with zero
production callers, and was cut. The five names themselves (severity order 0..4) live in
:mod:`tt_device_mcp.constants` as ``STAGE_NAMES`` — a leaf, not this package, so ``metrics.py``
can import them without running this package's own ``__init__.py`` (which pulls in
``health.recovery``, which imports ``metrics``) first.
"""

from __future__ import annotations
