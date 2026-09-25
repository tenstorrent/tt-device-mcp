# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""
tt-device-mcp: MCP server for shared Tenstorrent device access.

Manages job queuing and serialized execution across multiple AI agents.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tt-device-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0.dev"  # Fallback for development
