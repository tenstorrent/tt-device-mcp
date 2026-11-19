#!/usr/bin/env python3
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Retired: the AICLK ceiling step was removed and nothing runs this.

Kept only because updaters from before the removal stage this path as a required file and
refuse to mark the host current without it. Delete it once no host runs such an updater.
"""

import sys

sys.exit("aiclk-ceiling: retired; nothing to do")
