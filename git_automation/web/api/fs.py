"""Filesystem browser API endpoints (in-app repo picker, directories only)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel

from git_automation.core import fs
from git_automation.core.models import FsEntry

router = APIRouter()


class FsHomeResponse(BaseModel):
    """The user's home directory path."""

    path: str


class FsListResponse(BaseModel):
    """A directory listing: the directory, its parent, and its sub-dirs."""

    path: str
    parent: str | None
    entries: list[FsEntry]


@router.get("/fs/home")
async def fs_home() -> FsHomeResponse:
    """Return the absolute path of the user's home directory."""
    return FsHomeResponse(path=fs.home_dir())


@router.get("/fs/list")
async def fs_list(path: str = Query(...)) -> FsListResponse:
    """List the sub-directories of ``path`` for the repo picker."""
    entries = await fs.list_dirs(path)
    resolved = Path(os.path.expanduser(path))
    parent = None if resolved.parent == resolved else str(resolved.parent)
    return FsListResponse(path=str(resolved), parent=parent, entries=entries)
