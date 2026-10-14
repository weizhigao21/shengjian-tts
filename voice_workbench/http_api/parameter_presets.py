"""Parameter-preset management; no voice or reference is changed by these routes."""
from fastapi import APIRouter, Request
from ..parameter_presets import ParameterPresets
from ..schemas import ParameterPresetInput, ParameterPresetRename
from .context import manager

router = APIRouter()


@router.get('/parameter-presets')
def listing(request: Request):
    return ParameterPresets(manager(request).config).listing()


@router.post('/parameter-presets', status_code=201)
def create(body: ParameterPresetInput, request: Request):
    return ParameterPresets(manager(request).config).save(body.name, body.model.model_dump(), body.request_options)


@router.put('/parameter-presets/{identity}')
def update(identity: str, body: ParameterPresetInput, request: Request):
    return ParameterPresets(manager(request).config).save(body.name, body.model.model_dump(), body.request_options, identity)


@router.patch('/parameter-presets/{identity}')
def rename(identity: str, body: ParameterPresetRename, request: Request):
    return ParameterPresets(manager(request).config).rename(identity, body.name)


@router.delete('/parameter-presets/{identity}')
def delete(identity: str, request: Request):
    ParameterPresets(manager(request).config).delete(identity)
    return {'deleted': identity}
