"""The single model-call path. Every call is tool-less: repo content is untrusted data."""
import json
import shutil
import subprocess
import time
from typing import Any, Dict, Optional

from common import WhyLogError

DEFAULT_MODEL = "claude-opus-5-5[1m]"
# `--disallowedTools` leaves MCP tools reachable; `--tools ""` + `--strict-mcp-config` removes every tool,
# and `--safe-mode` keeps the user's CLAUDE.md/memory/plugins out of the analysis (both verified with canaries).
# `--bare` would also isolate but drops subscription auth.
ISOLATION = ["--safe-mode", "--tools", "", "--strict-mcp-config"]


def call(prompt: str, model: Optional[str] = None, timeout: int = 1800) -> Dict[str, Any]:
    """Run one non-interactive completion; returns {"text", "usage", "seconds", "model"}."""
    exe = shutil.which("claude")
    if not exe:
        raise WhyLogError("claude CLI not found on PATH", code=3)
    cmd = [exe, "-p", "--model", model or DEFAULT_MODEL, "--output-format", "json", *ISOLATION]
    started = time.time()
    try:
        proc = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WhyLogError(f"model call timed out after {timeout}s", code=3) from exc
    if proc.returncode != 0:
        raise WhyLogError(f"model call failed: {proc.stderr.decode('utf-8', 'replace')[-400:]}", code=3)
    try:
        envelope = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise WhyLogError("model call returned non-JSON envelope", code=3) from exc
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise WhyLogError(f"model reported an error: {str(envelope.get('result'))[:400]}", code=3)
    return {
        "text": envelope.get("result") or "",
        "usage": envelope.get("usage") or {},
        "cost_usd": envelope.get("total_cost_usd"),
        "seconds": round(time.time() - started, 1),
        "model": model or DEFAULT_MODEL,
    }
