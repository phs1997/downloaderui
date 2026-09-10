#!/usr/bin/env python3
"""
JDownloader Local CLI Utility
Kommandozeilenwerkzeug zur Steuerung einer rein lokalen JDownloader-Instanz (Port 3128).
Kein MyJDownloader-Konto erforderlich!

Verwendung:
  python cli.py add <url1> [url2] ...
  python cli.py list
  python cli.py status
  python cli.py pause
  python cli.py resume
  python cli.py stop
"""

import sys
import os
import json
import argparse
import requests
from dotenv import load_dotenv

load_dotenv()

JD_URL = os.getenv("JD_URL", "http://localhost:3128").rstrip("/")
if not JD_URL.startswith("http://") and not JD_URL.startswith("https://"):
    JD_URL = f"http://{JD_URL}"


def format_bytes(b):
    if not b or b <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024.0:
            return f"{b:.2f} {unit}"
        b /= 1024.0
    return f"{b:.2f} PB"


def call_api(path, method="GET", param0=None):
    url = f"{JD_URL}/{path.lstrip('/')}"
    try:
        if method == "POST":
            data = {}
            if param0 is not None:
                data["param0"] = json.dumps(param0) if not isinstance(param0, str) else param0
            res = requests.post(url, data=data, timeout=5)
        else:
            params = {}
            if param0 is not None:
                params["param0"] = json.dumps(param0) if not isinstance(param0, str) else param0
            res = requests.get(url, params=params, timeout=5)

        if res.status_code == 200:
            payload = res.json()
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        else:
            print(f"Fehler: JDownloader meldet Status {res.status_code}: {res.text[:200]}")
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"Fehler: Konnte keine Verbindung zu JDownloader unter {JD_URL} herstellen.")
        print("Tipp: Überprüfe, ob JDownloader läuft und die lokale API (Port 3128) aktiv ist:")
        print("  - Einstellungen > Profieinstellungen > RemoteAPI.deprecatedapienabled = true")
        print("  - RemoteAPI.deprecatedapilocalhostonly = false")
        sys.exit(1)
    except Exception as e:
        print(f"Fehler bei API-Aufruf: {e}")
        sys.exit(1)


def cmd_add(links, autostart=True, package=None):
    joined = "\n".join(links)
    print(f"Sende {len(links)} Link(s) an {JD_URL}...")
    call_api("/linkgrabberv2/addLinks", method="POST", param0={
        "autostart": autostart,
        "links": joined,
        "packageName": package or None,
        "priority": "DEFAULT",
        "deepDecrypt": True
    })
    print("✓ Links erfolgreich an JDownloader übergeben!")


def cmd_list():
    links = call_api("/downloadsV2/queryLinks", method="GET", param0={
        "bytesLoaded": True,
        "bytesTotal": True,
        "speed": True,
        "status": True,
        "finished": True,
        "running": True
    })

    print(f"Downloads auf {JD_URL}:\n" + "-" * 65)
    if not links:
        print("Keine Downloads vorhanden.")
        return

    for l in links:
        loaded = format_bytes(l.get("bytesLoaded", 0))
        total = format_bytes(l.get("bytesTotal", 0))
        speed = format_bytes(l.get("speed", 0)) + "/s" if l.get("speed") else ""
        status = l.get("status") or ("Lädt..." if l.get("running") else ("Fertig" if l.get("finished") else "Wartet"))
        pct = 0.0
        if l.get("bytesTotal", 0) > 0:
            pct = round((l.get("bytesLoaded", 0) / l.get("bytesTotal")) * 100, 1)

        print(f"[{pct:>5.1f}%] {l.get('name', 'Unbekannt')}")
        print(f"        Status: {status} | {loaded} / {total} {(' | ' + speed) if speed else ''}")


def cmd_status():
    speed = call_api("/downloadcontroller/getSpeedInBytes") or 0
    state = call_api("/downloadcontroller/getCurrentState") or "UNKNOWN"
    print(f"JDownloader URL:  {JD_URL}")
    print(f"Status:           {state}")
    print(f"Geschwindigkeit:  {format_bytes(speed)}/s")


def cmd_pause():
    call_api("/downloadcontroller/pauseDownloads", method="POST", param0="true")
    print("Downloads pausiert.")


def cmd_resume():
    call_api("/downloadcontroller/pauseDownloads", method="POST", param0="false")
    print("Downloads fortgesetzt.")


def cmd_stop():
    call_api("/downloadcontroller/stopDownloads", method="POST")
    print("Downloads gestoppt.")


def main():
    parser = argparse.ArgumentParser(description="JDownloader Local CLI (No MyJDownloader needed)")
    subparsers = parser.add_subparsers(dest="subcommand", help="Verfügbare Befehle")

    # add
    parser_add = subparsers.add_parser("add", help="Fügt Download-Links hinzu")
    parser_add.add_argument("links", nargs="+", help="Download-URLs")
    parser_add.add_argument("--no-autostart", action="store_true", help="Nicht sofort starten")
    parser_add.add_argument("-p", "--package", help="Name des Pakets")

    # list
    subparsers.add_parser("list", help="Listet alle Downloads auf")

    # status
    subparsers.add_parser("status", help="Zeigt allgemeinen Status & Geschwindigkeit")

    # pause
    subparsers.add_parser("pause", help="Pausiert Downloads")

    # resume
    subparsers.add_parser("resume", help="Setzt Downloads fort")

    # stop
    subparsers.add_parser("stop", help="Stoppt Downloads")

    args = parser.parse_args()

    if args.subcommand == "add":
        cmd_add(args.links, autostart=not args.no_autostart, package=args.package)
    elif args.subcommand == "list":
        cmd_list()
    elif args.subcommand == "status":
        cmd_status()
    elif args.subcommand == "pause":
        cmd_pause()
    elif args.subcommand == "resume":
        cmd_resume()
    elif args.subcommand == "stop":
        cmd_stop()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
