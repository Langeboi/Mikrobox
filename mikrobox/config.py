"""Settings. Everything can be overridden with MIKROBOX_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = Path("/var/lib/mikrobox")
    run_dir: Path = Path("/run/mikrobox")
    base_image: Path | None = None  # defaults to <data_dir>/images/chr.img
    dry_run: bool = False  # log host commands instead of running them
    auth_enabled: bool = True  # require an account to use the web interface and API

    max_devices: int = 128  # running routers + clients, across all labs
    max_chr_ports: int = 24  # PCI slots run out beyond this without extra buses
    max_client_ports: int = 4
    mem_reserve_mb: int = 512  # host memory that must stay free after a start

    mgmt_bridge: str = "mbcloud"  # the Cloud
    cloud_mode: str = "auto"  # auto | nat | bridged (auto: bridged if a real NIC is in the bridge)
    mgmt_net: str = "10.99.0.0/16"
    mgmt_host_ip: str = "10.99.0.1"
    mgmt_first_ip: str = "10.99.1.0"  # node N gets this address + N

    sys_net: Path = Path("/sys/class/net")
    netns_dir: Path = Path("/run/netns")
    netns_etc: Path = Path("/etc/netns")  # per-client resolv.conf lives here
    kvm_device: Path = Path("/dev/kvm")
    qemu_binary: str = "qemu-system-x86_64"
    web_ports: tuple[int, int] = (20000, 20999)  # links that open lab pages in your browser

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.base_image is None:
            self.base_image = self.data_dir / "images" / "chr.img"

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        kwargs: dict = {}
        if "MIKROBOX_DATA" in env:
            kwargs["data_dir"] = Path(env["MIKROBOX_DATA"])
        if "MIKROBOX_RUN" in env:
            kwargs["run_dir"] = Path(env["MIKROBOX_RUN"])
        if "MIKROBOX_CHR_IMAGE" in env:
            kwargs["base_image"] = Path(env["MIKROBOX_CHR_IMAGE"])
        if env.get("MIKROBOX_DRY_RUN") == "1":
            kwargs["dry_run"] = True
        if env.get("MIKROBOX_AUTH", "").lower() in ("off", "0", "false"):
            kwargs["auth_enabled"] = False
        if env.get("MIKROBOX_CLOUD_MODE") in ("auto", "nat", "bridged"):
            kwargs["cloud_mode"] = env["MIKROBOX_CLOUD_MODE"]
        if "MIKROBOX_WEB_PORTS" in env:
            lo, _, hi = env["MIKROBOX_WEB_PORTS"].partition("-")
            kwargs["web_ports"] = (int(lo), int(hi or lo))
        if "MIKROBOX_MAX_DEVICES" in env:
            kwargs["max_devices"] = min(128, int(env["MIKROBOX_MAX_DEVICES"]))
        return cls(**kwargs)
