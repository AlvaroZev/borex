from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from borex.config import ROOT_DIR

ALERT_CONFIG_PATH = ROOT_DIR / "data" / "alert_config.json"

_SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}


@dataclass
class AlertDeliveryConfig:
    enabled: bool = False
    webhook_url: str = ""
    slack_webhook_url: str = ""
    min_severity: str = "warn"

    def to_dict(self) -> dict:
        d = asdict(self)
        # Never expose full URLs in logs from here; API masks on read if needed
        return d


def load_alert_config() -> AlertDeliveryConfig:
    env_url = os.environ.get("BOREX_WEBHOOK_URL", "").strip()
    env_slack = os.environ.get("BOREX_SLACK_WEBHOOK_URL", "").strip()
    if ALERT_CONFIG_PATH.is_file():
        raw = json.loads(ALERT_CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = AlertDeliveryConfig(**{k: v for k, v in raw.items() if k in AlertDeliveryConfig.__dataclass_fields__})
    else:
        cfg = AlertDeliveryConfig()
    if env_url:
        cfg = AlertDeliveryConfig(
            enabled=True,
            webhook_url=env_url,
            slack_webhook_url=cfg.slack_webhook_url or env_slack,
            min_severity=cfg.min_severity,
        )
    elif env_slack:
        cfg = AlertDeliveryConfig(
            enabled=True,
            webhook_url=cfg.webhook_url,
            slack_webhook_url=env_slack,
            min_severity=cfg.min_severity,
        )
    return cfg


def save_alert_config(cfg: AlertDeliveryConfig) -> None:
    ALERT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")


def _meets_severity(severity: str, minimum: str) -> bool:
    return _SEVERITY_RANK.get(severity, 0) >= _SEVERITY_RANK.get(minimum, 1)


def _post_json(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status >= 400:
            raise urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, None)


def _slack_payload(severity: str, code: str, message: str, session_id: str, detail: dict) -> dict:
    emoji = {"critical": ":rotating_light:", "warn": ":warning:", "info": ":information_source:"}.get(
        severity, ":bell:"
    )
    text = f"{emoji} *Borex {severity.upper()}* — `{code}`\n{message}\nSession: `{session_id}`"
    if detail:
        text += f"\n```{json.dumps(detail, indent=2)[:500]}```"
    return {"text": text}


def dispatch_alert(
    session_id: str,
    *,
    severity: str,
    code: str,
    message: str,
    detail: dict[str, Any] | None = None,
    alert_id: int | None = None,
) -> dict[str, Any]:
    """Send alert to configured webhook(s). Failures are swallowed and returned in result."""
    cfg = load_alert_config()
    if not cfg.enabled:
        return {"sent": False, "reason": "disabled"}
    if not _meets_severity(severity, cfg.min_severity):
        return {"sent": False, "reason": "below_min_severity"}

    detail = detail or {}
    body = {
        "source": "borex",
        "session_id": session_id,
        "severity": severity,
        "code": code,
        "message": message,
        "detail": detail,
        "alert_id": alert_id,
    }
    results: dict[str, Any] = {"sent": False, "targets": []}

    urls: list[tuple[str, str]] = []
    if cfg.webhook_url:
        urls.append(("webhook", cfg.webhook_url))
    if cfg.slack_webhook_url and cfg.slack_webhook_url != cfg.webhook_url:
        urls.append(("slack", cfg.slack_webhook_url))

    for name, url in urls:
        try:
            if "hooks.slack.com" in url or name == "slack":
                _post_json(url, _slack_payload(severity, code, message, session_id, detail))
            else:
                _post_json(url, body)
            results["targets"].append({"name": name, "ok": True})
            results["sent"] = True
        except Exception as exc:
            results["targets"].append({"name": name, "ok": False, "error": str(exc)})

    return results


def send_test_alert(session_id: str = "test") -> dict[str, Any]:
    return dispatch_alert(
        session_id,
        severity="warn",
        code="test",
        message="Borex alert delivery test — webhook configured correctly",
        detail={"test": True},
    )


def dispatch_pipeline_digest(report: dict[str, Any]) -> dict[str, Any]:
    """Send pipeline run summary to webhooks (always if enabled; ignores min_severity)."""
    cfg = load_alert_config()
    if not cfg.enabled:
        return {"sent": False, "reason": "disabled"}

    run_id = report.get("run_id", "pipeline")
    status = report.get("status", "unknown")
    audit = report.get("audit") or {}
    screen = report.get("screen") or {}
    promoted = int(screen.get("promoted_count", 0))
    revals = report.get("revalidations") or []
    retired = report.get("retired") or []
    decayed = sum(1 for r in revals if r.get("verdict") == "decayed")
    tick_ok = sum(1 for t in report.get("paper_ticks") or [] if t.get("ok"))
    tick_fail = sum(1 for t in report.get("paper_ticks") or [] if not t.get("ok"))

    lines = [
        f"*Borex pipeline* — `{status}`",
        f"Run: `{run_id}`",
    ]
    if audit:
        lines.append(f"Audit: {audit.get('ok', 0)} ok, {audit.get('warn', 0)} warn, {audit.get('fail', 0)} fail")
    if screen:
        lines.append(
            f"Screen: {promoted} promoted / {screen.get('total', 0)} jobs "
            f"({screen.get('error_count', 0)} errors)"
        )
    if report.get("paper_ticks"):
        lines.append(f"Paper ticks: {tick_ok} ok, {tick_fail} failed")
    if revals:
        lines.append(f"Revalidate: {len(revals)} sessions, {decayed} decayed")
    if retired:
        lines.append(f"Retired: {len(retired)} session(s)")
    if report.get("errors"):
        lines.append(f"Errors: {'; '.join(report['errors'][:3])}")
    if report.get("stopped_at"):
        lines.append(f"Stopped at: {report['stopped_at']}")

    message = "\n".join(lines)
    severity = "critical" if status == "failed" else "warn" if status == "partial" else "info"

    body = {
        "source": "borex",
        "session_id": "pipeline",
        "severity": severity,
        "code": "pipeline_digest",
        "message": message,
        "detail": report,
    }
    results: dict[str, Any] = {"sent": False, "targets": []}

    urls: list[tuple[str, str]] = []
    if cfg.webhook_url:
        urls.append(("webhook", cfg.webhook_url))
    if cfg.slack_webhook_url and cfg.slack_webhook_url != cfg.webhook_url:
        urls.append(("slack", cfg.slack_webhook_url))

    for name, url in urls:
        try:
            if "hooks.slack.com" in url or name == "slack":
                _post_json(url, {"text": message})
            else:
                _post_json(url, body)
            results["targets"].append({"name": name, "ok": True})
            results["sent"] = True
        except Exception as exc:
            results["targets"].append({"name": name, "ok": False, "error": str(exc)})

    return results
