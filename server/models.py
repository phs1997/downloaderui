from typing import List, Optional, Any
from pydantic import BaseModel


class AddLinksRequest(BaseModel):
    links: str
    package_name: Optional[str] = None
    destination_folder: Optional[str] = None
    autostart: bool = False
    videos_only: bool = False


class SetDownloadDirectoryRequest(BaseModel):
    package_uuid: Any
    directory: str


class CreateFolderRequest(BaseModel):
    name: str


class ControlRequest(BaseModel):
    action: str


class MoveToDownloadsRequest(BaseModel):
    package_ids: Optional[List[Any]] = None
    download_all: bool = False
    force_offline: bool = False
    videos_only: bool = False


class PackageActionRequest(BaseModel):
    package_uuid: Any
    source: str = "downloads"  # "downloads", "linkgrabber" or "gallery-dl"
    priority: Optional[str] = None  # "DEFAULT", "HIGH", "HIGHEST", or None to cycle
    direction: Optional[str] = None  # "up", "down", "top"


class FilterVideosRequest(BaseModel):
    package_uuid: Optional[Any] = None  # Specific package or None for all packages
    source: str = "downloads"  # "downloads" or "linkgrabber"
