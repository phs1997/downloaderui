import os
import json
import time
import shutil
import threading
import subprocess
import re
from urllib.parse import urlparse
from typing import List, Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import requests

try:
    from gallery_dl import extractor as gdl_extractor
except ImportError:
    gdl_extractor = None

load_dotenv()

app = FastAPI(title="JDownloader WebUI Lite (Local)", version="3.4.0")

JD_URL = os.getenv("JD_URL", "http://172.17.0.2:3128").rstrip("/")
if not JD_URL.startswith("http://") and not JD_URL.startswith("https://"):
    JD_URL = f"http://{JD_URL}"

STORAGE_PATH = os.getenv("STORAGE_PATH", "/output")

VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".wmv", 
    ".m4v", ".ts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf"
)


def format_bytes(bytes_count: Optional[int]) -> str:
    if not bytes_count or bytes_count <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i, val = 0, float(bytes_count)
    while val >= 1024 and i < len(units) - 1:
        val /= 1024
        i += 1
    return f"{val:.2f} {units[i]}"


def format_speed(bps: Optional[int]) -> str:
    if not bps or bps <= 0:
        return "0 B/s"
    return f"{format_bytes(bps)}/s"


def format_eta(secs: Optional[int]) -> str:
    if not secs or secs <= 0:
        return "--"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    elif m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def format_disk_free(bytes_count: Optional[int]) -> str:
    if not bytes_count or bytes_count <= 0:
        return "0 GB"
    gb = bytes_count / (1024 ** 3)
    if gb >= 1024:
        tb = gb / 1024
        return f"{tb:.1f} TB"
    return f"{int(round(gb))} GB"


def get_disk_info() -> Optional[Dict[str, Any]]:
    path_to_check = STORAGE_PATH if os.path.exists(STORAGE_PATH) else "/"
    try:
        usage = shutil.disk_usage(path_to_check)
        return {
            "free": usage.free,
            "total": usage.total,
            "used": usage.used,
            "free_formatted": format_disk_free(usage.free),
            "total_formatted": format_bytes(usage.total),
            "used_formatted": format_bytes(usage.used),
            "percent_used": round((usage.used / usage.total) * 100, 1) if usage.total > 0 else 0.0
        }
    except Exception:
        return None


def is_gallery_dl_supported(url: str) -> bool:
    """Checks if gallery-dl has an actual extractor for this URL (not generic directlink / oauth)."""
    if not gdl_extractor or not url:
        return False
    try:
        extr = gdl_extractor.find(url)
        if extr is None:
            return False
        cat = getattr(extr, "category", "") or ""
        extr_name = type(extr).__name__
        # Ignore generic directlink / oauth fallback
        if cat in ("directlink", "oauth") or extr_name in ("DirectlinkExtractor", "OAuthExtractor"):
            return False
        return True
    except Exception:
        return False


class GalleryDlManager:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.tasks_file = os.path.join(self.output_dir, "gallery-dl", ".tasks.json")
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._load_tasks()

    def _load_tasks(self):
        try:
            if os.path.exists(self.tasks_file):
                with open(self.tasks_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    for tid, t in saved.items():
                        t["process"] = None
                        t["log"] = []
                        # If it was running or crawling when host died, restart it
                        self.tasks[tid] = t
                        if t.get("stage") == "downloading" and t.get("category") == "running":
                            t["status"] = "Resuming download..."
                            threading.Thread(target=self._run_download, args=(tid,), daemon=True).start()
                        elif t.get("stage") == "linkgrabber" and t.get("crawling"):
                            t["crawling"] = True
                            t["status"] = "Resuming crawl..."
                            threading.Thread(target=self._run_crawl, args=(tid,), daemon=True).start()
        except Exception as e:
            print(f"Error loading gallery-dl tasks: {e}")

    def _save_tasks(self):
        try:
            folder = os.path.join(self.output_dir, "gallery-dl")
            os.makedirs(folder, exist_ok=True)
            serializable = {}
            for tid, t in self.tasks.items():
                copy_t = dict(t)
                copy_t.pop("process", None)
                copy_t.pop("log", None)
                serializable[tid] = copy_t
            with open(self.tasks_file, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception as e:
            print(f"Error saving gallery-dl tasks: {e}")

    def create_task(self, url: str, videos_only: bool = False, custom_dest: Optional[str] = None, autostart: bool = False) -> str:
        with self._lock:
            task_id = f"gdl_{int(time.time() * 1000)}"
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")
            path_part = parsed.path.strip("/").replace("/", " - ") or "gallery"
            name = f"{domain} - {path_part}"
            if len(name) > 65:
                name = name[:62] + "..."

            task = {
                "uuid": task_id,
                "name": name,
                "url": url,
                "videos_only": videos_only,
                "custom_dest": custom_dest,
                "stage": "downloading" if autostart else "linkgrabber",
                "crawling": not autostart,
                "status": "Starting..." if autostart else "Crawling gallery...",
                "category": "running" if autostart else "queued",
                "is_error": False,
                "source": "gallery-dl",
                "priority": "DEFAULT",
                "prio_score": 0,
                "files_total": 0,
                "files_done": 0,
                "files_running": 1 if autostart else 0,
                "files_error": 0,
                "bytes_loaded": 0,
                "bytes_total": 0,
                "bytes_loaded_formatted": "0 B",
                "bytes_total_formatted": "--",
                "percent": 0.0,
                "speed": 0,
                "speed_formatted": "",
                "eta": 0,
                "eta_formatted": "",
                "timestamp": int(time.time() * 1000),
                "files": [],
                "crawled_urls": [],
                "process": None,
                "log": []
            }
            self.tasks[task_id] = task
            self._save_tasks()

        if autostart:
            threading.Thread(target=self._run_download, args=(task_id,), daemon=True).start()
            # Also run background counting scan in parallel so total files count is known
            threading.Thread(target=self._run_crawl_count_only, args=(task_id,), daemon=True).start()
        else:
            threading.Thread(target=self._run_crawl, args=(task_id,), daemon=True).start()
        return task_id

    def _run_crawl_count_only(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return
        cmd = ["gallery-dl", "-j"]
        if task["videos_only"]:
            cmd.extend(["--filter", "extension in ('mp4', 'mkv', 'mov', 'avi', 'webm', 'flv', 'wmv', 'm4v', 'ts')"])
        cmd.append(task["url"])
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
            data = json.loads(out)
            total_bytes = 0
            count = 0
            for entry in data:
                if isinstance(entry, list) and len(entry) >= 3 and entry[0] == 3:
                    count += 1
                    meta = entry[2] if isinstance(entry[2], dict) else {}
                    b = meta.get("bytes") or meta.get("filesize") or 0
                    total_bytes += b
            if count > 0:
                with self._lock:
                    task["files_total"] = max(task["files_total"], count)
                    if total_bytes > 0 and (task.get("bytes_total", 0) < total_bytes):
                        task["bytes_total"] = total_bytes
                        task["bytes_total_formatted"] = format_bytes(total_bytes)
                    if task["files_total"] > 0:
                        task["percent"] = round((task["files_done"] / task["files_total"]) * 100, 1)
                    self._save_tasks()
        except Exception:
            pass

    def _run_crawl(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return

        cmd = ["gallery-dl", "-j"]
        if task["videos_only"]:
            cmd.extend(["--filter", "extension in ('mp4', 'mkv', 'mov', 'avi', 'webm', 'flv', 'wmv', 'm4v', 'ts')"])
        cmd.append(task["url"])

        try:
            with self._lock:
                task["crawling"] = True
                task["status"] = "Crawling gallery metadata..."
                self._save_tasks()

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            with self._lock:
                task["process"] = proc

            stdout, _ = proc.communicate()
            ret = proc.returncode

            urls = []
            files_preview = []
            total_bytes = 0

            if ret == 0 and stdout:
                try:
                    data = json.loads(stdout)
                    for entry in data:
                        if isinstance(entry, list) and len(entry) >= 3 and entry[0] == 3:
                            u = str(entry[1])
                            meta = entry[2] if isinstance(entry[2], dict) else {}
                            b = meta.get("bytes") or meta.get("filesize") or 0
                            fname = meta.get("filename") or u.split("/")[-1].split("?")[0]
                            ext = meta.get("extension")
                            if ext and not fname.endswith(f".{ext}"):
                                fname = f"{fname}.{ext}"

                            urls.append(u)
                            total_bytes += b
                            if len(files_preview) < 200:
                                files_preview.append({
                                    "uuid": f"{task_id}_{len(urls)}",
                                    "name": fname,
                                    "bytes_loaded": 0,
                                    "bytes_total": b,
                                    "bytes_loaded_formatted": "0 B",
                                    "bytes_total_formatted": format_bytes(b) if b > 0 else "--",
                                    "percent": 0.0,
                                    "finished": False,
                                    "running": False,
                                    "speed": 0,
                                    "speed_formatted": "",
                                    "eta": 0,
                                    "eta_formatted": "",
                                    "status": "Ready"
                                })
                except Exception:
                    pass

            with self._lock:
                task["process"] = None
                task["crawling"] = False
                if len(urls) > 0:
                    task["files_total"] = len(urls)
                    task["bytes_total"] = total_bytes
                    task["bytes_total_formatted"] = format_bytes(total_bytes) if total_bytes > 0 else "--"
                    size_str = f" ({format_bytes(total_bytes)})" if total_bytes > 0 else ""
                    task["status"] = f"Ready ({len(urls)} items{size_str})"
                    task["crawled_urls"] = urls
                    task["files"] = files_preview
                else:
                    task["status"] = "Error crawling gallery"
                    task["is_error"] = True
                self._save_tasks()

        except Exception as e:
            with self._lock:
                task["process"] = None
                task["crawling"] = False
                task["is_error"] = True
                task["status"] = f"Crawl error: {str(e)[:50]}"
                self._save_tasks()

    def start_download_from_linkgrabber(self, task_id: str, videos_only: Optional[bool] = None, force_offline: bool = False):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            task["stage"] = "downloading"
            task["category"] = "running"
            task["status"] = "Starting download..."
            task["files_running"] = 1
            if videos_only is not None:
                task["videos_only"] = videos_only
            self._save_tasks()

        threading.Thread(target=self._run_download, args=(task_id,), daemon=True).start()
        return True

    def _run_download(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return

        dest_folder = task["custom_dest"]
        if not dest_folder:
            dest_folder = os.path.join(self.output_dir, "gallery-dl")
        os.makedirs(dest_folder, exist_ok=True)

        cmd = [
            "gallery-dl",
            "--dest", dest_folder,
            "--verbose"
        ]

        if task["videos_only"]:
            cmd.extend(["--filter", "extension in ('mp4', 'mkv', 'mov', 'avi', 'webm', 'flv', 'wmv', 'm4v', 'ts')"])

        cmd.append(task["url"])

        try:
            with self._lock:
                task["status"] = "Downloading..."
                task["category"] = "running"
                self._save_tasks()

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            with self._lock:
                task["process"] = proc

            downloaded_files = list(task.get("files", []))
            # Build set of existing names
            seen_names = set(f.get("name") for f in downloaded_files if f.get("finished"))

            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                task["log"].append(line)
                if len(task["log"]) > 100:
                    task["log"].pop(0)

                file_path = line
                if file_path.startswith("# "):
                    file_path = file_path[2:].strip()
                elif file_path.startswith("#"):
                    file_path = file_path[1:].strip()

                if not file_path.startswith("[") and not file_path.startswith("WARNING") and not file_path.startswith("ERROR"):
                    if os.path.exists(file_path):
                        fname = os.path.basename(file_path)
                        if fname not in seen_names:
                            seen_names.add(fname)
                            fsize = 0
                            try:
                                fsize = os.path.getsize(file_path)
                            except Exception:
                                pass

                            downloaded_files.append({
                                "uuid": f"{task_id}_{len(downloaded_files)+1}",
                                "name": fname,
                                "bytes_loaded": fsize,
                                "bytes_total": fsize,
                                "bytes_loaded_formatted": format_bytes(fsize),
                                "bytes_total_formatted": format_bytes(fsize),
                                "percent": 100.0,
                                "finished": True,
                                "running": False,
                                "speed": 0,
                                "speed_formatted": "",
                                "eta": 0,
                                "eta_formatted": "",
                                "status": "Finished"
                            })

                            with self._lock:
                                task["files"] = list(downloaded_files)
                                task["files_done"] = len(downloaded_files)
                                # Keep files_total if pre-crawled/known, otherwise grow with downloads
                                if not task.get("files_total") or task["files_total"] < len(downloaded_files):
                                    task["files_total"] = len(downloaded_files)
                                total = task["files_total"]
                                b_sum = sum(f["bytes_loaded"] for f in downloaded_files)
                                task["bytes_loaded"] = b_sum
                                task["bytes_loaded_formatted"] = format_bytes(b_sum)
                                if not task.get("bytes_total") or task["bytes_total"] < b_sum:
                                    task["bytes_total"] = b_sum
                                task["bytes_total_formatted"] = format_bytes(task["bytes_total"]) if task["bytes_total"] > 0 else "--"
                                if total > 0:
                                    task["percent"] = round((len(downloaded_files) / total) * 100, 1)
                                task["status"] = f"Downloaded {len(downloaded_files)}/{total} item(s)..."

            proc.wait()
            ret = proc.returncode

            with self._lock:
                task["process"] = None
                if ret == 0:
                    task["category"] = "finished"
                    task["status"] = f"Finished ({len(downloaded_files)} items)"
                    task["percent"] = 100.0
                    task["files_running"] = 0
                else:
                    task["category"] = "error"
                    task["is_error"] = True
                    task["status"] = f"Error (exit {ret})"
                    task["files_error"] = 1
                    task["files_running"] = 0
                self._save_tasks()

        except Exception as e:
            with self._lock:
                task["process"] = None
                task["category"] = "error"
                task["is_error"] = True
                task["status"] = f"Error: {str(e)[:60]}"
                task["files_error"] = 1
                task["files_running"] = 0
                self._save_tasks()

    def cancel_task(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            proc = task.get("process")
            if proc:
                try:
                    proc.terminate()
                    time.sleep(0.5)
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass
            task["category"] = "finished"
            task["status"] = "Cancelled"
            task["files_running"] = 0
            self._save_tasks()
            return True

    def delete_task(self, task_id: str):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            proc = task.get("process")
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
            self.tasks.pop(task_id, None)
            self._save_tasks()
            return True

    def set_destination(self, task_id: str, new_dest: str):
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            task["custom_dest"] = new_dest
            self._save_tasks()
            return True

    def get_downloads_items(self, mode: str = "active") -> List[Dict[str, Any]]:
        with self._lock:
            items = []
            for t in self.tasks.values():
                if t.get("stage") != "downloading":
                    continue
                if mode == "active" and t["category"] == "finished":
                    continue
                item = dict(t)
                item.pop("process", None)
                item.pop("log", None)
                items.append(item)
            return items

    def get_linkgrabber_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = []
            for t in self.tasks.values():
                if t.get("stage") != "linkgrabber":
                    continue
                dest = t.get("custom_dest") or os.path.join(self.output_dir, "gallery-dl")
                item = {
                    "uuid": t["uuid"],
                    "name": t["name"],
                    "save_to": dest,
                    "files_total": t["files_total"] or 1,
                    "bytes_loaded": 0,
                    "bytes_total": t["bytes_total"],
                    "bytes_total_formatted": t["bytes_total_formatted"] if t["bytes_total"] > 0 else "--",
                    "status": t["status"],
                    "category": "ready" if not t["is_error"] else "error",
                    "is_error": t["is_error"],
                    "source": "gallery-dl",
                    "crawling": t.get("crawling", False),
                    "videos_only": t.get("videos_only", False),
                    "timestamp": t.get("timestamp")
                }
                items.append(item)
            return items

    def get_task_files(self, task_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return []
            return list(task.get("files", []))


gdl_manager = GalleryDlManager(output_dir=STORAGE_PATH)


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

    def query_links_for_package(self, package_uuid: int) -> List[Dict[str, Any]]:
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

    def query_package_files(self, package_uuid: int) -> List[Dict[str, Any]]:
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

    def query_linkgrabber_package_files(self, package_uuid: int) -> List[Dict[str, Any]]:
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

    def set_package_download_directory(self, package_uuid: int, directory: str):
        return self._post("/linkgrabberv2/setDownloadDirectory", data={
            "directory": directory,
            "packageIds": json.dumps([package_uuid])
        })

    def move_to_downloadlist(self, package_ids: List[int], link_ids: Optional[List[int]] = None):
        return self._post("/linkgrabberv2/moveToDownloadlist", data={
            "linkIds": json.dumps(link_ids or []),
            "packageIds": json.dumps(package_ids),
        })

    def force_offline_downloads(self, package_ids: List[int]):
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

    def set_package_priority(self, package_uuid: int, priority: str):
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

    def move_package_direction(self, package_uuid: int, direction: str = "up"):
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

    def delete_package(self, package_uuid: int, source: str = "downloads"):
        endpoint = "/downloadsV2/removeLinks" if source == "downloads" else "/linkgrabberv2/removeLinks"
        return self._post(endpoint, data={
            "linkIds": "[]",
            "packageIds": json.dumps([package_uuid]),
        })

    def remove_links(self, link_ids: List[int], source: str = "downloads"):
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

    def start_downloads(self):
        return self._post("/downloadcontroller/start")

    def pause_downloads(self, pause: bool = True):
        return self._post("/downloadcontroller/pause", param0=pause)

    def stop_downloads(self):
        return self._post("/downloadcontroller/stop")


client = LocalJDClient(JD_URL)

# Status cache
cache_status = {"data": None, "ts": 0}


class AddLinksRequest(BaseModel):
    links: str
    package_name: Optional[str] = None
    destination_folder: Optional[str] = None
    autostart: bool = False
    videos_only: bool = False


class SetDownloadDirectoryRequest(BaseModel):
    package_uuid: int
    directory: str


class CreateFolderRequest(BaseModel):
    name: str


class ControlRequest(BaseModel):
    action: str


class MoveToDownloadsRequest(BaseModel):
    package_ids: Optional[List[int]] = None
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
    items: List[Dict[str, Any]] = []

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
    items: List[Dict[str, Any]] = []
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
    folders: List[str] = []
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


def perform_filter_videos(package_uuid: Optional[int] = None, source: str = "downloads") -> Dict[str, Any]:
    """Finds non-video links in specified package or across all packages, and removes them."""
    if package_uuid:
        pkg_uuids = [package_uuid]
    else:
        if source == "linkgrabber":
            pkgs = client.query_linkgrabber_packages()
        else:
            pkgs = client.query_packages(max_results=-1, start_at=0)
        pkg_uuids = [p["uuid"] for p in pkgs if p.get("uuid")]

    total_removed = 0
    packages_affected = 0

    for puuid in pkg_uuids:
        if source == "linkgrabber":
            files = client.query_linkgrabber_package_files(puuid)
        else:
            files = client.query_package_files(puuid)
        
        if not files:
            continue

        non_video_link_ids = []
        for f in files:
            fname = (f.get("name") or "").lower()
            # If not ending with any known video extension, mark for removal
            if not any(fname.endswith(ext) for ext in VIDEO_EXTENSIONS):
                fid = f.get("uuid")
                if fid:
                    non_video_link_ids.append(fid)

        if non_video_link_ids:
            try:
                if source == "linkgrabber":
                    client.remove_linkgrabber_links(non_video_link_ids, package_ids=[puuid])
                else:
                    client.remove_download_links(non_video_link_ids, package_ids=[puuid])
                total_removed += len(non_video_link_ids)
                packages_affected += 1
            except Exception as e:
                print(f"Error removing non-video links for package {puuid}: {e}")

    return {
        "success": True,
        "removed_count": total_removed,
        "packages_affected": packages_affected,
        "message": f"Removed {total_removed} non-video file(s) across {packages_affected} package(s)."
    }


def background_filter_task(source: str = "linkgrabber", delay: float = 3.0, retries: int = 4):
    """Background worker to filter out non-videos after links have crawled/scraped."""
    def run():
        for _ in range(retries):
            time.sleep(delay)
            try:
                perform_filter_videos(source=source)
            except Exception:
                pass
    t = threading.Thread(target=run, daemon=True)
    t.start()


@app.post("/api/filter-videos")
def filter_videos_endpoint(req: FilterVideosRequest):
    try:
        res = perform_filter_videos(package_uuid=req.package_uuid, source=req.source)
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
                        perform_filter_videos(package_uuid=pid, source="linkgrabber")
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


@app.post("/api/links")
def add_links(req: AddLinksRequest):
    raw_text = req.links.strip()
    if not raw_text:
        raise HTTPException(status_code=400, detail="No links provided.")

    link_list = [l.strip() for l in raw_text.split() if "://" in l]
    if not link_list:
        link_list = [l.strip() for l in raw_text.splitlines() if l.strip()]
    if not link_list:
        raise HTTPException(status_code=400, detail="No valid URLs found.")

    dest = req.destination_folder.strip() if req.destination_folder else None
    if dest:
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

    # Intelligent check: separate links supported by gallery-dl from standard links
    gdl_links = []
    jd_links = []
    for link in link_list:
        if is_gallery_dl_supported(link):
            gdl_links.append(link)
        else:
            jd_links.append(link)

    gdl_count = 0
    for gurl in gdl_links:
        try:
            gdl_manager.create_task(
                url=gurl,
                videos_only=req.videos_only,
                custom_dest=dest,
                autostart=req.autostart
            )
            gdl_count += 1
        except Exception:
            # Fallback to JDownloader if task creation failed
            jd_links.append(gurl)

    jd_count = 0
    if jd_links:
        try:
            client.add_links("\n".join(jd_links), autostart=req.autostart,
                             package_name=req.package_name,
                             destination_folder=dest)
            jd_count = len(jd_links)
            
            # If videos_only requested, run background filtering as the links crawl
            if req.videos_only:
                target_source = "downloads" if req.autostart else "linkgrabber"
                background_filter_task(source=target_source, delay=3.0, retries=5)
        except Exception as e:
            if gdl_count == 0:
                raise HTTPException(status_code=500, detail=str(e))

    messages = []
    if gdl_count > 0:
        messages.append(f"{gdl_count} link(s) started via gallery-dl")
    if jd_count > 0:
        messages.append(f"{jd_count} link(s) sent to JDownloader")

    return {
        "success": True,
        "count": len(link_list),
        "gdl_count": gdl_count,
        "jd_count": jd_count,
        "message": " & ".join(messages) if messages else "Links processed!"
    }


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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=port)
