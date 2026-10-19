"""
Security routes — system-level security scanning powered by the platform's
resolved analysis model.

Collects non-invasive system data (file permissions, packages, processes,
network listeners, OS config) and sends it to the resolved analysis model
for CVE analysis and security recommendations. Never reads user documents.

Scan targets: /etc, /usr, /var, system binaries, container config,
running processes, open ports, firewall rules. System only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from server.applications.middleware import requires_permission
from starlette.responses import StreamingResponse

logger = logging.getLogger("privatae.pc.security")

router = APIRouter(prefix="/api/security", tags=["security"])

SCAN_HISTORY_PATH = Path("/app/data/security-scans")

# System paths to audit (never user documents)
SYSTEM_AUDIT_PATHS = [
    "/etc/passwd", "/etc/shadow", "/etc/group", "/etc/sudoers",
    "/etc/ssh", "/etc/ssl", "/etc/crontab",
    "/app/entrypoint.sh", "/app/server",
]


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout[:8000]
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
        return f"[unavailable: {e.__class__.__name__}]"


def _collect_system_data() -> dict:
    """Collect non-invasive system information for security analysis."""
    data = {}

    # OS info
    data["os_release"] = _run(["cat", "/etc/os-release"])
    data["kernel"] = _run(["uname", "-a"])
    data["hostname"] = _run(["hostname"])

    # Installed packages
    data["packages_dpkg"] = _run(["dpkg", "-l"])
    if "[unavailable" in data["packages_dpkg"]:
        data["packages_apk"] = _run(["apk", "list", "--installed"])
    if "[unavailable" in data.get("packages_apk", "") and "[unavailable" in data["packages_dpkg"]:
        data["packages_pip"] = _run(["pip", "list", "--format=columns"])

    # Running processes
    data["processes"] = _run(["ps", "aux"])

    # Network listeners — use /proc/net/tcp if ss unavailable
    listeners = _run(["ss", "-tlnp"])
    if "[unavailable" in listeners:
        listeners = _run(["cat", "/proc/net/tcp"])
        listeners6 = _run(["cat", "/proc/net/tcp6"])
        if "[unavailable" not in listeners6:
            listeners += "\n--- IPv6 ---\n" + listeners6
    data["network_listeners"] = listeners
    data["network_connections"] = _run(["ss", "-tnp"])

    # File permissions on critical system files
    perm_lines = []
    for p in SYSTEM_AUDIT_PATHS:
        result = _run(["ls", "-la", p])
        if "[unavailable" not in result:
            perm_lines.append(f"--- {p} ---\n{result}")
    data["file_permissions"] = "\n".join(perm_lines)

    # SUID/SGID binaries
    data["suid_binaries"] = _run(
        ["find", "/usr", "/bin", "/sbin", "-perm", "-4000", "-type", "f"],
        timeout=10,
    )

    # World-writable files in system dirs
    data["world_writable"] = _run(
        ["find", "/etc", "/usr", "/var", "-perm", "-o+w", "-type", "f"],
        timeout=10,
    )

    # Container security
    data["capabilities"] = _run(["cat", "/proc/1/status"])
    data["seccomp_status"] = _run(["grep", "Seccomp", "/proc/1/status"])
    data["mounts"] = _run(["mount"])

    # SSH config
    data["ssh_config"] = _run(["cat", "/etc/ssh/sshd_config"])

    # Cron jobs
    data["crontabs"] = _run(["ls", "-la", "/etc/cron.d/"])
    data["crontab_root"] = _run(["crontab", "-l"])

    # Environment variables (redact values, show names only for leak detection)
    env_names = sorted(os.environ.keys())
    sensitive_patterns = ["KEY", "SECRET", "TOKEN", "PASS", "CRED"]
    data["env_audit"] = json.dumps({
        "total_vars": len(env_names),
        "names": env_names,
        "sensitive_detected": [
            n for n in env_names
            if any(p in n.upper() for p in sensitive_patterns)
        ],
    })

    # Firewall
    data["iptables"] = _run(["iptables", "-L", "-n"])

    return data


SCAN_SYSTEM_PROMPT = """You are a security analyst for Privatae Host, a container-based personal computer.
Your job is to analyze system data and produce a clear, actionable security report.

Rules:
- Focus on real, exploitable vulnerabilities — not theoretical noise.
- Use your knowledge of CVEs up to your training cutoff to flag known-vulnerable package versions.
- Categorize findings by severity: critical, high, medium, low, info.
- For each finding, explain in ONE plain sentence what the risk is and what to do about it.
- Write for a non-technical person. No jargon. No acronyms without explanation.
- Never suggest changes that would break the system. Be conservative.
- If the system looks well-secured, say so. Don't manufacture findings.

Respond with valid JSON matching this structure exactly:
{
  "overall_status": "secure" | "attention" | "critical",
  "summary": "One sentence overall assessment",
  "scan_time_note": "What you analyzed",
  "findings": [
    {
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "category": "packages" | "permissions" | "network" | "processes" | "configuration" | "container",
      "title": "Short title (under 60 chars)",
      "description": "Plain-language explanation of the risk",
      "recommendation": "What to do about it",
      "cve": "CVE-XXXX-XXXXX or null if not CVE-related"
    }
  ],
  "system_profile": {
    "os": "detected OS name and version",
    "package_count": number,
    "open_ports": ["list of listening ports"],
    "process_count": number,
    "container_hardening": "description of container security posture"
  }
}"""


async def _resolve_scan_route() -> dict:
    """Resolve the full route the platform routes the security analysis through.

    Uses the shared role resolver (the single read path every model-selection
    site uses) so the scan never bakes in a stale literal. There is NO fallback
    pin (2026-08-05, the no-pins rule): an unconfigured route comes back empty
    and the caller reports the scan can't run.

    CHAT (2026-09-19). This ran on the `research` role, which is gone. Reading
    the host's own collected system data and saying what is wrong with it is
    the host reasoning about its own machine — the same work as a turn where
    the user asks it that question — so it runs on the host's route rather
    than a seat the user had to fill before a scan would start at all.
    """
    try:
        from server import brain_service
        import sys as _sys
        _sys.path.insert(0, "/app/brain")
        from mastercode.inference_config import resolve_role_route
        pool = brain_service.get_pool()
        if pool:
            return await resolve_role_route(
                pool, brain_service._entity_id, "chat"
            )
    except Exception:
        logger.warning("Scan route resolve failed", exc_info=True)
    return {"provider": "", "model": "",
            "unconfigured": "no host model route — pick a provider "
                            "and model in the Orchestrator"}


async def _call_opus_analysis(system_data: dict, route: dict) -> dict:
    """Send collected system data to the resolved analysis route.

    Runs through provider_gateway.call_model like every routed caller
    (2026-08-05): the vault-typed credential for the route's provider is the
    key source (was a hand-rolled Anthropic/OpenRouter HTTP call reading env/
    secrets-file keys directly)."""
    import sys as _sys
    _sys.path.insert(0, "/app/brain")
    from mastercode.provider_gateway import call_model

    data_text = "\n\n".join(
        f"=== {key.upper().replace('_', ' ')} ===\n{val}"
        for key, val in system_data.items()
        if val and "[unavailable" not in str(val)
    )

    out = await call_model(
        [
            {"role": "system", "content": SCAN_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"Analyze this system for security issues:\n\n{data_text}"},
        ],
        temperature=0.3,
        max_tokens=8192,
        provider=route["provider"],
        provider_model=route["model"],
        base_url=route.get("base_url"),
        fallback_provider=route.get("fallback_provider"),
        fallback_model=route.get("fallback_model"),
        _source="security_scan",
    )
    if not isinstance(out, dict) or (out.get("error") and not out.get("content")):
        err = (out or {}).get("error") if isinstance(out, dict) else None
        logger.error("Security scan analysis failed: %s", (err or "no content")[:300])
        raise HTTPException(status_code=502, detail="Security analysis temporarily unavailable")
    text = out.get("content") or ""

    # Parse JSON from response
    try:
        # Handle potential markdown code blocks
        clean = text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            clean = clean.rsplit("```", 1)[0]
        return json.loads(clean)
    except json.JSONDecodeError:
        logger.warning("analysis model returned non-JSON response, wrapping")
        return {
            "overall_status": "attention",
            "summary": "Scan completed but results need manual review",
            "findings": [],
            "raw_analysis": text[:3000],
        }


def _save_scan(result: dict) -> str:
    """Persist scan result to disk. Returns scan ID."""
    SCAN_HISTORY_PATH.mkdir(parents=True, exist_ok=True)
    scan_id = f"scan-{int(time.time())}"
    result["scan_id"] = scan_id
    result["timestamp"] = int(time.time())
    path = SCAN_HISTORY_PATH / f"{scan_id}.json"
    path.write_text(json.dumps(result, indent=2))

    # Keep only last 20 scans
    scans = sorted(SCAN_HISTORY_PATH.glob("scan-*.json"), key=lambda p: p.stat().st_mtime)
    for old in scans[:-20]:
        old.unlink(missing_ok=True)

    return scan_id


def _load_scans() -> list[dict]:
    """Load scan history, newest first."""
    if not SCAN_HISTORY_PATH.exists():
        return []
    scans = []
    for p in sorted(SCAN_HISTORY_PATH.glob("scan-*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            data = json.loads(p.read_text())
            scans.append({
                "scan_id": data.get("scan_id", p.stem),
                "timestamp": data.get("timestamp", int(p.stat().st_mtime)),
                "overall_status": data.get("overall_status", "unknown"),
                "summary": data.get("summary", ""),
                "finding_count": len(data.get("findings", [])),
                "critical_count": sum(1 for f in data.get("findings", []) if f.get("severity") == "critical"),
                "high_count": sum(1 for f in data.get("findings", []) if f.get("severity") == "high"),
            })
        except Exception:
            continue
    return scans


@router.get("/status", dependencies=[Depends(requires_permission("read:incidents"))])
async def security_status():
    """Current security posture based on most recent scan."""
    scans = _load_scans()
    if not scans:
        return {
            "status": "no_scan",
            "message": "No security scan has been run yet",
            "last_scan": None,
        }
    latest = scans[0]
    return {
        "status": latest["overall_status"],
        "message": latest["summary"],
        "last_scan": latest,
    }


# A scan is ~20 system commands (up to 15 s each, _run) and then one model
# call that writes up to 8192 tokens with no overall time limit — longer than
# the 30 s the router in front of the Host gives a request
# (server/applications/long_request.py) whenever the model writes at length.
# So it runs through long_request: the result when it is ready in time, else
# 202 + a job the Security window polls at GET /api/security/jobs/{id}
# (execute:scan, the scan's own scope).
_JOB_AREA = "security-scan"


@router.post("/scan", dependencies=[Depends(requires_permission("execute:scan"))])
async def run_scan():
    """Run a full security scan (_scan), answered within the router's window
    or as a job."""
    from server.applications import long_request
    return await long_request.run(
        "scan", _scan, area=_JOB_AREA, poll="/api/security/jobs/{job}",
        message="The scan is still running — the result will appear here when it is done.")


@router.get("/jobs/{job_id}", dependencies=[Depends(requires_permission("execute:scan"))])
async def scan_job(job_id: str) -> dict:
    """A scan that outlasted its request: {status: running} | {status: done,
    result} | {status: failed, http_status, detail}."""
    from server.applications import long_request
    out = long_request.status(job_id, area=_JOB_AREA)
    if out is None:
        raise HTTPException(status_code=404, detail="no such scan job (it finished more than "
                                                    "15 minutes ago, or the Host restarted)")
    return out


async def _scan():
    """Run a full security scan. Collects system data, sends to the resolved analysis model."""
    logger.info("Security scan starting — collecting system data")
    system_data = await asyncio.get_event_loop().run_in_executor(None, _collect_system_data)

    route = await _resolve_scan_route()
    if not route.get("model"):
        # Unconfigured host model — the scan reports it can't run; there
        # is no silent default model (2026-08-05, the no-pins rule).
        raise HTTPException(status_code=503, detail=(
            route.get("unconfigured")
            or "no research route — pick a provider and model in the Orchestrator"))
    logger.info("System data collected — sending to %s/%s for analysis",
                route["provider"], route["model"])
    result = await _call_opus_analysis(system_data, route)
    result["model"] = route["model"]

    scan_id = _save_scan(result)
    logger.info("Security scan complete: %s — %s", scan_id, result.get("overall_status", "unknown"))

    return result


@router.get("/history", dependencies=[Depends(requires_permission("read:incidents"))])
async def scan_history():
    """List past scan summaries."""
    return {"scans": _load_scans()}


@router.get("/history/{scan_id}", dependencies=[Depends(requires_permission("read:incidents"))])
async def get_scan(scan_id: str):
    """Get full results of a specific scan."""
    if "/" in scan_id or ".." in scan_id:
        raise HTTPException(status_code=400, detail="Invalid scan ID")
    path = SCAN_HISTORY_PATH / f"{scan_id}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Scan not found")
    return json.loads(path.read_text())


@router.get("/processes", dependencies=[Depends(requires_permission("read:processes"))])
async def list_processes():
    """Current running processes (live, no Opus call needed)."""
    raw = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _run(["ps", "aux", "--sort=-%mem"])
    )
    lines = raw.strip().split("\n")
    if len(lines) < 2:
        return {"processes": []}
    header = lines[0]
    procs = []
    for line in lines[1:50]:
        parts = line.split(None, 10)
        if len(parts) >= 11:
            procs.append({
                "user": parts[0],
                "pid": parts[1],
                "cpu": parts[2],
                "mem": parts[3],
                "command": parts[10][:120],
            })
    return {"processes": procs}


@router.get("/network", dependencies=[Depends(requires_permission("read:network"))])
async def network_status():
    """Current network listeners and connections (live)."""
    def _collect():
        listeners = _run(["ss", "-tlnp"])
        if "[unavailable" in listeners:
            listeners = _run(["cat", "/proc/net/tcp"])
        connections = _run(["ss", "-tnp"])
        if "[unavailable" in connections:
            connections = _run(["cat", "/proc/net/tcp6"])
        return listeners, connections

    listeners_raw, connections_raw = await asyncio.get_event_loop().run_in_executor(None, _collect)
    return {
        "listeners": listeners_raw,
        "connections": connections_raw,
    }
