from typing import List, Literal, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.dependencies.auth import AdminAuthDep
from app.services.warp import warp_manager
from app.services.account import account_manager
from app.core.warp_instance import WarpInstanceStatus


class WarpInstanceResponse(BaseModel):
    instance_id: str
    port: int
    proxy_url: str
    ipv4_proxy_port: int
    ipv6_proxy_port: int
    ipv4_proxy_url: str
    ipv6_proxy_url: str
    endpoint_mode: Literal["auto", "scan", "custom"]
    custom_endpoints: List[str]
    public_ip: Optional[str]
    public_ipv4: Optional[str]
    public_ipv6: Optional[str]
    status: WarpInstanceStatus
    created_at: Optional[str]
    last_started_at: Optional[str]
    error_message: Optional[str]


class WarpBindResponse(BaseModel):
    organization_uuid: str
    proxy_url: Optional[str]
    warp_instance_id: Optional[str]
    proxy_ip_family: Optional[Literal["auto", "ipv4", "ipv6"]]


class WarpBindRequest(BaseModel):
    ip_family: Literal["auto", "ipv4", "ipv6"] = "auto"


class WarpRegisterRequest(BaseModel):
    register_proxy_mode: Literal["default", "direct", "custom"] = "default"
    register_proxy_url: Optional[str] = None
    endpoint_mode: Literal["default", "auto", "scan", "custom"] = "default"
    custom_endpoints: Optional[List[str]] = None


def _instance_to_response(inst) -> WarpInstanceResponse:
    return WarpInstanceResponse(
        instance_id=inst.instance_id,
        port=inst.port,
        proxy_url=inst.proxy_url,
        ipv4_proxy_port=inst.ipv4_proxy_port,
        ipv6_proxy_port=inst.ipv6_proxy_port,
        ipv4_proxy_url=inst.ipv4_proxy_url,
        ipv6_proxy_url=inst.ipv6_proxy_url,
        endpoint_mode=inst.endpoint_mode,
        custom_endpoints=inst.custom_endpoints,
        public_ip=inst.public_ip,
        public_ipv4=inst.public_ipv4,
        public_ipv6=inst.public_ipv6,
        status=inst.status,
        created_at=inst.created_at,
        last_started_at=inst.last_started_at,
        error_message=inst.error_message,
    )


router = APIRouter()


@router.get("", response_model=List[WarpInstanceResponse])
async def list_warp_instances(_: AdminAuthDep):
    """List all WARP proxy instances."""
    return [_instance_to_response(inst) for inst in warp_manager.get_all_instances()]


@router.post("/register", response_model=WarpInstanceResponse)
async def register_warp_instance(
    _: AdminAuthDep,
    payload: WarpRegisterRequest | None = None,
):
    """Register a new WARP instance with a unique public IP."""
    register_request = payload or WarpRegisterRequest()
    try:
        instance = await warp_manager.register_new_instance(
            register_proxy_mode=register_request.register_proxy_mode,
            register_proxy_url=register_request.register_proxy_url,
            endpoint_mode=register_request.endpoint_mode,
            custom_endpoints=register_request.custom_endpoints,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return _instance_to_response(instance)


@router.get("/{instance_id}", response_model=WarpInstanceResponse)
async def get_warp_instance(instance_id: str, _: AdminAuthDep):
    """Get a specific WARP instance."""
    instance = warp_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="WARP instance not found")
    return _instance_to_response(instance)


@router.post("/{instance_id}/start", response_model=WarpInstanceResponse)
async def start_warp_instance(instance_id: str, _: AdminAuthDep):
    """Start a stopped WARP instance."""
    try:
        instance = await warp_manager.start_instance(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return _instance_to_response(instance)


@router.post("/{instance_id}/stop", response_model=WarpInstanceResponse)
async def stop_warp_instance(instance_id: str, _: AdminAuthDep):
    """Stop a running WARP instance."""
    try:
        instance = await warp_manager.stop_instance(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return _instance_to_response(instance)


@router.post("/{instance_id}/restart", response_model=WarpInstanceResponse)
async def restart_warp_instance(instance_id: str, _: AdminAuthDep):
    """Restart a WARP instance and refresh its detected egress IPs."""
    try:
        instance = await warp_manager.restart_instance(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return _instance_to_response(instance)


@router.delete("/{instance_id}")
async def delete_warp_instance(instance_id: str, _: AdminAuthDep):
    """Delete a WARP instance (stops it and removes its data)."""
    try:
        await warp_manager.remove_instance(instance_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"message": f"WARP instance {instance_id} deleted"}


@router.post("/{instance_id}/bind/{organization_uuid}", response_model=WarpBindResponse)
async def bind_warp_to_account(
    instance_id: str,
    organization_uuid: str,
    _: AdminAuthDep,
    payload: WarpBindRequest | None = None,
):
    """Bind a WARP instance to a specific account."""
    instance = warp_manager.get_instance(instance_id)
    if not instance:
        raise HTTPException(status_code=404, detail="WARP instance not found")
    if instance.status != WarpInstanceStatus.RUNNING:
        raise HTTPException(status_code=400, detail="WARP instance is not running")

    if organization_uuid not in account_manager._accounts:
        raise HTTPException(status_code=404, detail="Account not found")

    account = account_manager._accounts[organization_uuid]
    ip_family = (payload.ip_family if payload else "auto")

    if account.warp_instance_id and account.warp_instance_id != instance_id:
        raise HTTPException(
            status_code=409,
            detail=f"该 Claude 账户已绑定到 {account.warp_instance_id}，请先解绑后再重新分配",
        )

    if account.proxy_url and not account.warp_instance_id:
        raise HTTPException(
            status_code=409,
            detail="该 Claude 账户当前使用了自定义代理，请先清空自定义代理后再绑定 WARP",
        )

    if ip_family == "ipv4" and not instance.public_ipv4:
        raise HTTPException(status_code=400, detail="该 WARP 实例当前没有可用的 IPv4 出口")

    if ip_family == "ipv6" and not instance.public_ipv6:
        raise HTTPException(status_code=400, detail="该 WARP 实例当前没有可用的 IPv6 出口")

    effective_proxy_url = warp_manager.get_proxy_url(instance_id, ip_family)
    if not effective_proxy_url:
        raise HTTPException(status_code=400, detail="WARP 实例代理当前不可用")

    account.proxy_url = effective_proxy_url
    account.warp_instance_id = instance_id
    account.proxy_ip_family = ip_family
    account_manager.save_accounts()

    return WarpBindResponse(
        organization_uuid=organization_uuid,
        proxy_url=account.proxy_url,
        warp_instance_id=instance_id,
        proxy_ip_family=account.proxy_ip_family,
    )


@router.post("/unbind/{organization_uuid}", response_model=WarpBindResponse)
async def unbind_warp_from_account(organization_uuid: str, _: AdminAuthDep):
    """Remove WARP proxy binding from an account."""
    if organization_uuid not in account_manager._accounts:
        raise HTTPException(status_code=404, detail="Account not found")

    account = account_manager._accounts[organization_uuid]
    account.proxy_url = None
    account.warp_instance_id = None
    account.proxy_ip_family = None
    account_manager.save_accounts()

    return WarpBindResponse(
        organization_uuid=organization_uuid,
        proxy_url=None,
        warp_instance_id=None,
        proxy_ip_family=None,
    )
