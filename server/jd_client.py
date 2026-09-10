import json
from typing import List, Optional, Dict, Any

import requests
from fastapi import HTTPException


class LocalJDClient:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def _get(self, path: str, params: Any = None, timeout: float = 6.0) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            kw = {"params": {"params": json.dumps(params)}} if params is not None else {}
            res = requests.get(url, timeout=timeout, **kw)
            if res.status_code == 200:
                p = res.json()
                return p["data"] if isinstance(p, dict) and "data" in p else p
            raise HTTPException(status_code=res.status_code,
                detail=f"JDownloader: {res.status_code} – {res.text[:200]}")
        except requests.exceptions.ConnectionError:
            raise HTTPException(status_code=503,
                detail=f"Cannot connect to JDownloader ({self.base_url}).")
        except requests.exceptions.Timeout:
            raise HTTPException(status_code=504, detail="Timeout connecting to JDownloader.")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def _post(self, path: str, param0: Any = None, data: Any = None, timeout: float = 5.0) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            post_data = {}
            if data is not None:
                post_data = data
            elif param0 is not None:
                post_data["params"] = json.dumps(param0) if not isinstance(param0, str) else param0

            res = requests.post(url, data=post_data, timeout=timeout)
            if res.status_code == 200:
                p = res.json()
                return p["data"] if isinstance(p, dict) and "data" in p else p
            raise HTTPException(status_code=res.status_code,
                detail=f"JDownloader: {res.status_code} – {res.text[:200]}")
        except requests.exceptions.ConnectionError:
            raise HTTPException(status_code=503,
                detail=f"Cannot connect to JDownloader ({self.base_url}).")
        except requests.exceptions.Timeout:
            raise HTTPException(status_code=504, detail="Timeout connecting to JDownloader.")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    def get_state(self) -> str:
        return self._get("/downloadcontroller/getCurrentState") or "UNKNOWN"

    def get_speed(self) -> int:
        try:
            spd = self._get("/downloadcontroller/getSpeedInBps")
            return int(spd) if spd else 0
        except Exception:
            return 0

    def query_packages(self, max_results: int = -1, start_at: int = 0) -> List[Dict[str, Any]]:
        res = self._get("/downloadsV2/queryPackages", params={
            "bytesLoaded": True,
            "bytesTotal": True,
            "speed": True,
            "status": True,
            "finished": True,
            "running": True,
            "priority": True,
            "eta": True,
            "childCount": True,
            "name": True,
            "maxResults": max_results,
            "startAt": start_at,
        })
        return res if isinstance(res, list) else []

    def query_links_for_package(self, package_uuid) -> List[Dict[str, Any]]:
        try:
            res = self._get("/downloadsV2/queryLinks", params={
                "packageUUIDs": [package_uuid],
                "bytesTotal": True,
                "bytesLoaded": True,
                "status": True,
                "finished": True,
                "maxResults": -1,
            }, timeout=6.0)
            return res if isinstance(res, list) else []
        except Exception:
            return []

    def query_package_files(self, package_uuid) -> List[Dict[str, Any]]:
        try:
            res = self._get("/downloadsV2/queryLinks", params={
                "packageUUIDs": [package_uuid],
                "bytesTotal": True,
                "bytesLoaded": True,
                "status": True,
                "finished": True,
                "running": True,
                "speed": True,
                "eta": True,
                "name": True,
                "maxResults": -1,
            }, timeout=8.0)
            return res if isinstance(res, list) else []
        except Exception:
            return []

    def query_linkgrabber_package_files(self, package_uuid) -> List[Dict[str, Any]]:
        try:
            res = self._get("/linkgrabberv2/queryLinks", params={
                "packageUUIDs": [package_uuid],
                "bytesTotal": True,
                "status": True,
                "name": True,
                "maxResults": -1,
            }, timeout=8.0)
            return res if isinstance(res, list) else []
        except Exception:
            return []

    def query_linkgrabber_packages(self) -> List[Dict[str, Any]]:
        try:
            res = self._get("/linkgrabberv2/queryPackages", params={
                "bytesLoaded": True,
                "bytesTotal": True,
                "status": True,
                "childCount": True,
                "name": True,
                "saveTo": True,
                "maxResults": 50,
                "startAt": 0,
            })
            return res if isinstance(res, list) else []
        except Exception:
            return []

    def add_links(self, links: str, autostart: bool = False, package_name: Optional[str] = None, destination_folder: Optional[str] = None):
        param0: Dict[str, Any] = {
            "autostart": autostart,
            "links": links,
            "packageName": package_name or None,
            "priority": "DEFAULT",
            "deepDecrypt": True,
        }
        if destination_folder:
            param0["destinationFolder"] = destination_folder
        return self._post("/linkgrabberv2/addLinks", param0=param0)

    def set_package_download_directory(self, package_uuid, directory: str):
        return self._post("/linkgrabberv2/setDownloadDirectory", data={
            "directory": directory,
            "packageIds": json.dumps([package_uuid])
        })

    def move_to_downloadlist(self, package_ids, link_ids: Optional[list] = None):
        return self._post("/linkgrabberv2/moveToDownloadlist", data={
            "linkIds": json.dumps(link_ids or []),
            "packageIds": json.dumps(package_ids),
        })

    def force_offline_downloads(self, package_ids):
        try:
            self._post("/downloadsV2/resetLinks", data={
                "linkIds": "[]",
                "packageIds": json.dumps(package_ids),
            })
        except Exception:
            pass
        try:
            self._post("/downloadsV2/resumeLinks", data={
                "linkIds": "[]",
                "packageIds": json.dumps(package_ids),
            })
        except Exception:
            pass
        try:
            self._post("/downloadcontroller/forceDownload", data={
                "linkIds": "[]",
                "packageIds": json.dumps(package_ids),
            })
        except Exception:
            pass

    def set_package_priority(self, package_uuid, priority: str):
        try:
            self._post("/downloadsV2/setPriority", data={
                "priority": priority,
                "linkIds": "[]",
                "packageIds": json.dumps([package_uuid]),
            })
        except Exception:
            pass
        if priority in ("HIGH", "HIGHEST"):
            try:
                self._post("/downloadcontroller/forceDownload", data={
                    "linkIds": "[]",
                    "packageIds": json.dumps([package_uuid]),
                })
            except Exception:
                pass
            try:
                self._post("/downloadcontroller/start")
            except Exception:
                pass

    def move_package_direction(self, package_uuid, direction: str = "up"):
        # Query current package list to find neighbor
        pkgs = self.query_packages(max_results=-1, start_at=0)
        uuids = [p.get("uuid") for p in pkgs if p.get("uuid")]
        if package_uuid not in uuids:
            return
        idx = uuids.index(package_uuid)
        if direction == "top":
            self._post("/downloadsV2/movePackages", data={
                "packageIds": json.dumps([package_uuid]),
                "afterDestPackageId": -1
            })
        elif direction == "up":
            if idx == 0:
                return
            # To put it before uuids[idx-1], we place it after uuids[idx-2] (or -1 if idx-1 was first)
            after_id = -1 if (idx - 1 == 0) else uuids[idx - 2]
            self._post("/downloadsV2/movePackages", data={
                "packageIds": json.dumps([package_uuid]),
                "afterDestPackageId": after_id
            })
        elif direction == "down":
            if idx >= len(uuids) - 1:
                return
            # To move down by 1, place it after uuids[idx+1]
            after_id = uuids[idx + 1]
            self._post("/downloadsV2/movePackages", data={
                "packageIds": json.dumps([package_uuid]),
                "afterDestPackageId": after_id
            })

    def delete_package(self, package_uuid, source: str = "downloads"):
        endpoint = "/downloadsV2/removeLinks" if source == "downloads" else "/linkgrabberv2/removeLinks"
        return self._post(endpoint, data={
            "linkIds": "[]",
            "packageIds": json.dumps([package_uuid]),
        })

    def remove_links(self, link_ids, source: str = "downloads"):
        if not link_ids:
            return None
        endpoint = "/downloadsV2/removeLinks" if source == "downloads" else "/linkgrabberv2/removeLinks"
        # Process in batches to avoid overly long requests
        batch_size = 500
        for i in range(0, len(link_ids), batch_size):
            batch = link_ids[i:i + batch_size]
            self._post(endpoint, data={
                "linkIds": json.dumps(batch),
                "packageIds": "[]",
            })
        return True

    def remove_linkgrabber_links(self, link_ids, package_ids=None):
        return self._post("/linkgrabberv2/removeLinks", data={
            "linkIds": json.dumps(list(link_ids)),
            "packageIds": json.dumps(package_ids or []),
        })

    def remove_download_links(self, link_ids, package_ids=None):
        return self._post("/downloadsV2/removeLinks", data={
            "linkIds": json.dumps(list(link_ids)),
            "packageIds": json.dumps(package_ids or []),
        })

    def start_downloads(self):
        return self._post("/downloadcontroller/start")

    def pause_downloads(self, pause: bool = True):
        return self._post("/downloadcontroller/pause", param0=pause)

    def stop_downloads(self):
        return self._post("/downloadcontroller/stop")