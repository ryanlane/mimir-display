from enum import Enum


class CommandType(str, Enum):
    ASSIGN = "assign"
    REFRESH = "refresh"
    REGISTER = "register"
    READY = "ready"
    REG_COMPLETE = "registration_complete"
    DISPLAY_IMAGE = "display_image"

STATUS_QOS = 1
EVENTS_QOS = 0
COMMANDS_QOS = 1
HEARTBEAT_INTERVAL_DEFAULT = 30
