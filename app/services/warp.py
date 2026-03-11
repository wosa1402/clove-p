import asyncio
import json
import os
import shutil
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from app.core.config import settings
from app.core.warp_instance import WarpInstance, WarpInstanceStatus


class WarpManager:
    """Singleton manager for Cloudflare WARP proxy instances."""

    _instance: Optional["WarpManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._instances: Dict[str, WarpInstance] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._register_lock = asyncio.Lock()

    # --- Lifecycle ---

    async def start(self) -> None:
        """Load persisted instances and restart running ones."""
        self._load_instances()
        for instance in self._instances.values():
            if instance.status in (
                WarpInstanceStatus.RUNNING,
                WarpInstanceStatus.STARTING,
            ):
                try:
                    await self._start_process(instance)
                    await self._wait_for_ready(instance)
                    await self._refresh_public_ips(instance)
                    instance.status = WarpInstanceStatus.RUNNING
                    instance.error_message = None
                except Exception as e:
                    logger.error(
                        f"Failed to restart WARP instance {instance.instance_id}: {e}"
                    )
                    instance.status = WarpInstanceStatus.ERROR
                    instance.error_message = str(e)
        self._save_instances()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"WarpManager started with {len(self._instances)} instance(s)")

    async def stop(self) -> None:
        """Stop all processes and save state."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        for instance_id in list(self._processes.keys()):
            await self._stop_process(instance_id)
        self._save_instances()
        logger.info("WarpManager stopped")

    # --- Core operations ---

    async def register_new_instance(self) -> WarpInstance:
        """
        Register a new WARP identity, start the tunnel, detect IP.
        Retries up to max_retries if IP is a duplicate.
        """
        async with self._register_lock:
            max_retries = settings.warp_max_register_retries
            existing_ips = {
                inst.public_ip
                for inst in self._instances.values()
                if inst.public_ip
            }

            for attempt in range(1, max_retries + 1):
                logger.info(f"WARP registration attempt {attempt}/{max_retries}")
                instance = self._create_instance_metadata()

                try:
                    await self._start_process(instance)
                    await self._wait_for_ready(instance)
                    await self._refresh_public_ips(instance)
                    public_ip = instance.public_ip

                    if public_ip in existing_ips:
                        logger.warning(
                            f"Duplicate IP {public_ip} on attempt {attempt}, retrying"
                        )
                        await self._teardown_instance(instance)
                        continue

                    # Success
                    instance.status = WarpInstanceStatus.RUNNING
                    self._instances[instance.instance_id] = instance
                    self._save_instances()
                    logger.info(
                        f"Registered WARP instance {instance.instance_id} "
                        f"on port {instance.port} with IPv4 {instance.public_ipv4 or '-'} "
                        f"and IPv6 {instance.public_ipv6 or '-'}"
                    )
                    return instance

                except Exception as e:
                    logger.error(f"Registration attempt {attempt} failed: {e}")
                    await self._teardown_instance(instance)
                    if attempt == max_retries:
                        raise RuntimeError(
                            f"Failed to register WARP instance with unique IP "
                            f"after {max_retries} attempts: {e}"
                        )

    async def remove_instance(self, instance_id: str) -> None:
        """Stop and remove a WARP instance, deleting its data directory."""
        if instance_id not in self._instances:
            raise ValueError(f"Instance {instance_id} not found")

        # Unbind from any accounts
        self._unbind_instance_from_accounts(instance_id)

        instance = self._instances[instance_id]
        await self._teardown_instance(instance)
        del self._instances[instance_id]
        self._save_instances()

    async def start_instance(self, instance_id: str) -> WarpInstance:
        """Start a stopped WARP instance."""
        instance = self._instances.get(instance_id)
        if not instance:
            raise ValueError(f"Instance {instance_id} not found")
        await self._start_process(instance)
        await self._wait_for_ready(instance)
        await self._refresh_public_ips(instance)
        instance.status = WarpInstanceStatus.RUNNING
        instance.last_started_at = datetime.now().isoformat()
        instance.error_message = None
        self._save_instances()
        return instance

    async def stop_instance(self, instance_id: str) -> WarpInstance:
        """Stop a running WARP instance."""
        instance = self._instances.get(instance_id)
        if not instance:
            raise ValueError(f"Instance {instance_id} not found")
        await self._stop_process(instance_id)
        instance.status = WarpInstanceStatus.STOPPED
        self._save_instances()
        return instance

    def get_instance(self, instance_id: str) -> Optional[WarpInstance]:
        return self._instances.get(instance_id)

    def get_all_instances(self) -> List[WarpInstance]:
        return list(self._instances.values())

    def get_proxy_url(self, instance_id: str) -> Optional[str]:
        """Get the SOCKS5 proxy URL for a running instance."""
        instance = self._instances.get(instance_id)
        if instance and instance.status == WarpInstanceStatus.RUNNING:
            return instance.proxy_url
        return None

    # --- Internal helpers ---

    def _get_warp_binary(self) -> str:
        """Resolve the path to the warp binary."""
        if settings.warp_binary_path:
            return settings.warp_binary_path

        data_path = Path(settings.data_folder) / "warp"
        if data_path.exists() and os.access(str(data_path), os.X_OK):
            return str(data_path)

        warp_in_path = shutil.which("warp")
        if warp_in_path:
            return warp_in_path

        raise FileNotFoundError(
            "WARP binary not found. Set WARP_BINARY_PATH, "
            "place binary in data_folder/warp, or ensure 'warp' is in PATH."
        )

    def _get_warp_data_dir(self) -> Path:
        """Return the base directory for all WARP instance data."""
        return Path(settings.data_folder) / "warp_instances"

    def _allocate_port(self) -> int:
        """Find next available port starting from base_port."""
        used_ports = {inst.port for inst in self._instances.values()}
        port = settings.warp_base_port
        while port in used_ports:
            port += 1
        return port

    def _create_instance_metadata(self) -> WarpInstance:
        """Create a new WarpInstance with allocated port and data dir."""
        existing_nums = []
        for iid in self._instances.keys():
            try:
                existing_nums.append(int(iid.split("_")[1]))
            except (IndexError, ValueError):
                pass
        next_num = max(existing_nums, default=0) + 1
        instance_id = f"warp_{next_num}"

        port = self._allocate_port()
        data_dir = str(self._get_warp_data_dir() / instance_id)
        os.makedirs(data_dir, exist_ok=True)

        return WarpInstance(
            instance_id=instance_id,
            port=port,
            data_dir=data_dir,
            status=WarpInstanceStatus.STARTING,
            created_at=datetime.now().isoformat(),
        )

    async def _start_process(self, instance: WarpInstance) -> None:
        """Spawn the Go binary as a subprocess."""
        binary = self._get_warp_binary()
        cmd = [
            binary,
            "run",
            "--data-dir", instance.data_dir,
            "--socks-addr", f"0.0.0.0:{instance.port}",
        ]
        logger.info(f"Starting WARP process: {' '.join(cmd)}")

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._processes[instance.instance_id] = process
        instance.status = WarpInstanceStatus.STARTING
        instance.last_started_at = datetime.now().isoformat()

        asyncio.create_task(
            self._log_process_output(instance.instance_id, process)
        )

    async def _stop_process(self, instance_id: str) -> None:
        """Gracefully stop a WARP subprocess."""
        process = self._processes.pop(instance_id, None)
        if process and process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            logger.info(f"Stopped WARP process for {instance_id}")

    async def _teardown_instance(self, instance: WarpInstance) -> None:
        """Stop process and delete data directory."""
        await self._stop_process(instance.instance_id)
        if os.path.exists(instance.data_dir):
            shutil.rmtree(instance.data_dir, ignore_errors=True)
            logger.info(f"Deleted data dir: {instance.data_dir}")

    async def _wait_for_ready(self, instance: WarpInstance) -> None:
        """Poll the SOCKS5 port until it accepts connections."""
        timeout = settings.warp_startup_timeout
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < timeout:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", instance.port),
                    timeout=2,
                )
                writer.close()
                await writer.wait_closed()
                logger.info(
                    f"WARP instance {instance.instance_id} ready on port {instance.port}"
                )
                return
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
                await asyncio.sleep(1)

        proc = self._processes.get(instance.instance_id)
        if proc and proc.returncode is not None:
            raise RuntimeError(
                f"WARP process exited with code {proc.returncode}"
            )
        raise TimeoutError(
            f"WARP instance {instance.instance_id} did not become "
            f"ready within {timeout}s"
        )

    async def _refresh_public_ips(self, instance: WarpInstance) -> None:
        """Refresh both IPv4 and IPv6 public egress addresses."""
        ipv4: Optional[str] = None
        ipv6: Optional[str] = None
        errors: List[str] = []

        try:
            ipv4 = await self._detect_ip(
                instance, settings.warp_ip_check_url, "IPv4"
            )
        except Exception as e:
            errors.append(f"IPv4: {e}")
            logger.warning(
                f"Failed to detect IPv4 for {instance.instance_id}: {e}"
            )

        try:
            ipv6 = await self._detect_ip(
                instance, settings.warp_ip_check_url_v6, "IPv6"
            )
        except Exception as e:
            errors.append(f"IPv6: {e}")
            logger.warning(
                f"Failed to detect IPv6 for {instance.instance_id}: {e}"
            )

        if not ipv4 and not ipv6:
            raise RuntimeError(
                f"Failed to detect public IPs for {instance.instance_id}: "
                + "; ".join(errors)
            )

        instance.public_ipv4 = ipv4
        instance.public_ipv6 = ipv6

    async def _detect_ip(
        self, instance: WarpInstance, check_url: str, ip_family: str
    ) -> str:
        """Detect a public egress IP through the WARP SOCKS5 proxy."""
        import httpx

        async with httpx.AsyncClient(
            proxy=instance.proxy_url, timeout=15
        ) as client:
            response = await client.get(check_url)
            response.raise_for_status()
            ip = response.text.strip()
            logger.info(
                f"Detected {ip_family} for {instance.instance_id}: {ip}"
            )
            return ip

    async def _log_process_output(
        self, instance_id: str, process: asyncio.subprocess.Process
    ) -> None:
        """Read and log subprocess stderr."""
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                logger.debug(f"[WARP:{instance_id}] {line.decode().strip()}")
        except Exception:
            pass

    async def _monitor_loop(self) -> None:
        """Background loop to check process health and auto-restart."""
        while True:
            try:
                for instance_id, instance in list(self._instances.items()):
                    if instance.status != WarpInstanceStatus.RUNNING:
                        continue
                    proc = self._processes.get(instance_id)
                    if proc and proc.returncode is not None:
                        logger.warning(
                            f"WARP {instance_id} exited unexpectedly "
                            f"(code {proc.returncode}), restarting"
                        )
                        try:
                            await self._start_process(instance)
                            await self._wait_for_ready(instance)
                            await self._refresh_public_ips(instance)
                            instance.status = WarpInstanceStatus.RUNNING
                            instance.error_message = None
                        except Exception as e:
                            instance.status = WarpInstanceStatus.ERROR
                            instance.error_message = str(e)
                            logger.error(f"Failed to restart {instance_id}: {e}")
                        self._save_instances()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in WARP monitor loop: {e}")
            await asyncio.sleep(30)

    def _unbind_instance_from_accounts(self, instance_id: str) -> None:
        """Remove proxy binding from any accounts using this instance."""
        from app.services.account import account_manager

        instance = self._instances.get(instance_id)
        if not instance:
            return
        proxy_url = instance.proxy_url
        for account in account_manager._accounts.values():
            if account.proxy_url == proxy_url:
                account.proxy_url = None
        account_manager.save_accounts()

    # --- Persistence ---

    def _save_instances(self) -> None:
        """Save instance metadata to JSON."""
        if settings.no_filesystem_mode:
            return
        base_dir = self._get_warp_data_dir()
        base_dir.mkdir(parents=True, exist_ok=True)
        meta_file = base_dir / "warp_instances.json"
        data = {iid: inst.to_dict() for iid, inst in self._instances.items()}
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved {len(data)} WARP instance(s) to {meta_file}")

    def _load_instances(self) -> None:
        """Load instance metadata from JSON."""
        if settings.no_filesystem_mode:
            return
        meta_file = self._get_warp_data_dir() / "warp_instances.json"
        if not meta_file.exists():
            return
        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for iid, inst_data in data.items():
                self._instances[iid] = WarpInstance.from_dict(inst_data)
            logger.info(f"Loaded {len(data)} WARP instance(s) from {meta_file}")
        except Exception as e:
            logger.error(f"Failed to load WARP instances: {e}")


warp_manager = WarpManager()
