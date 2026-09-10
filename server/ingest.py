import os
import time
import threading
from typing import Optional, Dict, Any

from fastapi import HTTPException

from .resources import STORAGE_PATH, VIDEO_EXTENSIONS


def perform_filter_videos(client, package_uuid=None, source: str = "downloads") -> Dict[str, Any]:
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


def background_filter_task(client, source: str = "linkgrabber", delay: float = 3.0, retries: int = 4):
    """Background worker to filter out non-videos after links have crawled/scraped."""
    def run():
        for _ in range(retries):
            time.sleep(delay)
            try:
                perform_filter_videos(client, source=source)
            except Exception:
                pass
    t = threading.Thread(target=run, daemon=True)
    t.start()


def process_add_links(client, gdl_manager, req) -> Dict[str, Any]:
    """Shared AddLinks processing: separates gallery-dl supported links from JD links."""
    from .gallery import is_gallery_dl_supported

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
                background_filter_task(client, source=target_source, delay=3.0, retries=5)
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