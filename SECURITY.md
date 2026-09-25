# Security Policy

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability in tt-device-mcp, report it privately
using one of the following methods:

### Preferred: GitHub private vulnerability reporting

1. Navigate to the [Security tab](https://github.com/tenstorrent/tt-device-mcp/security)
   of this repository
2. Click **"Report a vulnerability"**
3. Fill out the report form with:
   - a description of the vulnerability
   - steps to reproduce
   - potential impact
   - any suggested fixes or mitigations (if known)

For detailed instructions, see
[GitHub's documentation](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).

### Alternative: Email

Contact Tenstorrent's Open Source Program Office at **ospo@tenstorrent.com**.

## Handling Process

1. **Report**: You submit a vulnerability report through one of the channels above
2. **Acknowledgment**: Tenstorrent will acknowledge receipt within **2 business days**
3. **Triage**: Our team assesses the issue and assigns a priority and risk level
4. **Fix development**: A fix is developed privately, with reporter feedback
   where possible
5. **Disclosure**: A security advisory is published once the fix has merged and
   released; we will coordinate disclosure timing with you

We request that you do not publicly disclose the vulnerability until a fix is
available. We will credit reporters in the advisory unless they prefer to
remain anonymous.

## Scope

This policy covers the broker daemon, the CLI, the MCP transports, and the
scripts under `deploy/`. Note that the broker's security model — peer
authentication via `SO_PEERCRED` on a unix socket, per-submitter job execution
under privilege separation, and reset gating on foreign device holders — is
part of the project's contract: a way to bypass any of those controls is a
vulnerability, please report it.

## Supported Versions

Security updates are provided for the latest release. Always run the most
recent version.
