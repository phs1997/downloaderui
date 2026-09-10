import os
import json
import time
import threading
import subprocess
from urllib.parse import urlparse
from typing import List, Optional, Dict, Any

from .resources import format_bytes

try:
    from gallery_dl import extractor as gdl_extractor
except ImportError:
    gdl_extractor = None


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

    def retry_download(self, task_id: str) -> bool:
        """Restart a failed (or any) gallery-dl download task. Already-downloaded
        files are skipped automatically by gallery-dl (part/archive markers), so
        this only re-attempts missing/failed items."""
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                return False
            # If a process is still running, refuse (avoid double-run).
            if task.get("process"):
                task["status"] = "Already running"
                return False
            # Reset error state and queue as running again.
            task["stage"] = "downloading"
            task["category"] = "running"
            task["is_error"] = False
            task["files_error"] = 0
            task["files_running"] = 1
            task["status"] = "Restarting download..."
            if not task.get("files_total"):
                task["files_total"] = 0
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