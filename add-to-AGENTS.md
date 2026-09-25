# Add to AGENTS.md

Copy the content below into your project's `AGENTS.md` (or `CLAUDE.md`).

---

## Running Tests (IMPORTANT)

**NEVER run tests directly via Bash** - this causes conflicts when multiple agents
share the device. On a locked host a bare `pytest` is denied (`Permission denied`
opening `/dev/tenstorrent`); route it through the queue instead.

Always use the MCP device queue:

| Tool | Purpose |
|------|---------|
| `tt_device_job_run` | Queue and wait for completion |
| `tt_device_job_run_bg` | Queue job (non-blocking) |
| `tt_device_job_status` | Check test status/results |
| `tt_device_job_wait` | Wait for completion with log streaming |
| `tt_device_job_logs` | Get job logs |
| `tt_device_job_kill` | Stop a hung test |
| `tt_device_queue_status` | See running/queued jobs |
| `tt_device_exec` | Direct command (tt-smi, etc.) |
| `tt_device_reset` | Reset device after crash |

### Example

To run a test (blocking):
```
tt_device_job_run(
    workspace="/path/to/workspace",
    command="pytest tests/test_example.py -v"
)
```

Or queue and check later (non-blocking):
```
result = tt_device_job_run_bg(
    workspace="/path/to/workspace",
    command="pytest tests/test_example.py -v"
)
# Returns: {job_id, position, log_file, status, message}

# Later, check status or wait:
tt_device_job_status(job_id=result["job_id"])
tt_device_job_wait(job_id=result["job_id"])
```

### Guidelines
- Wait for job completion before proceeding
- If a test seems stuck (>5 min), ask the user if they want to kill it
- Multiple agents can queue tests - they run sequentially
