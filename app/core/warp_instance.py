from dataclasses import dataclass
from typing import Optional
from enum import Enum


class WarpInstanceStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class WarpInstance:
    """Represents a single Cloudflare WARP proxy instance."""

    instance_id: str
    port: int
    data_dir: str
    public_ipv4: Optional[str] = None
    public_ipv6: Optional[str] = None
    status: WarpInstanceStatus = WarpInstanceStatus.STOPPED
    created_at: Optional[str] = None
    last_started_at: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def proxy_url(self) -> str:
        """Return the SOCKS5 proxy URL for this instance."""
        return f"socks5://127.0.0.1:{self.port}"

    @property
    def public_ip(self) -> Optional[str]:
        """Return the primary public IP for backward compatibility."""
        return self.public_ipv4 or self.public_ipv6

    def to_dict(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "port": self.port,
            "data_dir": self.data_dir,
            "public_ip": self.public_ip,
            "public_ipv4": self.public_ipv4,
            "public_ipv6": self.public_ipv6,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_started_at": self.last_started_at,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WarpInstance":
        legacy_ip = data.get("public_ip")
        public_ipv4 = data.get("public_ipv4")
        public_ipv6 = data.get("public_ipv6")

        if legacy_ip and not public_ipv4 and ":" not in legacy_ip:
            public_ipv4 = legacy_ip
        if legacy_ip and not public_ipv6 and ":" in legacy_ip:
            public_ipv6 = legacy_ip

        return cls(
            instance_id=data["instance_id"],
            port=data["port"],
            data_dir=data["data_dir"],
            public_ipv4=public_ipv4,
            public_ipv6=public_ipv6,
            status=WarpInstanceStatus(data.get("status", "stopped")),
            created_at=data.get("created_at"),
            last_started_at=data.get("last_started_at"),
            error_message=data.get("error_message"),
        )
