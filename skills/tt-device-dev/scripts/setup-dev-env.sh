#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
#
# tt-device-dev: check and set up the same development environment every time, idempotently.
#
# Run from the workspace root (the folder holding tt-metal/). Prints one line per check:
#   OK         already right
#   FIXED      this run made it right
#   WOULD FIX  --check: this run would make it right
#   MISSING    this script cannot fix it; the line names the command that does
#   SKIP       does not apply here
# Exit status: 0 when nothing is MISSING (and, under --check, nothing would be fixed), else 1.
#
# It never creates a GitHub repo, never logs in, never builds tt-metal, never touches the device,
# and never overwrites a file the developer wrote (CLAUDE.md is edited only between its markers).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATES="$(dirname "$HERE")/templates"

CHECK_ONLY=0
WORKSPACE="$PWD"
REPO_DIR=""

usage() {
    cat <<'EOF'
usage: setup-dev-env.sh [--check] [--workspace DIR] [--repo-dir DIR]

  --check          report only; change nothing
  --workspace DIR  the folder holding tt-metal/ (default: current directory)
  --repo-dir DIR   the git repo holding project.md, whose GitHub issues track the work
                   (default: the workspace)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --workspace) [ $# -ge 2 ] || { usage >&2; exit 2; }; WORKSPACE="$2"; shift ;;
        --repo-dir) [ $# -ge 2 ] || { usage >&2; exit 2; }; REPO_DIR="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

WORKSPACE="$(cd "$WORKSPACE" 2>/dev/null && pwd)" || { echo "no such workspace directory" >&2; exit 2; }
REPO_DIR="${REPO_DIR:-$WORKSPACE}"
REPO_DIR="$(cd "$REPO_DIR" 2>/dev/null && pwd)" || { echo "no such repo directory" >&2; exit 2; }

MISSING_COUNT=0
PENDING_COUNT=0

report() {
    printf '%-10s %-28s %s\n' "$1" "$2" "${3:-}"
    case "$1" in
        MISSING) MISSING_COUNT=$((MISSING_COUNT + 1)) ;;
        "WOULD FIX") PENDING_COUNT=$((PENDING_COUNT + 1)) ;;
    esac
}

# Print a template without its leading SPDX comment block (and the blank lines after it): the
# header licenses the template in this repo, not the file a developer's project gets.
strip_header() {
    awk '
        NR == 1 && /^<!--/ { skip = 1 }
        skip { if (/-->/) { skip = 0; drop_blank = 1 }; next }
        drop_blank && /^[[:space:]]*$/ { next }
        { drop_blank = 0; print }
    ' "$1"
}

echo "tt-device-dev setup: workspace $WORKSPACE, repo $REPO_DIR$([ "$CHECK_ONLY" = 1 ] && echo ' (check only)')"
echo

# --- tt-device-mcp binary: same search order as deploy/tt-device-client-setup.sh ---------------
TTDEV=""
for cand in "$(command -v tt-device-mcp 2>/dev/null)" \
    "${TTDEV_ROOT:-/opt/tt-device-broker}/venv/bin/tt-device-mcp" \
    "${TT_DEVICE_MCP_INSTALL_DIR:-/tmp/tt-device-mcp-$(id -u)}/bin/tt-device-mcp"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then TTDEV="$cand"; break; fi
done
if [ -n "$TTDEV" ]; then
    report OK "tt-device-mcp binary" "$TTDEV"
else
    report MISSING "tt-device-mcp binary" \
        "install: git clone https://github.com/tenstorrent/tt-device-mcp.git && ./tt-device-mcp/install.sh"
fi

# --- broker answers ----------------------------------------------------------------------------
if [ -z "$TTDEV" ]; then
    report MISSING "broker reachable" "needs the tt-device-mcp binary first"
elif "$TTDEV" status >/dev/null 2>&1; then
    report OK "broker reachable"
else
    report MISSING "broker reachable" \
        "per-user host: $TTDEV daemon start; shared host: ask the admin to check tt-device-broker"
fi

# --- Claude Code MCP registration ----------------------------------------------------------------
if ! command -v claude >/dev/null 2>&1; then
    report SKIP "claude mcp registration" "claude CLI not on PATH (other clients: see the tt-device-mcp README)"
elif claude mcp get tt-device-mcp >/dev/null 2>&1; then
    report OK "claude mcp registration"
elif [ -z "$TTDEV" ]; then
    report MISSING "claude mcp registration" "needs the tt-device-mcp binary first"
elif [ "$CHECK_ONLY" = 1 ]; then
    report "WOULD FIX" "claude mcp registration" "claude mcp add tt-device-mcp -s user -- $TTDEV"
elif claude mcp add tt-device-mcp -s user -- "$TTDEV" >/dev/null 2>&1; then
    report FIXED "claude mcp registration" "added; restart Claude Code to load it"
else
    report MISSING "claude mcp registration" "run: claude mcp add tt-device-mcp -s user -- $TTDEV"
fi

# --- tt-metal checkout and its python_env ---------------------------------------------------------
if [ -d "$WORKSPACE/tt-metal" ]; then
    ref="$(git -C "$WORKSPACE/tt-metal" rev-parse --short HEAD 2>/dev/null || echo 'not a git checkout')"
    report OK "tt-metal checkout" "ref $ref (record it in project.md)"
    if [ -x "$WORKSPACE/tt-metal/python_env/bin/python" ]; then
        report OK "tt-metal python_env"
    else
        report MISSING "tt-metal python_env" "build tt-metal and create tt-metal/python_env (tt-metal INSTALLING.md)"
    fi
else
    report MISSING "tt-metal checkout" "ask the developer which tt-metal ref to use, then clone it into $WORKSPACE/tt-metal"
    report MISSING "tt-metal python_env" "needs the tt-metal checkout first"
fi

# --- gh -----------------------------------------------------------------------------------------
GH_OK=0
if ! command -v gh >/dev/null 2>&1; then
    report MISSING "gh CLI" "install GitHub CLI: https://cli.github.com"
elif gh auth status >/dev/null 2>&1; then
    GH_OK=1
    report OK "gh CLI" "logged in"
else
    report MISSING "gh CLI" "the developer runs: gh auth login"
fi

# --- repo and its GitHub remote -----------------------------------------------------------------
GH_REPO=""
if git -C "$REPO_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
    report OK "git repo" "$REPO_DIR"
    if [ "$GH_OK" = 1 ]; then
        GH_REPO="$(cd "$REPO_DIR" && gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
        if [ -n "$GH_REPO" ]; then
            report OK "GitHub remote" "$GH_REPO"
        else
            report MISSING "GitHub remote" "the developer creates or links a repo: gh repo create (or git remote add origin ...)"
        fi
    else
        report MISSING "GitHub remote" "needs gh logged in first"
    fi
else
    report MISSING "git repo" "the developer chooses: git init in $REPO_DIR, or pass --repo-dir"
    report MISSING "GitHub remote" "needs a git repo first"
fi

# --- workflow labels ----------------------------------------------------------------------------
LABELS="status:planned|ededed|Planned, approved, not started
status:in-progress|1d76db|Being implemented
status:in-review|fbca04|Hostile review in progress
review:changes-requested|d93f0b|Hostile reviewer requested changes
review:passed|0e8a16|Passed hostile review (both scores 8 or higher)
review:escalated|b60205|Failed review after 5 rounds of changes; needs the developer"
if [ -z "$GH_REPO" ]; then
    report MISSING "issue labels" "needs a GitHub remote first"
else
    have="$(gh label list --repo "$GH_REPO" --limit 500 --json name -q '.[].name' 2>/dev/null)"
    missing_labels=""
    while IFS='|' read -r name color desc; do
        printf '%s\n' "$have" | grep -qxF "$name" || missing_labels="$missing_labels$name|$color|$desc
"
    done <<EOF
$LABELS
EOF
    if [ -z "$missing_labels" ]; then
        report OK "issue labels"
    else
        names="$(printf '%s' "$missing_labels" | cut -d'|' -f1 | tr '\n' ' ')"
        if [ "$CHECK_ONLY" = 1 ]; then
            report "WOULD FIX" "issue labels" "create: $names"
        else
            failed=""
            while IFS='|' read -r name color desc; do
                [ -n "$name" ] || continue
                gh label create "$name" --repo "$GH_REPO" --color "$color" --description "$desc" >/dev/null 2>&1 \
                    || failed="$failed$name "
            done <<EOF
$missing_labels
EOF
            if [ -z "$failed" ]; then
                report FIXED "issue labels" "created: $names"
            else
                report MISSING "issue labels" "could not create: $failed(check repo permissions)"
            fi
        fi
    fi
fi

# --- project.md ---------------------------------------------------------------------------------
if [ -f "$REPO_DIR/project.md" ]; then
    if grep -q '^<One or two sentences' "$REPO_DIR/project.md"; then
        report OK "project.md" "present, not yet filled in (Phase 2)"
    else
        report OK "project.md"
    fi
elif [ "$CHECK_ONLY" = 1 ]; then
    report "WOULD FIX" "project.md" "create from the template"
else
    strip_header "$TEMPLATES/project.md" >"$REPO_DIR/project.md"
    report FIXED "project.md" "created from the template; fill it in during planning"
fi

# --- CLAUDE.md workflow block -------------------------------------------------------------------
BEGIN='<!-- tt-device-dev:begin -->'
END='<!-- tt-device-dev:end -->'
CLAUDE_MD="$REPO_DIR/CLAUDE.md"
want_block="$(printf '%s\n' "$BEGIN"; strip_header "$TEMPLATES/claude-md-snippet.md"; printf '%s\n' "$END")"
if [ -f "$CLAUDE_MD" ] && grep -qxF "$BEGIN" "$CLAUDE_MD"; then
    if ! grep -qxF "$END" "$CLAUDE_MD"; then
        report MISSING "CLAUDE.md block" "begin marker without end marker; fix CLAUDE.md by hand"
    else
        have_block="$(awk -v b="$BEGIN" -v e="$END" '$0 == b { p = 1 } p { print } $0 == e { p = 0 }' "$CLAUDE_MD")"
        if [ "$have_block" = "$want_block" ]; then
            report OK "CLAUDE.md block"
        elif [ "$CHECK_ONLY" = 1 ]; then
            report "WOULD FIX" "CLAUDE.md block" "refresh the block between its markers"
        else
            tmp="$CLAUDE_MD.tt-device-dev.$$"
            block_file="$tmp.block"
            printf '%s\n' "$want_block" >"$block_file"
            awk -v b="$BEGIN" -v e="$END" -v f="$block_file" '
                $0 == b { while ((getline line < f) > 0) print line; skip = 1; next }
                skip { if ($0 == e) skip = 0; next }
                { print }
            ' "$CLAUDE_MD" >"$tmp" && mv -f "$tmp" "$CLAUDE_MD"
            rm -f "$block_file" "$tmp"
            report FIXED "CLAUDE.md block" "refreshed between its markers"
        fi
    fi
elif [ "$CHECK_ONLY" = 1 ]; then
    report "WOULD FIX" "CLAUDE.md block" "add the workflow block"
else
    if [ -s "$CLAUDE_MD" ]; then printf '\n' >>"$CLAUDE_MD"; fi
    printf '%s\n' "$want_block" >>"$CLAUDE_MD"
    report FIXED "CLAUDE.md block" "added"
fi

# --- env file -----------------------------------------------------------------------------------
# An env file replaces the job environment wholesale (spec 01 I11), so it must be complete. These
# are the broker's own workspace defaults (server.py activation script), pinned so every run sees
# the same environment whatever the caller's shell exports.
ENV_FILE="$WORKSPACE/tt-device.env.yaml"
REQUIRED_KEYS="TT_METAL_HOME PYTHONPATH PYTHON_ENV_DIR TT_METAL_CACHE TT_METAL_ENV"
if [ -f "$ENV_FILE" ]; then
    absent=""
    for key in $REQUIRED_KEYS; do
        grep -qE "^${key}:" "$ENV_FILE" || absent="$absent$key "
    done
    if [ -z "$absent" ]; then
        report OK "env file" "tt-device.env.yaml"
    else
        report MISSING "env file" "tt-device.env.yaml lacks: $absent(add them; it replaces the environment)"
    fi
elif [ "$CHECK_ONLY" = 1 ]; then
    report "WOULD FIX" "env file" "write tt-device.env.yaml from the workspace layout"
else
    cat >"$ENV_FILE" <<EOF
# tt-device.env.yaml: the complete environment for device jobs from this workspace (tt-device-dev).
# Submit with: tt-device-mcp run -e tt-device.env.yaml "..."  (MCP: env="tt-device.env.yaml")
# This file REPLACES the job environment wholesale: add anything else a run needs here
# (HF_HOME, HF_HUB_OFFLINE, MESH_DEVICE, ...), not to your shell.
TT_METAL_HOME: "$WORKSPACE/tt-metal"
PYTHONPATH: "$WORKSPACE/tt-metal"
PYTHON_ENV_DIR: "$WORKSPACE/tt-metal/python_env"
TT_METAL_CACHE: "$WORKSPACE/.tt-metal-cache"
TT_METAL_ENV: "dev"
VLLM_TARGET_DEVICE: "tt"
EOF
    report FIXED "env file" "wrote tt-device.env.yaml; add HF_HOME, MESH_DEVICE etc. as the work needs"
fi

# --- .gitignore: keep the tt-metal checkout and cache out of the project repo -------------------
if [ "$REPO_DIR" = "$WORKSPACE" ] && git -C "$REPO_DIR" rev-parse --show-toplevel >/dev/null 2>&1; then
    absent=""
    for pat in /tt-metal/ /.tt-metal-cache/; do
        [ -f "$REPO_DIR/.gitignore" ] && grep -qxF "$pat" "$REPO_DIR/.gitignore" || absent="$absent$pat "
    done
    if [ -z "$absent" ]; then
        report OK ".gitignore"
    elif [ "$CHECK_ONLY" = 1 ]; then
        report "WOULD FIX" ".gitignore" "add: $absent"
    else
        if [ -s "$REPO_DIR/.gitignore" ] && [ "$(tail -c1 "$REPO_DIR/.gitignore")" != "" ]; then
            printf '\n' >>"$REPO_DIR/.gitignore"
        fi
        for pat in $absent; do printf '%s\n' "$pat" >>"$REPO_DIR/.gitignore"; done
        report FIXED ".gitignore" "added: $absent"
    fi
else
    report SKIP ".gitignore" "repo dir is not the workspace"
fi

echo
if [ "$MISSING_COUNT" -gt 0 ]; then
    echo "$MISSING_COUNT check(s) MISSING: run the commands above, then run this script again."
    exit 1
fi
if [ "$PENDING_COUNT" -gt 0 ]; then
    echo "$PENDING_COUNT check(s) would be fixed: run without --check."
    exit 1
fi
echo "Environment ready."
exit 0
