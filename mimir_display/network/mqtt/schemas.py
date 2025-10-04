from typing import TypedDict, Optional, Literal, Dict, Any

class DeliverySpec(TypedDict, total=False):
    type: Literal["url", "inline"]
    url: str
    content_type: str
    etag: str
    ttl_seconds: int

class ContentSpec(TypedDict, total=False):
    delivery: DeliverySpec
    metadata: Dict[str, Any]

class AssignCommand(TypedDict, total=False):
    type: Literal["assign"]
    assignment_id: str
    sequence: Optional[int]
    scene_id: str
    scene_name: str
    display: Dict[str, Any]
    content: ContentSpec
    timestamp: str
    # New scheduling hints (server >= added feature):
    # update_type: "push" for real-time pushed updates, "scheduled" when client should poll.
    # refresh_interval_s: Polling interval in seconds when update_type == "scheduled"; absent/null for push.
    update_type: Optional[Literal["push", "scheduled"]]
    refresh_interval_s: Optional[int]

class AckEvent(TypedDict, total=False):
    type: Literal["ack"]
    assignment_id: Optional[str]
    sequence: Optional[int]
    ok: bool
    timestamp: str
    message: Optional[str]
