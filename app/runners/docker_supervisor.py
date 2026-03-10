from __future__ import annotations


class DockerSupervisor:
    def __init__(self) -> None:
        self.enabled = False

    def available(self) -> bool:
        return self.enabled
