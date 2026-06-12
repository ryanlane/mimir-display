from __future__ import annotations

from typing import Any, Literal, TypedDict


class DeliverySpec(TypedDict, total=False):
    type: Literal["url", "inline"]
    url: str
    content_type: str
    etag: str
    ttl_seconds: int

class ContentSpec(TypedDict, total=False):
    delivery: DeliverySpec
    metadata: dict[str, Any]

class AssignCommand(TypedDict, total=False):
    type: Literal["assign"]
    assignment_id: str
    sequence: int | None
    scene_id: str
    scene_name: str
    display: dict[str, Any]
    content: ContentSpec
    timestamp: str
    # New scheduling hints (server >= added feature):
    # update_type: "push" for real-time pushed updates, "scheduled" when client should poll.
    # refresh_interval_s: Polling interval in seconds when update_type == "scheduled"; absent/null for push.
    update_type: Literal["push", "scheduled"] | None
    refresh_interval_s: int | None

class AckEvent(TypedDict, total=False):
    type: Literal["ack"]
    assignment_id: str | None
    sequence: int | None
    ok: bool
    timestamp: str
    message: str | None
