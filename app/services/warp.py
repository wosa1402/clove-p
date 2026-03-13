import asyncio
import json
import os
import platform
import shutil
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from loguru import logger

from app.core.config import settings
from app.core.http_client import create_session
from app.core.warp_instance import WarpInstance, WarpInstanceStatus

DIRECT_AUTO_PROXY_PORT = 19080
DIRECT_IPV4_PROXY_PORT = 19081
DIRECT_IPV6_PROXY_PORT = 19082


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
        self._family_proxy_processes: Dict[
            str, Dict[Literal["ipv4", "ipv6"], asyncio.subprocess.Process]
        ] = {}
        self._direct_proxy_processes: Dict[
            Literal["auto", "ipv4", "ipv6"], asyncio.subprocess.Process
        ] = {}
        self._direct_public_ips: Dict[Literal["auto", "ipv4", "ipv6"], Optional[str]] = {
            "auto": None,
            "ipv4": None,
            "ipv6": None,
        }
        self._monitor_task: Optional[asyncio.Task] = None
        self._register_lock = asyncio.Lock()

    # --- Lifecycle ---

    async def start(self) -> None:
        """Load persisted instances and restart running ones."""
        await self._start_direct_proxy_processes()
        await self._refresh_direct_public_ips()
        self._load_instances()
        for instance in self._instances.values():
            if instance.status in (
                WarpInstanceStatus.RUNNING,
                WarpInstanceStatus.STARTING,
            ):
                try:
                    await self._start_process(instance)
                    await self._wait_for_ready(instance)
                    await self._start_family_proxy_processes(instance)
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
        for instance_id in list(self._family_proxy_processes.keys()):
            await self._stop_family_proxy_processes(instance_id)
        await self._stop_direct_proxy_processes()
        self._save_instances()
        logger.info("WarpManager stopped")

    # --- Core operations ---

    async def register_new_instance(
        self,
        register_proxy_mode: Literal["default", "direct", "custom"] = "default",
        register_proxy_url: Optional[str] = None,
        endpoint_mode: Literal["default", "auto", "scan", "custom"] = "default",
        custom_endpoints: Optional[List[str]] = None,
    ) -> WarpInstance:
        """
        Register a new WARP identity, start the tunnel, detect IP.
        Retries up to max_retries if both IPv4 and IPv6 match an existing instance.
        """
        async with self._register_lock:
            max_retries = settings.warp_max_register_retries
            resolved_register_proxy_url = self._resolve_register_proxy_url(
                register_proxy_mode, register_proxy_url
            )
            resolved_endpoint_mode, resolved_custom_endpoints = (
                self._resolve_endpoint_config(endpoint_mode, custom_endpoints)
            )
            existing_ip_pairs = {
                (inst.public_ipv4, inst.public_ipv6)
                for inst in self._instances.values()
                if inst.public_ipv4 or inst.public_ipv6
            }

            for attempt in range(1, max_retries + 1):
                logger.info(f"WARP registration attempt {attempt}/{max_retries}")
                instance = self._create_instance_metadata(
                    endpoint_mode=resolved_endpoint_mode,
                    custom_endpoints=resolved_custom_endpoints,
                )

                try:
                    await self._start_process(
                        instance,
                        register_proxy_mode=(
                            "custom"
                            if resolved_register_proxy_url
                            else register_proxy_mode
                        ),
                        register_proxy_url=resolved_register_proxy_url,
                    )
                    await self._wait_for_ready(instance)
                    await self._start_family_proxy_processes(instance)
                    await self._refresh_public_ips(instance)
                    public_ip_pair = (instance.public_ipv4, instance.public_ipv6)

                    if public_ip_pair in existing_ip_pairs:
                        logger.warning(
                            "Duplicate WARP egress pair on attempt "
                            f"{attempt}, retrying: IPv4={instance.public_ipv4 or '-'} "
                            f"IPv6={instance.public_ipv6 or '-'}"
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
                            f"Failed to register WARP instance with unique IPv4/IPv6 pair "
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
        await self._start_family_proxy_processes(instance)
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
        await self._stop_family_proxy_processes(instance_id)
        await self._stop_process(instance_id)
        instance.status = WarpInstanceStatus.STOPPED
        self._save_instances()
        return instance

    async def restart_instance(self, instance_id: str) -> WarpInstance:
        """Restart an existing WARP instance using the same identity data."""
        instance = self._instances.get(instance_id)
        if not instance:
            raise ValueError(f"Instance {instance_id} not found")
        if instance.status == WarpInstanceStatus.STARTING:
            raise RuntimeError("WARP instance is still starting")

        await self._stop_family_proxy_processes(instance_id)
        await self._stop_process(instance_id)

        try:
            await self._start_process(instance)
            await self._wait_for_ready(instance)
            await self._start_family_proxy_processes(instance)
            await self._refresh_public_ips(instance)
            instance.status = WarpInstanceStatus.RUNNING
            instance.error_message = None
        except Exception as e:
            instance.status = WarpInstanceStatus.ERROR
            instance.error_message = str(e)
            self._save_instances()
            raise

        self._save_instances()
        return instance

    def get_instance(self, instance_id: str) -> Optional[WarpInstance]:
        return self._instances.get(instance_id)

    def get_all_instances(self) -> List[WarpInstance]:
        return list(self._instances.values())

    def get_proxy_url(
        self,
        instance_id: str,
        ip_family: Literal["auto", "ipv4", "ipv6"] = "auto",
    ) -> Optional[str]:
        """Get the effective SOCKS5 proxy URL for a running instance."""
        instance = self._instances.get(instance_id)
        if not instance or instance.status != WarpInstanceStatus.RUNNING:
            return None

        if ip_family == "auto":
            return instance.proxy_url
        if ip_family == "ipv4":
            return instance.ipv4_proxy_url
        if ip_family == "ipv6":
            return instance.ipv6_proxy_url
        return None

    def get_direct_proxy_url(
        self,
        ip_family: Literal["auto", "ipv4", "ipv6"] = "auto",
    ) -> str:
        """Get the local host direct family proxy URL."""
        port_map = {
            "auto": DIRECT_AUTO_PROXY_PORT,
            "ipv4": DIRECT_IPV4_PROXY_PORT,
            "ipv6": DIRECT_IPV6_PROXY_PORT,
        }
        return f"socks5://127.0.0.1:{port_map[ip_family]}"

    def get_direct_public_ip(
        self,
        ip_family: Literal["auto", "ipv4", "ipv6"] = "auto",
    ) -> Optional[str]:
        """Get the cached direct host egress IP for a given family."""
        return self._direct_public_ips.get(ip_family)

    def _resolve_register_proxy_url(
        self,
        register_proxy_mode: Literal["default", "direct", "custom"] = "default",
        register_proxy_url: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve the registration proxy URL for this WARP creation attempt."""
        normalized_mode = (register_proxy_mode or "default").strip().lower()

        if normalized_mode == "default":
            return settings.warp_register_proxy_url

        if normalized_mode == "direct":
            return None

        if normalized_mode == "custom":
            proxy_candidate = (register_proxy_url or "").strip()
            if not proxy_candidate:
                raise ValueError("自定义申请代理模式需要提供代理 URL")
            return proxy_candidate

        raise ValueError("不支持的 WARP 申请代理模式")

    def _resolve_endpoint_config(
        self,
        endpoint_mode: Literal["default", "auto", "scan", "custom"] = "default",
        custom_endpoints: Optional[List[str]] = None,
    ) -> tuple[Literal["auto", "scan", "custom"], List[str]]:
        """Resolve the endpoint strategy for this WARP instance."""
        normalized_mode = (endpoint_mode or "default").strip().lower()

        if normalized_mode == "default":
            resolved_mode = settings.warp_endpoint_mode
            resolved_custom_endpoints = list(settings.warp_custom_endpoints)
        elif normalized_mode == "auto":
            resolved_mode = "auto"
            resolved_custom_endpoints = []
        elif normalized_mode == "scan":
            resolved_mode = "scan"
            resolved_custom_endpoints = []
        elif normalized_mode == "custom":
            resolved_mode = "custom"
            resolved_custom_endpoints = [
                endpoint.strip()
                for endpoint in (custom_endpoints or [])
                if endpoint and endpoint.strip()
            ]
        else:
            raise ValueError("不支持的 WARP endpoint 模式")

        if resolved_mode == "custom" and not resolved_custom_endpoints:
            raise ValueError("自定义 endpoint 模式需要至少提供一个 endpoint")

        return resolved_mode, resolved_custom_endpoints

    # --- Internal helpers ---

    def _ensure_executable(self, candidate: Path) -> Optional[str]:
        """Return an executable binary path, adding execute permission when possible."""
        if not candidate.exists() or not candidate.is_file():
            return None

        candidate_str = str(candidate)
        if os.access(candidate_str, os.X_OK):
            return candidate_str

        try:
            candidate.chmod(candidate.stat().st_mode | 0o111)
        except OSError as e:
            logger.warning(f"Failed to chmod WARP binary {candidate}: {e}")
            return None

        if os.access(candidate_str, os.X_OK):
            logger.info(f"Enabled execute permission for bundled WARP binary: {candidate}")
            return candidate_str

        return None

    def _get_warp_binary(self) -> str:
        """Resolve the path to the warp binary."""
        if settings.warp_binary_path:
            return settings.warp_binary_path

        data_path = Path(settings.data_folder) / "warp"
        bundled_path = Path(__file__).resolve().parents[2] / "warp"
        arch_map = {
            "x86_64": "amd64",
            "amd64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
        }
        machine = platform.machine().lower()
        arch_specific_path = None
        if machine in arch_map:
            arch_specific_path = (
                Path(__file__).resolve().parents[2]
                / f"warp-linux-{arch_map[machine]}"
            )

        for candidate in (data_path, bundled_path, arch_specific_path):
            if candidate is None:
                continue
            resolved = self._ensure_executable(candidate)
            if resolved:
                return resolved

        warp_in_path = shutil.which("warp")
        if warp_in_path:
            return warp_in_path

        raise FileNotFoundError(
            "WARP binary not found. Set WARP_BINARY_PATH, "
            "place binary in data_folder/warp, project_root/warp, "
            "or project_root/warp-linux-<arch>, "
            "or ensure 'warp' is in PATH."
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

    def _create_instance_metadata(
        self,
        endpoint_mode: Literal["auto", "scan", "custom"] = "auto",
        custom_endpoints: Optional[List[str]] = None,
    ) -> WarpInstance:
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
            endpoint_mode=endpoint_mode,
            custom_endpoints=list(custom_endpoints or []),
            status=WarpInstanceStatus.STARTING,
            created_at=datetime.now().isoformat(),
        )

    def _build_endpoint_args(self, instance: WarpInstance) -> List[str]:
        """Translate the instance endpoint strategy into CLI flags."""
        if instance.endpoint_mode == "auto":
            return []

        if instance.endpoint_mode == "scan":
            return ["--scan", "--scan-rtt", f"{settings.warp_scan_rtt_ms}ms"]

        if instance.endpoint_mode == "custom":
            args: List[str] = []
            for endpoint in instance.custom_endpoints:
                args.extend(["--endpoint", endpoint])
            return args

        raise ValueError("不支持的 WARP endpoint 模式")

    async def _start_process(
        self,
        instance: WarpInstance,
        register_proxy_mode: Literal["default", "direct", "custom"] = "default",
        register_proxy_url: Optional[str] = None,
    ) -> None:
        """Spawn the Go binary as a subprocess."""
        binary = self._get_warp_binary()
        cmd = [
            binary,
            "run",
            "--data-dir", instance.data_dir,
            "--socks-addr", f"0.0.0.0:{instance.port}",
        ]
        cmd.extend(self._build_endpoint_args(instance))
        logger.info(f"Starting WARP process: {' '.join(cmd)}")

        env = os.environ.copy()
        resolved_register_proxy_url = self._resolve_register_proxy_url(
            register_proxy_mode, register_proxy_url
        )
        if resolved_register_proxy_url:
            env["WARP_REGISTER_PROXY_URL"] = resolved_register_proxy_url
        else:
            env.pop("WARP_REGISTER_PROXY_URL", None)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
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

    def _get_family_proxy_script(self) -> str:
        script_path = Path(__file__).resolve().parents[1] / "tools" / "warp_family_proxy.py"
        return str(script_path)

    async def _start_family_proxy_processes(self, instance: WarpInstance) -> None:
        """Start IPv4/IPv6 family wrapper proxies for a WARP instance."""
        await self._stop_family_proxy_processes(instance.instance_id)

        script_path = self._get_family_proxy_script()
        processes: Dict[Literal["ipv4", "ipv6"], asyncio.subprocess.Process] = {}

        for family, port in (
            ("ipv4", instance.ipv4_proxy_port),
            ("ipv6", instance.ipv6_proxy_port),
        ):
            cmd = [
                sys.executable,
                script_path,
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(port),
                "--upstream-host",
                "127.0.0.1",
                "--upstream-port",
                str(instance.port),
                "--family",
                family,
            ]
            logger.info(
                f"Starting WARP family proxy for {instance.instance_id} ({family}): {' '.join(cmd)}"
            )
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes[family] = process
            asyncio.create_task(
                self._log_process_output(f"{instance.instance_id}:{family}", process)
            )

        self._family_proxy_processes[instance.instance_id] = processes

        for family, port in (
            ("ipv4", instance.ipv4_proxy_port),
            ("ipv6", instance.ipv6_proxy_port),
        ):
            await self._wait_for_port(
                "127.0.0.1",
                port,
                timeout=5,
                label=f"{instance.instance_id} {family} family proxy",
            )

    async def _stop_family_proxy_processes(self, instance_id: str) -> None:
        """Stop IPv4/IPv6 family wrapper proxies for a WARP instance."""
        processes = self._family_proxy_processes.pop(instance_id, {})
        for family, process in processes.items():
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                logger.info(
                    f"Stopped WARP family proxy for {instance_id} ({family})"
                )

    async def _start_direct_proxy_processes(self) -> None:
        """Start host direct auto/IPv4/IPv6 wrapper proxies."""
        await self._stop_direct_proxy_processes()

        script_path = self._get_family_proxy_script()
        process_specs = (
            ("auto", DIRECT_AUTO_PROXY_PORT),
            ("ipv4", DIRECT_IPV4_PROXY_PORT),
            ("ipv6", DIRECT_IPV6_PROXY_PORT),
        )

        for family, port in process_specs:
            cmd = [
                sys.executable,
                script_path,
                "--mode",
                "direct",
                "--listen-host",
                "127.0.0.1",
                "--listen-port",
                str(port),
                "--family",
                family,
            ]
            logger.info(f"Starting direct family proxy ({family}): {' '.join(cmd)}")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._direct_proxy_processes[family] = process
            asyncio.create_task(self._log_process_output(f"direct:{family}", process))

        for family, port in process_specs:
            await self._wait_for_port(
                "127.0.0.1",
                port,
                timeout=5,
                label=f"direct {family} family proxy",
            )

    async def _stop_direct_proxy_processes(self) -> None:
        """Stop host direct auto/IPv4/IPv6 wrapper proxies."""
        processes = self._direct_proxy_processes
        self._direct_proxy_processes = {}
        for family, process in processes.items():
            if process.returncode is None:
                process.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                logger.info(f"Stopped direct family proxy ({family})")

    async def _refresh_direct_public_ips(self) -> None:
        """Refresh host direct auto/IPv4/IPv6 egress IPs through local wrappers."""
        direct_auto = self.get_direct_proxy_url("auto")
        direct_ipv4 = self.get_direct_proxy_url("ipv4")
        direct_ipv6 = self.get_direct_proxy_url("ipv6")

        try:
            self._direct_public_ips["ipv4"] = await self._probe_proxy_url(
                direct_ipv4,
                settings.warp_ip_check_url,
            )
        except Exception as e:
            self._direct_public_ips["ipv4"] = None
            logger.warning(f"Failed to detect direct IPv4 egress IP: {e}")

        try:
            self._direct_public_ips["ipv6"] = await self._probe_proxy_url(
                direct_ipv6,
                settings.warp_ip_check_url_v6,
            )
        except Exception as e:
            self._direct_public_ips["ipv6"] = None
            logger.warning(f"Failed to detect direct IPv6 egress IP: {e}")

        try:
            self._direct_public_ips["auto"] = await self._probe_proxy_url(
                direct_auto,
                settings.warp_ip_check_url,
            )
        except Exception:
            self._direct_public_ips["auto"] = self._direct_public_ips["ipv4"] or self._direct_public_ips["ipv6"]

    async def _teardown_instance(self, instance: WarpInstance) -> None:
        """Stop process and delete data directory."""
        await self._stop_family_proxy_processes(instance.instance_id)
        await self._stop_process(instance.instance_id)
        if os.path.exists(instance.data_dir):
            shutil.rmtree(instance.data_dir, ignore_errors=True)
            logger.info(f"Deleted data dir: {instance.data_dir}")

    async def _wait_for_ready(self, instance: WarpInstance) -> None:
        """Poll the SOCKS5 port until it accepts connections."""
        timeout = settings.warp_startup_timeout
        await self._wait_for_port(
            "127.0.0.1",
            instance.port,
            timeout=timeout,
            label=f"WARP instance {instance.instance_id}",
        )
        logger.info(
            f"WARP instance {instance.instance_id} ready on port {instance.port}"
        )

        proc = self._processes.get(instance.instance_id)
        if proc and proc.returncode is not None:
            raise RuntimeError(
                f"WARP process exited with code {proc.returncode}"
            )

    async def _wait_for_port(
        self,
        host: str,
        port: int,
        *,
        timeout: int,
        label: str,
    ) -> None:
        """Poll a TCP port until it accepts connections."""
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < timeout:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=2,
                )
                writer.close()
                await writer.wait_closed()
                return
            except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
                await asyncio.sleep(1)

        raise TimeoutError(
            f"{label} did not become ready within {timeout}s"
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
        ip = await self._probe_proxy_url(instance.proxy_url, check_url)
        logger.info(
            f"Detected {ip_family} for {instance.instance_id}: {ip}"
        )
        return ip

    async def _probe_proxy_url(self, proxy_url: str, check_url: str) -> str:
        """Probe a public IP through the specified SOCKS5 proxy URL."""
        async with create_session(timeout=15, proxy=proxy_url) as session:
            response = await session.request("GET", check_url)
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Probe returned status {response.status_code}"
                )

            content = b""
            async for chunk in response.aiter_bytes():
                content += chunk

            return content.decode("utf-8").strip()

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
                            await self._start_family_proxy_processes(instance)
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
            if account.warp_instance_id == instance_id or account.proxy_url == proxy_url:
                account.proxy_url = None
                account.warp_instance_id = None
                account.proxy_ip_family = None
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
