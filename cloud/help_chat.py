"""
Member help chat — OpenRouter (Claude Haiku 4.5) with weekly rate limits.

The assistant knows The Telescope Net architecture and can queue safe
config.yaml patches for the member's node agent to apply on the next poll.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from cloud import db

logger = logging.getLogger("cloud.help_chat")

WEEKLY_USER_MESSAGE_LIMIT = 5
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_DEFAULT = "anthropic/claude-haiku-4.5"

CONTACT = {
    "email": "info@thetelescope.net",
    "app_url": "https://app.thetelescope.net",
    "docs_url": "https://thetelescope.net",
    "github": "https://github.com/telescopenet",
}

#: Exactly the keys documented to the model in _PROJECT_CONTEXT below as
#: "Safe config.yaml keys you may patch" -- kept in sync with that list.
#: This is an allowlist, not a denylist: a key not in here is dropped
#: regardless of what it's named, because src/config_patch.py deep-merges
#: whatever survives _sanitize_patch straight into the node's config.yaml
#: with no further validation. A denylist on secret-sounding fragments
#: (password/api_key/secret/token/auth/credential) used to be the only
#: check, which let an adversarial or hallucinated patch through for any
#: other key -- including things like cloud.url, which would silently
#: redirect the node's authenticated cloud traffic (api_key and all) to
#: wherever the patch pointed it.
_ALLOWED_PATCH_KEYS = frozenset({
    "cloud.auto_run_plans", "cloud.plan_poll_interval", "cloud.heartbeat_interval",
    "cloud.enabled", "cloud.upload_images", "cloud.disconnect_park_timeout",
    "photometry.enabled", "safety.park_at_dawn", "safety.dawn_type",
    "safety.disconnect_timeout", "image_watcher.enabled",
    "devices.telescope.enabled", "devices.camera.enabled",
})

_CONFIG_PATCH_RE = re.compile(
    r"```config_patch\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)

_PROJECT_CONTEXT = """
The Telescope Net is a distributed network of member telescopes running the
Node Agent (Python dashboard on localhost:5173). The cloud at api.thetelescope.net
schedules nightly observation plans; nodes poll /api/v1/plan and execute when
cloud.auto_run_plans is true in config.yaml.

Common member issues:
- "Not dark enough yet": SafetyManager waits for astronomical twilight (sun below -18°).
- "Plan ready — auto-run off": set cloud.auto_run_plans: true in config.yaml.
- Fixed nodes run unattended overnight once online; portable nodes need "Start tonight" in the app.
- Node must stay connected via ALPACA to the telescope (Seestar, etc.).

Safe config.yaml keys you may patch (never secrets):
  cloud.auto_run_plans, cloud.plan_poll_interval, cloud.heartbeat_interval,
  cloud.enabled, cloud.upload_images, cloud.disconnect_park_timeout,
  photometry.enabled, safety.park_at_dawn, safety.dawn_type,
  safety.disconnect_timeout, image_watcher.enabled,
  devices.telescope.enabled, devices.camera.enabled

When a config change would help, append a fenced block exactly like:
```config_patch
{"cloud": {"auto_run_plans": true}}
```
Use deep-merge YAML paths only. One patch block per reply maximum.
""".strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _week_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


def _resolve_api_key(config: dict) -> str:
    cfg = config.get("help", {}) or {}
    return str(
        cfg.get("openrouter_api_key")
        or os.environ.get("OPENROUTER_API_KEY", "")
    ).strip()


def user_messages_used(user_id: str) -> int:
    row = db.query_one(
        """SELECT COUNT(*) AS n FROM help_chat_messages
           WHERE user_id = %s AND role = 'user' AND created_at >= %s""",
        (user_id, _week_ago()),
    )
    return int((row or {}).get("n", 0) or 0)


def remaining_messages(user_id: str) -> int:
    return max(0, WEEKLY_USER_MESSAGE_LIMIT - user_messages_used(user_id))


def _load_history(user_id: str, limit: int = 16) -> list[dict]:
    rows = db.query(
        """SELECT role, content FROM help_chat_messages
           WHERE user_id = %s ORDER BY id DESC LIMIT %s""",
        (user_id, limit),
    )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _member_nodes(user_id: str) -> list[dict]:
    return db.query(
        """SELECT n.node_id, n.telescope_model, n.status, n.last_conditions,
                  nm.display_name
           FROM nodes n
           JOIN node_members nm ON nm.node_id = n.node_id
           WHERE nm.user_id = %s""",
        (user_id,),
    )


def _parse_config_patch(text: str) -> Optional[dict]:
    match = _CONFIG_PATCH_RE.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _strip_patch_block(text: str) -> str:
    return _CONFIG_PATCH_RE.sub("", text).strip()


def _sanitize_patch(obj, path: str = "") -> dict:
    """Keep only leaves whose full dotted path is in _ALLOWED_PATCH_KEYS."""
    if not isinstance(obj, dict):
        return {}
    out: dict = {}
    for key, val in obj.items():
        full = f"{path}.{key}" if path else key
        if isinstance(val, dict):
            nested = _sanitize_patch(val, full)
            if nested:
                out[key] = nested
        elif full in _ALLOWED_PATCH_KEYS and isinstance(val, (bool, int, float, str)):
            out[key] = val
        else:
            logger.info("Help chat: blocked patch key %s (not in safe-key allowlist)", full)
    return out


def _queue_patch(user_id: str, node_id: str, patch: dict) -> Optional[int]:
    clean = _sanitize_patch(patch)
    if not clean:
        return None
    patch_id = db.execute(
        """INSERT INTO node_config_patches
               (node_id, user_id, patch_json, created_at, status)
           VALUES (%s,%s,%s,%s,'pending')""",
        (node_id, user_id, json.dumps(clean), _now()),
        returning_id=True,
    )
    return int(patch_id) if patch_id else None


def pending_patches(node_id: str) -> list[dict]:
    rows = db.query(
        """SELECT id, patch_json, created_at FROM node_config_patches
           WHERE node_id = %s AND status = 'pending'
           ORDER BY id ASC LIMIT 5""",
        (node_id,),
    )
    out = []
    for r in rows:
        try:
            patch = json.loads(r["patch_json"] or "{}")
        except json.JSONDecodeError:
            patch = {}
        out.append({
            "id": r["id"],
            "patch": patch,
            "created_at": r["created_at"],
        })
    return out


def ack_patch(patch_id: int, node_id: str, ok: bool, error: str = "") -> None:
    status = "applied" if ok else "failed"
    db.execute(
        """UPDATE node_config_patches
           SET status = %s, applied_at = %s, error = %s
           WHERE id = %s AND node_id = %s""",
        (status, _now(), error[:500], patch_id, node_id),
    )


def get_session(user_id: str) -> dict:
    history = db.query(
        """SELECT id, role, content, config_patch, node_id, created_at
           FROM help_chat_messages
           WHERE user_id = %s ORDER BY id DESC LIMIT 40""",
        (user_id,),
    )
    messages = []
    for r in reversed(history):
        entry = {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
        }
        if r.get("config_patch"):
            try:
                entry["config_patch"] = json.loads(r["config_patch"])
            except json.JSONDecodeError:
                pass
        messages.append(entry)
    return {
        "contact": CONTACT,
        "weekly_limit": WEEKLY_USER_MESSAGE_LIMIT,
        "messages_used": user_messages_used(user_id),
        "messages_remaining": remaining_messages(user_id),
        "messages": messages,
    }


def chat(user_id: str, message: str, node_id: str | None, config: dict) -> dict:
    text = (message or "").strip()
    if not text:
        raise ValueError("message required")
    if len(text) > 4000:
        raise ValueError("message too long (max 4000 characters)")

    if remaining_messages(user_id) <= 0:
        raise PermissionError(
            f"Weekly limit reached ({WEEKLY_USER_MESSAGE_LIMIT} messages per week)."
        )

    api_key = _resolve_api_key(config)
    if not api_key:
        raise RuntimeError("Help chat is not configured (OPENROUTER_API_KEY missing).")

    nodes = _member_nodes(user_id)
    if node_id:
        nodes = [n for n in nodes if n["node_id"] == node_id] or nodes
    primary = nodes[0] if nodes else None
    target_node_id = primary["node_id"] if primary else ""

    node_context = []
    for n in nodes[:3]:
        cond = db.loads(n.get("last_conditions"), {})
        node_context.append({
            "node_id": n["node_id"],
            "display_name": n.get("display_name") or n.get("telescope_model"),
            "status": n.get("status"),
            "conditions": cond,
        })

    system = (
        _PROJECT_CONTEXT
        + "\n\nMember node telemetry (from last heartbeat):\n"
        + json.dumps(node_context, indent=2)
    )

    messages = [{"role": "system", "content": system}]
    for item in _load_history(user_id):
        messages.append(item)
    messages.append({"role": "user", "content": text})

    model = str((config.get("help") or {}).get("model") or MODEL_DEFAULT)
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://thetelescope.net",
                "X-Title": "The Telescope Net Help",
            },
            json={
                "model": model,
                "messages": messages,
                "max_tokens": 1200,
                "temperature": 0.3,
            },
            timeout=45,
        )
        resp.raise_for_status()
        body = resp.json()
        reply = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        ).strip()
    except requests.RequestException as exc:
        logger.error("OpenRouter help chat failed: %s", exc)
        raise RuntimeError("Help assistant is temporarily unavailable.") from exc

    patch = _parse_config_patch(reply)
    display_reply = _strip_patch_block(reply)
    patch_id = None
    if patch and target_node_id:
        patch_id = _queue_patch(user_id, target_node_id, patch)

    db.execute(
        """INSERT INTO help_chat_messages
               (user_id, role, content, config_patch, node_id, created_at)
           VALUES (%s,'user',%s,'{}',%s,%s)""",
        (user_id, text, target_node_id, _now()),
    )
    db.execute(
        """INSERT INTO help_chat_messages
               (user_id, role, content, config_patch, node_id, created_at)
           VALUES (%s,'assistant',%s,%s,%s,%s)""",
        (
            user_id,
            display_reply,
            json.dumps(patch or {}),
            target_node_id,
            _now(),
        ),
    )

    result = {
        "reply": display_reply,
        "messages_remaining": remaining_messages(user_id),
        "weekly_limit": WEEKLY_USER_MESSAGE_LIMIT,
    }
    if patch:
        result["config_patch"] = _sanitize_patch(patch)
        result["patch_queued"] = patch_id is not None
        if patch_id:
            result["patch_id"] = patch_id
            result["patch_node_id"] = target_node_id
    return result