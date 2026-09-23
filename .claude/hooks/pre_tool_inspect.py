#!/usr/bin/env python3
"""
Claude Code PreToolUse hook.
Inspects Bash commands and file paths for dangerous patterns and
prompt-injection-style content. Exit 2 to block (stderr -> Claude).
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone


SHELL_TOOLS = ("Bash", "PowerShell", "Shell")
SENSITIVE_PATH_PATTERNS = [
    r"(^|/)\.env($|\.|/)",
    r"(^|/)\.ssh/",
    r"(^|/)\.aws/",
    r"(^|/)\.gnupg/",
    r"\.pem$",
    r"\.key$",
    r"(^|/)id_rsa($|\.pub$)",
    r"(^|/)id_ed25519($|\.pub$)",
    r"(^|/)credentials(\.json|\.yaml|\.yml)?$",
]
SECRET_PATH_PATTERN = (
    r"(\.env(\.|$|\s|/)"
    r"|/\.ssh/|/\.aws/|/\.gnupg/"
    r"|\.pem(\s|$)|\.key(\s|$)"
    r"|id_rsa(\s|$|\.pub)|id_ed25519(\s|$|\.pub)"
    r"|credentials(\.|$|\s))"
)
SECRET_URL_PATTERNS = [
    r"https?://[^\s'\"]*[?#][^\s'\"]*(token|secret|api[-_]?key|password|credential)\s*=",
    r"https?://[^\s/'\":]+:[^\s/@'\"]+@",
]
# Glassworm: invisible/bidi control chars that can hide malicious content.
# Built via chr() to avoid embedding actual invisible chars in this source file.
_INVISIBLE_CODEPOINTS = (
    [0x061C]                        # Arabic Letter Mark
    + [0x00AD]                      # soft hyphen
    + list(range(0x200B, 0x2010))   # ZWSP, ZWNJ, ZWJ, LRM, RLM
    + list(range(0x202A, 0x202F))   # LRE, RLE, PDF, LRO, RLO (bidi overrides)
    + list(range(0x2060, 0x2065))   # word joiner, invisible operators
    + list(range(0x2066, 0x206A))   # LRI, RLI, FSI, PDI (bidi isolates)
    + [0xFEFF]                      # BOM / ZWNBSP
)
INVISIBLE_CHAR_RE = re.compile(
    "[" + "".join(chr(cp) for cp in _INVISIBLE_CODEPOINTS) + "]"
)

_DEFAULT_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "audit.log")


def audit_log(phase: str, tool: str, result: str, detail: str = "") -> None:
    """Append one audit record. No-ops when HOOK_NO_LOG is set or write fails."""
    if os.environ.get("HOOK_NO_LOG"):
        return
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = f"{ts} [{phase:<4}] {result:<8} {tool:<20} {detail[:120]}\n"
    try:
        log_path = os.path.abspath(os.environ.get("HOOK_LOG_PATH") or _DEFAULT_LOG_FILE)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as exc:
        print(f"[audit_log] write failed: {exc}", file=sys.stderr)


def summarize_path(path: str) -> str:
    normalized = normalize_path(path).strip()
    if not normalized:
        return ""
    parts = [part for part in normalized.split("/") if part and part != "."]
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return normalized


def summarize_command(command: str) -> str:
    tokens = re.findall(r'"[^"]*"|\'[^\']*\'|\S+', command)
    for token in tokens:
        cleaned = token.strip().strip("\"'")
        if not cleaned:
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", cleaned):
            continue
        if cleaned.lower() in {"sudo", "env", "/usr/bin/env", "command", "time"}:
            continue
        return os.path.basename(normalize_path(cleaned))
    return "shell"


def normalize_path(value: str) -> str:
    return value.replace("\\", "/")


def _invisible_chars_desc(text: str) -> str:
    found = sorted({f"U+{ord(c):04X}" for c in INVISIBLE_CHAR_RE.findall(text)})
    return ", ".join(found)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception as e:
        # Don't block on hook input failure; surface non-blocking error.
        print(f"[pre_tool_inspect] input parse error: {e}", file=sys.stderr)
        sys.exit(1)

    tool: str = data.get("tool_name", "")
    inp: dict = data.get("tool_input", {}) or {}

    def block(reason: str) -> None:
        audit_log("PRE", tool, "BLOCKED", reason)
        print(f"BLOCKED: {reason}. Remove suspicious content and retry.", file=sys.stderr)
        sys.exit(2)

    # ---- Shell command checks -------------------------------------------
    if tool in SHELL_TOOLS:
        cmd: str = inp.get("command", "") or ""
        normalized_cmd = normalize_path(cmd)

        if INVISIBLE_CHAR_RE.search(cmd):
            block(f"invisible Unicode chars in command ({_invisible_chars_desc(cmd)})")

        dangerous = [
            (r"\brm\s+-rf?\s+(/|~|\$HOME|\*)", "rm -rf against root/home/wildcard"),
            (r"\|\s*(sh|bash|zsh)\b",          "piping into a shell"),
            (r"\bgit\s+push\s+(--force|-f\b)", "git force-push"),
            (r"\bgit\s+reset\s+--hard\b",      "git reset --hard"),
            (r"\bgit\s+filter-(branch|repo)\b","git history rewrite"),
            (r"\beval\b",                      "use of eval"),
            (r"\bchmod\s+-?R?\s*777\b",        "chmod 777"),
            (r":\(\)\s*\{.*:\|:&.*\}",         "fork bomb pattern"),
            (r"\bdd\s+[^|]*\bof=/dev/",        "dd writing to device"),
            (r">\s*/dev/sd[a-z]",              "raw disk write"),
            (
                r"\bremove-item\b(?=[^\n\r]*\s-(recurse|r)\b)"
                r"(?=[^\n\r]*\s-(force|fo)\b)[^\n\r]*(?:[a-z]:/|/|~|\$HOME|\*)",
                "powershell recursive force remove",
            ),
            (r"\b(del|erase)\b(?=[^\n\r]*\s/s\b)(?=[^\n\r]*\s/q\b)", "cmd recursive quiet delete"),
            (r"\brmdir\b(?=[^\n\r]*\s/s\b)(?=[^\n\r]*\s/q\b)", "cmd recursive quiet directory delete"),
        ]
        for pat, label in dangerous:
            if re.search(pat, normalized_cmd, flags=re.IGNORECASE):
                block(f"{label}: {cmd!r}")

        readers = (
            r"\b(cat|less|more|head|tail|cp|mv|grep|awk|sed|od|xxd|base64|tar|zip|"
            r"get-content|gc|type|copy-item|move-item)\b"
        )
        if re.search(readers + r"[^|;&]*" + SECRET_PATH_PATTERN, normalized_cmd, flags=re.IGNORECASE):
            block(f"shell access to secret-like path: {cmd!r}")

    # ---- File-path checks (defense in depth for Read/Edit/Write) --------
    if tool in ("Read", "Edit", "Write"):
        path: str = inp.get("file_path") or inp.get("path") or ""
        normalized_path = normalize_path(path)
        for pat in SENSITIVE_PATH_PATTERNS:
            if re.search(pat, normalized_path, flags=re.IGNORECASE):
                block(f"sensitive file access: {path!r}")

    # ---- Content checks for Write/Edit (exfiltration + invisible chars) -
    if tool in ("Write", "Edit"):
        content = (
            inp.get("content")
            or inp.get("new_string")
            or inp.get("new_content")
            or ""
        )
        for pat in SECRET_URL_PATTERNS:
            if re.search(pat, content, flags=re.IGNORECASE):
                block("suspicious URL with credential-like data in output")
        if INVISIBLE_CHAR_RE.search(content):
            block(f"invisible Unicode chars in written content ({_invisible_chars_desc(content)})")

    # ---- Audit log for allowed operations -------------------------------
    if tool in SHELL_TOOLS:
        detail = f"cmd:{summarize_command(inp.get('command', '') or '')}"
    elif tool in ("Read", "Edit", "Write"):
        detail = f"path:{summarize_path(inp.get('file_path') or inp.get('path') or '')}"
    else:
        detail = ""
    audit_log("PRE", tool, "ALLOWED", detail)
    sys.exit(0)


if __name__ == "__main__":
    main()
