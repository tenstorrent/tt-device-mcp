# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
#
# SPDX-License-Identifier: Apache-2.0
"""A fabric pass that exits 77 keeps the validator's first error line in its message.

An uncaught exception prints "terminate called after throwing an instance of '<type>'" on the line
above "  what():  <cause>". Every reason picker took the first match in line order, so a validator
that died on a missing 1G hugepage pool was logged with the exception type and no cause. Runs the
wrapper script against a stand-in validator: no device is opened.
"""

import subprocess
from pathlib import Path

import pytest

from tt_device_mcp.constants import FABRIC_CHECK_CANNOT_CHECK_RC
from tt_device_mcp.health import monitor as health_monitor_mod
from tt_device_mcp.health.monitors import fabric as fabric_mod
from tt_device_mcp.health.monitors.fabric import classify, first_reason

# Fork-heavy deploy-script laps: one xdist worker runs them all (pyproject: --dist loadgroup).
pytestmark = pytest.mark.xdist_group("forks")

_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "tt-device-fabric-check.sh"

_UNCAUGHT = (
    "2026-10-08 20:44:38.1 | info | Device | Opening 32 chips\n"
    "terminate called after throwing an instance of 'tt::umd::error::UmdException<RuntimeError>'\n"
    "  what():  Hugepages are not allocated for channel: 0\n"
    "Aborted (core dumped)\n"
)
_LOGGED = (
    "2026-10-08 20:44:38.1 | info     | Device | Opening 32 chips\n"
    "2026-10-08 20:44:38.2 | critical | Always | TT_THROW @ sysmem.cpp:362: IOMMU is required\n"
    "backtrace:\n"
    " --- /opt/x/libtt_metal.so(+0x1234)\n"
)
_NO_ERROR_LINE = "banner\nsomething odd\nlast words\n"
# metal's TT_FATAL: the what() header names only the source line and the failed condition; the
# message itself comes two lines down, under "info:" (as run_cluster_validation printed it when a
# route was missing on a galaxy).
_TT_FATAL = (
    "2026-10-14 08:55:40.1 | info     | Device | Opening 32 chips\n"
    "terminate called after throwing an instance of 'std::runtime_error'\n"
    "  what():  TT_FATAL @ /opt/v/tt_metal/fabric/fabric.cpp:173: forwarding_direction.has_value()\n"
    "info:\n"
    "Could not find any forwarding direction from src (M0, D0) to dst (M0, D28)\n"
    "backtrace:\n"
    " --- /opt/v/build/lib/libtt_metal.so(+0x18ebee8)\n"
)
_TT_FATAL_REASON = (
    "what():  TT_FATAL @ /opt/v/tt_metal/fabric/fabric.cpp:173: forwarding_direction.has_value()"
    " -- Could not find any forwarding direction from src (M0, D0) to dst (M0, D28)"
)


@pytest.mark.parametrize(
    "output,expected",
    [
        (_UNCAUGHT, "what():  Hugepages are not allocated for channel: 0"),
        (_LOGGED, "| critical | Always | TT_THROW @ sysmem.cpp:362: IOMMU is required"),
        ("terminate called without an active exception\n", "terminate called without an active exception"),
        (
            "x\nfilesystem error: cannot create directory: Permission denied\n",
            "filesystem error: cannot create directory: Permission denied",
        ),
        (_NO_ERROR_LINE, None),
        (_TT_FATAL, _TT_FATAL_REASON),
        # An "info:" line that does not follow the matched line is not its message.
        ("  what():  TT_FATAL @ a.cpp:1: x\nbacktrace:\ninfo:\nunrelated\n", "what():  TT_FATAL @ a.cpp:1: x"),
    ],
)
def test_first_reason_prefers_the_cause_over_the_terminate_banner(output, expected):
    assert first_reason(output) == expected


def test_builtin_path_skip_detail_names_the_cause():
    ok, detail = classify(134, _UNCAUGHT)
    assert ok is None
    assert detail.endswith("what():  Hugepages are not allocated for channel: 0")


def test_override_path_reason_names_the_cause():
    text = _UNCAUGHT + "fabric-check: no link was tested\n"
    assert health_monitor_mod._override_reason(text) == "what():  Hugepages are not allocated for channel: 0"


def _run_wrapper(tmp_path, validator_output: str, rc: int, **extra_env):
    root = tmp_path / "root"
    root.mkdir()
    out = tmp_path / "validator-output.txt"
    out.write_text(validator_output)
    fake = tmp_path / "run_cluster_validation"
    fake.write_text(f"#!/bin/sh\ncat '{out}'\nexit {rc}\n")
    fake.chmod(0o755)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TTDEV_FABRIC_BIN": str(fake),
        "TTDEV_FABRIC_RUNTIME_ROOT": str(root),
        "TTDEV_FABRIC_CACHE": str(tmp_path / "cache"),
        "TTDEV_DISPATCH_BIN": str(tmp_path / "absent"),
        "TTDEV_PCI_DEVICES_DIR": str(tmp_path / "no-pci"),
        **extra_env,
    }
    return subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def _pci_with_pool(tmp_path, iommu_types, pages):
    """A stand-in PCI bus of Tenstorrent functions (an IOMMU group type each, None for no group), one
    foreign function, and a 1G pool holding ``pages``; the env that points the wrapper at them."""
    pci = tmp_path / "pci"
    for i, kind in enumerate(iommu_types):
        fn = pci / f"0000:{i + 1:02x}:00.0"
        fn.mkdir(parents=True)
        (fn / "vendor").write_text("0x1e52\n")
        if kind is not None:
            (fn / "iommu_group").mkdir()
            (fn / "iommu_group" / "type").write_text(kind + "\n")
    other = pci / "0000:ff:00.0"
    other.mkdir(parents=True)
    (other / "vendor").write_text("0x8086\n")
    nr = tmp_path / "nr_hugepages"
    nr.write_text(f"{pages}\n")
    return {"TTDEV_PCI_DEVICES_DIR": str(pci), "TTDEV_HUGEPAGES_1G_NR": str(nr)}


def test_wrapper_77s_on_a_short_hugepage_pool_without_running_the_validator(tmp_path):
    """Boot fills the 1G pool after the hugetlbfs mount is up with 0 pages; a pass run first dies on
    'Failed to pin pages for hugepage'. The wrapper names the cause and does not open the device."""
    env = _pci_with_pool(tmp_path, ["identity", None, "DMA-FQ"], 1)
    r = _run_wrapper(tmp_path, "ran\n", 0, **env)
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert "hugepage pool not ready: 1/2 1G pages allocated" in last
    assert "ran" not in r.stderr, "the validator must not run on a short pool"
    log = (tmp_path / "cache" / "cluster_validation_logs" / "last-run.log").read_text()
    assert "hugepage pool not ready: 1/2" in log
    assert "hugepage pool not ready" in health_monitor_mod._override_reason(r.stderr)


@pytest.mark.parametrize(
    "iommu_types,pages",
    [(["identity", None], 2), (["DMA-FQ", "DMA"], 0), (["identity"], "")],
    ids=["pool-full", "translating-iommu", "unreadable-pool"],
)
def test_wrapper_runs_the_validator_when_no_hugepage_is_owed(tmp_path, iommu_types, pages):
    r = _run_wrapper(tmp_path, "ran\n", 0, **_pci_with_pool(tmp_path, iommu_types, pages))
    assert r.returncode == 0, r.stderr
    assert "ran" in r.stderr


def test_wrapper_names_a_failed_hugepage_pin_as_the_pool_not_ready(tmp_path):
    out = (
        "terminate called after throwing an instance of 'std::runtime_error'\n"
        "  what():  Failed to pin pages for hugepage\n"
        "Aborted (core dumped)\n"
    )
    r = _run_wrapper(tmp_path, out, 134)
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert "hugepage pool not ready: what():  Failed to pin pages for hugepage" in last


def test_wrapper_saves_the_whole_validator_output_in_last_run_log(tmp_path):
    """A 77's cause can sit anywhere in the output: last-run.log keeps all of it, not a tail."""
    lines = ["  what():  the first cause"] + [f"line {i} " + "x" * 500 for i in range(400)]
    r = _run_wrapper(tmp_path, "\n".join(lines) + "\n", 134)
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    log = (tmp_path / "cache" / "cluster_validation_logs" / "last-run.log").read_text()
    for line in lines:
        assert line in log
    assert "validator did not complete a measurement (rc=134): what():  the first cause" in log


def test_wrapper_last_run_log_keeps_both_ends_past_its_cap(tmp_path):
    body = "HEAD-CAUSE\n" + "y" * 20000 + "\nTAIL-VERDICT\n"
    r = _run_wrapper(tmp_path, body, 134, TTDEV_FABRIC_LAST_RUN_MAX_BYTES="4000")
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    log = (tmp_path / "cache" / "cluster_validation_logs" / "last-run.log").read_text()
    assert "HEAD-CAUSE" in log and "TAIL-VERDICT" in log and "bytes cut" in log
    assert len(log) < 6000


@pytest.mark.parametrize("raw", ["4M", "0", "-1", "12", "1e6", "99999999999999999999"])
@pytest.mark.parametrize("rc", [0, 134])
def test_a_malformed_last_run_cap_falls_back_to_the_default(tmp_path, raw, rc):
    """A cap that is not a byte count must not turn the run into a fault (rc 1 reads as a measured
    fabric fail): the wrapper says so and keeps its verdict, with the default cap."""
    body = "HEAD-CAUSE\n" + "y" * 20000 + "\nTAIL-VERDICT\n"
    r = _run_wrapper(tmp_path, body, rc, TTDEV_FABRIC_LAST_RUN_MAX_BYTES=raw)
    assert r.returncode == (0 if rc == 0 else FABRIC_CHECK_CANNOT_CHECK_RC), r.stderr
    assert "TTDEV_FABRIC_LAST_RUN_MAX_BYTES" in r.stderr and "4194304" in r.stderr
    log = (tmp_path / "cache" / "cluster_validation_logs" / "last-run.log").read_text()
    assert body in log and "bytes cut" not in log


@pytest.mark.parametrize(
    "validator_output,cause",
    [
        (_UNCAUGHT, "what():  Hugepages are not allocated for channel: 0"),
        (_LOGGED, "TT_THROW @ sysmem.cpp:362: IOMMU is required"),
        (_NO_ERROR_LINE, "last output: last words"),
        (_TT_FATAL, _TT_FATAL_REASON),
    ],
)
def test_wrapper_77_final_line_keeps_the_validators_first_error_line(tmp_path, validator_output, cause):
    r = _run_wrapper(tmp_path, validator_output, 134)
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert "validator did not complete a measurement (rc=134)" in last
    assert cause in last
    # The broker's override path quotes the same cause from the wrapper's whole output.
    if cause.startswith("last output"):
        assert health_monitor_mod._override_reason(r.stderr) == last.strip()
    else:
        assert cause in health_monitor_mod._override_reason(r.stderr)


def test_wrapper_reports_links_missing_after_retrains_as_resettable_not_77(tmp_path):
    """Links still missing against the cabling descriptor after the validator's own retrains are a
    measured fault, never the could-not-check 77. Fails on base, which reported it as 77."""
    r = _run_wrapper(tmp_path, "Encountered unrecoverable state. Please check the system and try again.\n", 134)
    assert r.returncode not in (0, FABRIC_CHECK_CANNOT_CHECK_RC), r.stderr
    assert "fabric UNHEALTHY" in r.stderr and "-> resettable" in r.stderr


# The validator's own throw when the links it discovered are not the descriptor's: tray ids come
# from PCI bus numbers, so a moved uplink or an absent cable mismatches on every run.
_MISMATCH = (
    "2026-10-13 10:00:00.1 | info | Distributed | Opening 32 chips\n"
    "Physical Discovery found 2 missing port/cable connections:\n"
    "  PhysicalPortEndpoint{hostname=host0, tray_id=1, asic_location=3, port_type=QSFP_DD, port_id=1}\n"
    "  PhysicalPortEndpoint{hostname=host0, tray_id=2, asic_location=4, port_type=QSFP_DD, port_id=2}\n"
    "Physical Discovery found 1 extra port/cable connections:\n"
    "  PhysicalPortEndpoint{hostname=host0, tray_id=3, asic_location=3, port_type=QSFP_DD, port_id=1}\n"
    "terminate called after throwing an instance of 'std::runtime_error'\n"
    "  what():  Connection mismatch detected. Check the log above for details.\n"
    "Aborted (core dumped)\n"
)
_UNHEALTHY = "| critical | Always | TT_THROW: Encountered unhealthy ethernet connections, listed above\n"


def test_a_topology_mismatch_is_a_skip_not_a_measurement():
    ok, detail = classify(134, _MISMATCH)
    assert ok is None
    assert detail.startswith("topology mismatch: 2 missing, 1 extra port connections")


def test_an_ok_carrying_the_topology_marker_stays_ok_and_names_the_mismatch():
    marker = fabric_mod.topology_marker("/x/desc.textproto", 2, 1)
    ok, detail = classify(0, "fabric healthy\n" + marker + "\n")
    assert ok is True
    assert marker in detail


def _run_wrapper_topology(tmp_path, rerun_output: str, rerun_rc: int):
    """The wrapper with a descriptor set: the stand-in validator mismatches whenever it is handed the
    descriptor, and prints ``rerun_output`` (exit ``rerun_rc``) on the run without it."""
    root = tmp_path / "root"
    root.mkdir()
    desc = tmp_path / "desc.textproto"
    desc.write_text("# cabling\n")
    (tmp_path / "mismatch.txt").write_text(_MISMATCH)
    (tmp_path / "rerun.txt").write_text(rerun_output)
    calls = tmp_path / "calls"
    fake = tmp_path / "run_cluster_validation"
    fake.write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> '{calls}'\n"
        f"case \"$*\" in *cabling*) cat '{tmp_path / 'mismatch.txt'}'; exit 134 ;; esac\n"
        f"cat '{tmp_path / 'rerun.txt'}'\nexit {rerun_rc}\n"
    )
    fake.chmod(0o755)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TTDEV_FABRIC_BIN": str(fake),
        "TTDEV_FABRIC_RUNTIME_ROOT": str(root),
        "TTDEV_FABRIC_CACHE": str(tmp_path / "cache"),
        "TTDEV_FABRIC_DESCRIPTOR": str(desc),
        "TTDEV_DISPATCH_BIN": str(tmp_path / "absent"),
        "TTDEV_PCI_DEVICES_DIR": str(tmp_path / "no-pci"),
    }
    r = subprocess.run(["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    return r, calls.read_text().splitlines(), desc


def test_wrapper_reruns_a_topology_mismatch_without_the_descriptor_and_ends_on_the_marker(tmp_path):
    r, calls, desc = _run_wrapper_topology(tmp_path, "all links ok\n", 0)
    assert r.returncode == 0, r.stderr
    assert len(calls) == 2
    assert "--cabling-descriptor-path" in calls[0]
    assert "cabling" not in calls[1]
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert f"TOPOLOGY MISMATCH vs {desc}: 2 missing, 1 extra port connections" in last
    # The broker reads the same marker off the wrapper's output.
    ok, detail = classify(0, r.stderr)
    assert ok is True and "TOPOLOGY MISMATCH" in detail


def test_wrapper_topology_rerun_that_measures_bad_links_is_unhealthy(tmp_path):
    r, calls, _ = _run_wrapper_topology(tmp_path, _UNHEALTHY, 1)
    assert r.returncode == 1, r.stderr
    assert len(calls) == 2


def test_wrapper_topology_rerun_that_cannot_measure_is_77_and_says_so(tmp_path):
    r, calls, _ = _run_wrapper_topology(tmp_path, _UNCAUGHT, 134)
    assert r.returncode == FABRIC_CHECK_CANNOT_CHECK_RC, r.stderr
    last = [ln for ln in r.stderr.splitlines() if ln.strip()][-1]
    assert "topology mismatch, traffic-only rerun: what():  Hugepages are not allocated" in last
