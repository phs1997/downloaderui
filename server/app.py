import os
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from dotenv import load_dotenv

from .resources import (
    STORAGE_PATH, VIDEO_EXTENSIONS,
    format_bytes, format_speed, format_eta,
    get_disk_info,
)
from .gallery import GalleryDlManager
from .jd_client import LocalJDClient
from .ingest import perform_filter_videos, background_filter_task, process_add_links
from .models import (
    AddLinksRequest,
    SetDownloadDirectoryRequest,
    CreateFolderRequest,
    ControlRequest,
    MoveToDownloadsRequest,
    PackageActionRequest,
    FilterVideosRequest,
)

load_dotenv()

app = FastAPI(title="JDownloader WebUI Lite (Local)", version="3.4.0")

JD_URL = os.getenv("JD_URL", "http://172.17.0.2:3128").rstrip("/")
if not JD_URL.startswith("http://") and not JD_URL.startswith("https://"):
    JD_URL = f"http://{JD_URL}"

client = LocalJDClient(JD_URL)
gdl_manager = GalleryDlManager(output_dir=STORAGE_PATH)

# Status cache
cache_status = {"data": None, "ts": 0}


@app.get("/api/status")
def get_status():
    global cache_status
    now = time.time()
    if cache_status["data"] and (now - cache_status["ts"]) < 2.0:
        return cache_status["data"]

    disk_info = get_disk_info()

    try:
        state = client.get_state()
        speed = client.get_speed()
        data = {
            "configured": True,
            "connected": True,
            "device_name": f"Local ({JD_URL})",
            "state": state,
            "speed": speed,
            "speed_formatted": format_speed(speed),
            "downloading": state in ("RUNNING", "DOWNLOADING"),
            "disk": disk_info,
            "error": None,
        }
        cache_status = {"data": data, "ts": now}
        return data
    except HTTPException as he:
        return {"configured": True, "connected": False, "device_name": None,
                "state": "OFFLINE", "speed": 0, "speed_formatted": "0 B/s",
                "downloading": False, "disk": disk_info, "error": he.detail}
    except Exception as e:
        return {"configured": True, "connected": False, "device_name": None,
                "state": "OFFLINE", "speed": 0, "speed_formatted": "0 B/s",
                "downloading": False, "disk": disk_info, "error": str(e)}


@app.get("/api/downloads")
def get_downloads(mode: str = Query("active"), hours: Optional[int] = Query(None)):
    ORDER = {"running": 0, "crawling": 1, "error": 2, "queued": 3, "skipped": 4, "finished": 5}
    items = []

    raw_pkgs = client.query_packages(max_results=-1, start_at=0)

    # 10 MB threshold: 10 * 1024 * 1024 = 10485760 Bytes
    TEN_MB = 10 * 1024 * 1024

    for pkg in raw_pkgs:
        is_finished = pkg.get("finished", False)
        is_running = pkg.get("running", False)
        status_text = pkg.get("status") or ""

        if mode == "active" and is_finished and not is_running:
            continue

        pkg_uuid = pkg.get("uuid")
        b_total = pkg.get("bytesTotal", 0) or 0
        b_loaded = pkg.get("bytesLoaded", 0) or 0
        percent = 0.0
        if b_total > 0:
            percent = round((b_loaded / b_total) * 100, 1)
        elif is_finished:
            percent = 100.0

        child_count = pkg.get("childCount", 0)

        raw_error = bool(status_text and any(err in status_text.lower() for err in ["error", "failed", "offline"]))
        is_error = raw_error

        # If error reported: check sublinks, ignore if only files < 10MB failed
        if is_error and pkg_uuid:
            try:
                sublinks = client.query_links_for_package(pkg_uuid)
                if sublinks:
                    failed_or_incomplete = [
                        l for l in sublinks
                        if not l.get("finished", False) or (
                            bool(l.get("status") and any(err in l.get("status", "").lower() for err in ["error", "failed", "offline"]))
                        )
                    ]
                    if failed_or_incomplete and all((l.get("bytesTotal", 0) or 0) < TEN_MB for l in failed_or_incomplete):
                        is_error = False
                        status_text = "Finished (files <10MB ignored)"
            except Exception:
                pass

        if is_error:
            category = "error"
        elif is_running:
            category = "running"
            status_text = status_text or "Downloading..."
        elif is_finished or not is_error:
            category = "finished" if (is_finished or percent >= 98.0) else "queued"
            status_text = status_text or ("Finished" if category == "finished" else "Queued")
        else:
            category = "queued"
            status_text = status_text or "Queued"

        files_done = child_count if category == "finished" else (child_count if is_finished else 0)
        if is_running and pkg_uuid:
            try:
                sublinks = client.query_links_for_package(pkg_uuid)
                if sublinks:
                    files_done = sum(1 for l in sublinks if l.get("finished", False))
            except Exception:
                pass

        if not is_error and category == "finished":
            percent = 100.0

        speed = pkg.get("speed", 0) or 0
        eta = pkg.get("eta", 0) or 0
        pkg_priority = (pkg.get("priority") or "DEFAULT").upper()
        prio_score = 0
        if pkg_priority == "HIGHEST":
            prio_score = 2
        elif pkg_priority == "HIGH":
            prio_score = 1

        items.append({
            "uuid": str(pkg_uuid or ""),
            "name": pkg.get("name", "Unknown Package"),
            "files_total": child_count,
            "files_done": files_done,
            "files_running": child_count if is_running else 0,
            "files_error": 1 if is_error else 0,
            "bytes_loaded": b_loaded,
            "bytes_total": b_total,
            "bytes_loaded_formatted": format_bytes(b_loaded),
            "bytes_total_formatted": format_bytes(b_total),
            "percent": percent,
            "speed": speed,
            "speed_formatted": format_speed(speed),
            "eta": eta,
            "eta_formatted": format_eta(eta),
            "priority": pkg_priority,
            "prio_score": prio_score,
            "is_prioritized": (prio_score > 0),
            "status": status_text,
            "category": category,
            "is_error": is_error,
            "source": "downloads",
            "timestamp": pkg_uuid if (pkg_uuid and isinstance(pkg_uuid, (int, float)) and pkg_uuid > 1000000000000) else None
        })

    # Calculate total remaining bytes in download queue across all active/unfinished packages
    queue_bytes_remaining = 0
    for pkg in raw_pkgs:
        if not pkg.get("finished", False):
            tot = pkg.get("bytesTotal", 0) or 0
            lod = pkg.get("bytesLoaded", 0) or 0
            if tot > lod:
                queue_bytes_remaining += (tot - lod)

    disk_info = get_disk_info()
    queue_disk_info = None
    if disk_info and "free" in disk_info:
        free_bytes = disk_info["free"]
        after_free = free_bytes - queue_bytes_remaining
        queue_disk_info = {
            "queue_remaining_bytes": queue_bytes_remaining,
            "queue_remaining_formatted": format_bytes(queue_bytes_remaining),
            "after_download_free_bytes": after_free,
            "after_download_free_formatted": format_bytes(max(0, after_free)),
            "sufficient_space": after_free >= 0,
        }

    if mode == "all":
        items = list(reversed(items))[:60]

    if hours:
        now_ms = time.time() * 1000
        cutoff_ms = now_ms - (hours * 3600 * 1000)
        items = [p for p in items if not p.get("timestamp") or p["timestamp"] >= cutoff_ms]

    # Sort priority:
    # 1. Running downloads: HIGHEST first, then HIGH, then normal running (by speed)
    # 2. Queued downloads: HIGHEST first, then HIGH, then normal queued
    # 3. Other categories by ORDER
    def sort_order(p):
        cat = p["category"]
        pscore = p.get("prio_score", 0)
        if cat == "running":
            # 0 for HIGHEST, 1 for HIGH, 2 for normal running
            running_tier = 2 - pscore
            return (0, running_tier, -p.get("speed", 0))
        if cat != "finished":
            # Tier 1 for prioritized queued, Tier 2 for normal queued
            queued_tier = 2 - pscore
            return (1, queued_tier, ORDER.get(cat, 9))
        return (2, ORDER.get(cat, 9), 0)

    # Merge gallery-dl items into the download items list
    gdl_items = gdl_manager.get_downloads_items(mode=mode)
    if hours:
        now_ms = time.time() * 1000
        cutoff_ms = now_ms - (hours * 3600 * 1000)
        gdl_items = [p for p in gdl_items if not p.get("timestamp") or p["timestamp"] >= cutoff_ms]

    all_items = items + gdl_items
    all_items.sort(key=sort_order)

    return {
        "count": len(all_items),
        "items": all_items,
        "mode": mode,
        "queue_info": queue_disk_info
    }


@app.get("/api/downloads/files")
def get_package_files(package_uuid: str = Query(...), source: str = Query("downloads")):
    if source == "gallery-dl":
        files = gdl_manager.get_task_files(str(package_uuid))
        return {
            "package_uuid": str(package_uuid),
            "source": source,
            "count": len(files),
            "files": files
        }
    try:
        numeric_uuid = int(package_uuid)
    except Exception:
        numeric_uuid = 0

    if source == "linkgrabber":
        raw_files = client.query_linkgrabber_package_files(numeric_uuid)
    else:
        raw_files = client.query_package_files(numeric_uuid)
    files = []
    for f in raw_files:
        b_total = f.get("bytesTotal", 0) or 0
        b_loaded = f.get("bytesLoaded", 0) or 0
        is_fin = f.get("finished", False)
        is_run = f.get("running", False)
        status = f.get("status") or ("Finished" if is_fin else ("Downloading..." if is_run else "Ready"))
        pct = round((b_loaded / b_total * 100), 1) if b_total > 0 else (100.0 if is_fin else 0.0)
        speed = f.get("speed", 0) or 0
        eta = f.get("eta", 0) or 0

        files.append({
            "uuid": str(f.get("uuid", "")),
            "name": f.get("name", "Unknown File"),
            "bytes_loaded": b_loaded,
            "bytes_total": b_total,
            "bytes_loaded_formatted": format_bytes(b_loaded),
            "bytes_total_formatted": format_bytes(b_total),
            "percent": pct,
            "finished": is_fin,
            "running": is_run,
            "speed": speed,
            "speed_formatted": format_speed(speed) if speed > 0 else "",
            "eta": eta,
            "eta_formatted": format_eta(eta) if eta > 0 else "",
            "status": status,
        })
    return {"package_uuid": str(package_uuid), "source": source, "count": len(files), "files": files}


@app.get("/api/linkgrabber")
def get_linkgrabber():
    items = []
    raw_grabber = client.query_linkgrabber_packages()

    for pkg in raw_grabber:
        pkg_uuid = pkg.get("uuid")
        b_total = pkg.get("bytesTotal", 0) or 0
        b_loaded = pkg.get("bytesLoaded", 0) or 0
        status_text = pkg.get("status") or "Ready to download"
        save_to = pkg.get("saveTo") or ""
        is_error = "error" in status_text.lower() or "offline" in status_text.lower()
        items.append({
            "uuid": str(pkg_uuid or ""),
            "name": pkg.get("name", "Linkgrabber Package"),
            "save_to": save_to,
            "files_total": pkg.get("childCount", 1),
            "bytes_loaded": b_loaded,
            "bytes_total": b_total,
            "bytes_total_formatted": format_bytes(b_total),
            "status": status_text,
            "category": "error" if is_error else "ready",
            "is_error": is_error,
            "source": "linkgrabber",
            "timestamp": pkg_uuid if (pkg_uuid and isinstance(pkg_uuid, (int, float)) and pkg_uuid > 1000000000000) else None
        })

    # Merge gallery-dl linkgrabber staged items
    gdl_grabber_items = gdl_manager.get_linkgrabber_items()
    all_grabber = items + gdl_grabber_items
    all_grabber.sort(key=lambda p: -(p.get("timestamp") or 0))

    return {"count": len(all_grabber), "items": all_grabber}


@app.get("/api/folders")
def get_folders():
    """Returns available directories inside STORAGE_PATH (/output) for quick selection."""
    folders = []
    if os.path.exists(STORAGE_PATH):
        try:
            for entry in os.scandir(STORAGE_PATH):
                if entry.is_dir() and not entry.name.startswith("."):
                    folders.append(entry.name)
        except Exception:
            pass
    folders.sort(key=str.lower)
    return {"base_path": STORAGE_PATH, "folders": folders}


@app.post("/api/folders/create")
def create_folder(req: CreateFolderRequest):
    """Creates a new folder inside STORAGE_PATH (/output)."""
    raw_name = req.name.strip()
    if not raw_name:
        raise HTTPException(status_code=400, detail="Folder name cannot be empty.")

    # Remove forbidden characters or path traversal
    clean_name = raw_name.replace("..", "").strip("/\\")
    if not clean_name:
        raise HTTPException(status_code=400, detail="Invalid folder name.")

    target_path = os.path.join(STORAGE_PATH, clean_name)
    try:
        os.makedirs(target_path, exist_ok=True)
        # Attempt to set permissions so JD and host can read/write without issue
        try:
            os.chmod(target_path, 0o777)
        except Exception:
            pass
        return {"success": True, "name": clean_name, "path": target_path, "message": f"Folder '{clean_name}' created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error creating folder: {str(e)}")


@app.post("/api/linkgrabber/set-directory")
def set_linkgrabber_directory(req: SetDownloadDirectoryRequest):
    dest = req.directory.strip()
    if not dest:
        raise HTTPException(status_code=400, detail="No path specified.")

    # If user provided a relative subfolder name, resolve it under STORAGE_PATH
    if not dest.startswith("/"):
        dest = f"{STORAGE_PATH}/{dest}"

    try:
        os.makedirs(dest, exist_ok=True)
        try:
            os.chmod(dest, 0o777)
        except Exception:
            pass
    except Exception:
        pass

    try:
        if str(req.package_uuid).startswith("gdl_"):
            gdl_manager.set_destination(str(req.package_uuid), dest)
            return {"success": True, "message": f"gallery-dl download path changed to: {dest}", "directory": dest}
        client.set_package_download_directory(req.package_uuid, dest)
        return {"success": True, "message": f"Download path changed to: {dest}", "directory": dest}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/filter-videos")
def filter_videos_endpoint(req: FilterVideosRequest):
    try:
        res = perform_filter_videos(client, package_uuid=req.package_uuid, source=req.source)
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/linkgrabber/download")
def download_linkgrabber(req: MoveToDownloadsRequest):
    try:
        gdl_started = 0
        if req.download_all:
            # Start all gallery-dl linkgrabber items
            for tid, t in list(gdl_manager.tasks.items()):
                if t.get("stage") == "linkgrabber":
                    gdl_manager.start_download_from_linkgrabber(tid, videos_only=req.videos_only, force_offline=req.force_offline)
                    gdl_started += 1

            pkgs = client.query_linkgrabber_packages()
            pkg_ids = [p["uuid"] for p in pkgs if p.get("uuid")]
        else:
            pkg_ids = req.package_ids or []
            remaining_jd_ids = []
            for pid in pkg_ids:
                spid = str(pid)
                if spid.startswith("gdl_"):
                    gdl_manager.start_download_from_linkgrabber(spid, videos_only=req.videos_only, force_offline=req.force_offline)
                    gdl_started += 1
                else:
                    remaining_jd_ids.append(pid)
            pkg_ids = remaining_jd_ids

        if not pkg_ids and gdl_started == 0:
            return {"success": False, "message": "No packages selected"}

        if pkg_ids:
            if req.videos_only:
                for pid in pkg_ids:
                    try:
                        perform_filter_videos(client, package_uuid=pid, source="linkgrabber")
                    except Exception:
                        pass

            client.move_to_downloadlist(pkg_ids)

            if req.force_offline:
                client.force_offline_downloads(pkg_ids)

            client.start_downloads()

        total_started = len(pkg_ids) + gdl_started
        msg_suffix = " (including forced offline links)" if req.force_offline else ""
        return {"success": True, "message": f"{total_started} package(s) moved to downloads and started{msg_suffix}!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/downloads/prioritize")
def prioritize_download(req: PackageActionRequest):
    try:
        # If specific priority requested:
        target_priority = req.priority
        if not target_priority:
            # Cycle through DEFAULT -> HIGH -> HIGHEST -> DEFAULT
            pkgs = client.query_packages(max_results=-1, start_at=0)
            curr = "DEFAULT"
            for p in pkgs:
                if p.get("uuid") == req.package_uuid:
                    curr = (p.get("priority") or "DEFAULT").upper()
                    break

            if curr == "DEFAULT":
                target_priority = "HIGH"
            elif curr == "HIGH":
                target_priority = "HIGHEST"
            else:
                target_priority = "DEFAULT"

        client.set_package_priority(req.package_uuid, target_priority)
        if target_priority == "HIGHEST":
            # If boosted to HIGHEST, also move it to the very top
            client.move_package_direction(req.package_uuid, "top")

        return {
            "success": True,
            "priority": target_priority,
            "message": f"Priority set to {target_priority}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/downloads/move")
def move_download(req: PackageActionRequest):
    try:
        direction = req.direction or "up"
        client.move_package_direction(req.package_uuid, direction)
        return {"success": True, "message": f"Package moved {direction}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/downloads/delete")
def delete_download(req: PackageActionRequest):
    try:
        if req.source == "gallery-dl" or str(req.package_uuid).startswith("gdl_"):
            gdl_manager.delete_task(str(req.package_uuid))
            return {"success": True, "message": "gallery-dl task deleted!"}

        try:
            numeric_uuid = int(req.package_uuid)
        except Exception:
            numeric_uuid = 0
        client.delete_package(numeric_uuid, source=req.source)
        return {"success": True, "message": "Package deleted!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/downloads/retry")
def retry_download(req: PackageActionRequest):
    """Retry a failed gallery-dl download. gallery-dl skips already-finished
    files, so this only re-attempts missing/failed ones."""
    try:
        if req.source == "gallery-dl" or str(req.package_uuid).startswith("gdl_"):
            ok = gdl_manager.retry_download(str(req.package_uuid))
            if not ok:
                raise HTTPException(status_code=404, detail="gallery-dl task not found or already running")
            return {"success": True, "message": "gallery-dl download restarted"}
        raise HTTPException(status_code=400, detail="Retry is only supported for gallery-dl downloads")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/links")
def add_links(req: AddLinksRequest):
    return process_add_links(client, gdl_manager, req)


@app.post("/api/control")
def control_downloads(req: ControlRequest):
    action = req.action.lower()
    try:
        if action == "start":
            client.start_downloads()
            return {"success": True, "message": "Downloads started"}
        elif action == "pause":
            client.pause_downloads(True)
            return {"success": True, "message": "Downloads paused"}
        elif action == "resume":
            client.pause_downloads(False)
            return {"success": True, "message": "Downloads resumed"}
        elif action == "stop":
            client.stop_downloads()
            return {"success": True, "message": "Downloads stopped"}
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action '{action}'.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error executing '{action}': {str(e)}")


os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_index():
    return FileResponse("static/index.html")


@app.get("/test")
def serve_test_index():
    """Separates Test-UI (fancy Dashboard) - nur als Test, Produktion bleibt auf /."""
    return FileResponse("static/test_index.html")