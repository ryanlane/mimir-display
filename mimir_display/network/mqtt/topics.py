class MqttTopicManager:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.base = f"mimir/{device_id}"
    @property
    def status(self) -> str: return f"{self.base}/status"
    @property
    def heartbeat(self) -> str: return f"{self.base}/heartbeat"
    @property
    def events(self) -> str: return f"{self.base}/evt"
    @property
    def commands(self) -> str: return f"{self.base}/cmd"
    @staticmethod
    def registry() -> str: return "mimir/registry/register"

    @property
    def pair_ack(self) -> str: return f"{self.base}/pair/ack"

    @property
    def registration_reply(self) -> str:
        # Canonical reply topic for proactive registration (spec §7.2) —
        # `reg/reply` matches the Windows client so all displays converge.
        return f"{self.base}/reg/reply"

    @staticmethod
    def pair_request() -> str: return "mimir/registry/pair"
