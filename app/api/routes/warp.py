from typing import List, Optional
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
    public_ip: Optional[str]
    status: WarpInstanceStatus
    created_at: Optional[str]
    last_started_at: Optional[str]
    error_message: Optional[str]


class WarpBindResponse(BaseModel):
    organization_uuid: str
    proxy_url: Optional[str]
    warp_instance_id: Optional[str]


def _instance_to_response(inst) -> WarpInstanceResponse:
    return WarpInstanceResponse(
        instance_id=inst.instance_id,
        port=inst.port,
        proxy_url=inst.proxy_url,
        public_ip=inst.public_ip,
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
async def register_warp_instance(_: AdminAuthDep):
    """Register a new WARP instance with a unique public IP."""
    try:
        instance = await warp_manager.register_new_instance()
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
    instance_id: str, organization_uuid: str, _: AdminAuthDep
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
    account.proxy_url = instance.proxy_url
    account_manager.save_accounts()

    return WarpBindResponse(
        organization_uuid=organization_uuid,
        proxy_url=account.proxy_url,
        warp_instance_id=instance_id,
    )


@router.post("/unbind/{organization_uuid}", response_model=WarpBindResponse)
async def unbind_warp_from_account(organization_uuid: str, _: AdminAuthDep):
    """Remove WARP proxy binding from an account."""
    if organization_uuid not in account_manager._accounts:
        raise HTTPException(status_code=404, detail="Account not found")

    account = account_manager._accounts[organization_uuid]
    account.proxy_url = None
    account_manager.save_accounts()

    return WarpBindResponse(
        organization_uuid=organization_uuid,
        proxy_url=None,
        warp_instance_id=None,
    )
