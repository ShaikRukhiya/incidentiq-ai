from __future__ import annotations

import re
from collections import Counter

SEVERITY_PATTERN = re.compile(r"\b(ERROR|WARNING|CRITICAL|INFO|DEBUG)\b", re.IGNORECASE)


def analyze_logs(raw_logs: str, services: list[str] | None = None) -> dict:
    lines = raw_logs.splitlines() if raw_logs else []
    events: list[dict] = []
    stack_traces: list[str] = []
    current_trace: list[str] = []
    seen_http_errors: list[str] = []
    error_counts = Counter()
    repeated_patterns: Counter[str] = Counter()

    for line_number, line in enumerate(lines, start=1):
        match = SEVERITY_PATTERN.search(line)
        level = match.group(1).upper() if match else "INFO"
        if "Traceback" in line or current_trace:
            current_trace.append(line)
            if line.startswith(" ") or line.startswith("\t") or line.startswith("File "):
                continue
            stack_traces.append("\n".join(current_trace).strip())
            current_trace = []
        elif "Traceback" in line:
            current_trace.append(line)

        if "ERROR" in line.upper() or "WARNING" in line.upper() or "CRITICAL" in line.upper():
            error_counts[level] += 1
            normalized = re.sub(r"\s+", " ", line.strip())
            repeated_patterns[normalized] += 1

        http_match = re.search(r"(?:HTTP\s*(?:/\s*)?(?:\d{3})|status(?:\s*=|:)?\s*(?:\d{3}))", line, re.IGNORECASE)
        if http_match:
            status_value = re.search(r"(\d{3})", http_match.group(0))
            if status_value:
                seen_http_errors.append(f"HTTP {status_value.group(1)}")

        if line:
            events.append({
                "line": line_number,
                "level": level,
                "message": line.strip(),
            })

    if current_trace:
        stack_traces.append("\n".join(current_trace).strip())

    relevant_services = []
    if services:
        relevant_services.extend(s.strip() for s in services if s.strip())
    extracted_services = {
        "api": "api",
        "gateway": "gateway",
        "auth": "auth",
        "db": "database",
        "database": "database",
        "worker": "worker",
        "web": "web",
        "frontend": "frontend",
        "cache": "cache",
    }
    for token in re.split(r"[^a-zA-Z0-9]+", " ".join(lines).lower()):
        if token in extracted_services:
            relevant_services.append(extracted_services[token])
    unique_services = list(dict.fromkeys(relevant_services))

    repeated_pattern_items = [
        {"pattern": pattern, "count": count}
        for pattern, count in repeated_patterns.most_common(5)
        if count > 1
    ]

    http_codes = []
    for status in sorted(set(seen_http_errors)):
        http_codes.append(status)

    return {
        "severity_counts": dict(error_counts),
        "error_events": [item for item in events if item["level"] in {"ERROR", "WARNING", "CRITICAL"}],
        "stack_traces": stack_traces,
        "http_errors": http_codes,
        "repeated_patterns": repeated_pattern_items,
        "service_names": unique_services,
        "summary": {
            "total_lines": len(lines),
            "error_lines": sum(1 for item in events if item["level"] in {"ERROR", "WARNING", "CRITICAL"}),
            "stack_traces_found": len(stack_traces),
        },
    }
