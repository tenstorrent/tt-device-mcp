<!--
SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.

SPDX-License-Identifier: Apache-2.0
-->

# Project: <name>

<!-- Fill in every section during planning (tt-device-dev, Phase 2). Every GitHub issue serves a
goal below, and the hostile reviewer scores alignment against this file. Change it only in its own
commit that names the issue or decision behind the change. -->

## Mission

<One or two sentences: what this effort exists to achieve, and for whom.>

## Goals

1. <G1: a concrete outcome>
2. <G2>

## Non-goals

- <What this effort will not do, even if it looks related.>

## Success criteria

- <Measurable checks that say the mission is met, such as "demo X passes on N300 through the queue",
  or "accuracy within Y of reference".>

## Hardware target

- Board / chips: <from `tt-smi -ls` via tt_device_exec, e.g. 1x n300 = 2 chips>
- `MESH_DEVICE`: <value; it must divide the model's KV heads>

## Environment

- tt-metal ref: <commit from `git -C tt-metal rev-parse --short HEAD`>
- Env file: `tt-device.env.yaml`
- Model / weights: <model id, HF_HOME snapshot>

## Decisions

<!-- Newest first: date, decision, why, issue. -->
