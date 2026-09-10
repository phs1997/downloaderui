# JDownloader & Gallery-DL WebUI (Lokal & ohne MyJDownloader)

Ein schlankes, responsives Dark-Mode-Webinterface und CLI-Tool für deine lokale JDownloader 2 Instanz sowie integriertes Multi-Source Scraping via `gallery-dl` – komplett lokal, ohne Abhängigkeit von MyJDownloader Cloud-Konten.

---

## 📂 Quellcode-Ablageort (Wo liegt der Code?)

### 1. Auf dem Docker-Host (Server: `192.168.178.84`)
Der vollständige Quellcode liegt persistent unter:
```bash
/opt/jdownloader-webui/
```

### 2. Innerhalb des Docker-Containers (`jdownloader-webui`)
Innerhalb des laufenden Containers befindet sich der Quellcode im Arbeitsverzeichnis:
```bash
/app/
```
* `/app/app.py` &rarr; Backend-Server (FastAPI, JDownloader API-Client, Gallery-DL Engine & Auto-Resume)
* `/app/static/index.html` &rarr; Single-Page Web-Frontend (TailwindCSS, Lucide/Heroicons, Realtime-Polling)
* `/app/requirements.txt` &rarr; Python-Abhängigkeiten
* `/app/Dockerfile` &rarr; Container-Buildfile
* `/app/docker-compose.yml` &rarr; Compose-Setup
* `/app/cli.py` &rarr; CLI-Kommandozeilentool
* `/output` &rarr; Gemounteter NAS-Download-Speicher (`/mnt/nas/dateien`)

---

## 🚀 Kernfunktionen & Architektur

1. **JDownloader 2 Integration (Port 3128):**
   - Spricht direkt über HTTP mit der lokalen REST-API von JDownloader (`RemoteAPI`).
   - Downloads pausieren, fortsetzen, stoppen, priorisieren und Pakete/Dateien filtern.
   - Linkgrabber-Steuerung (Pakete einzeln oder gesammelt in die Download-Warteschlange schieben).

2. **Gallery-DL Native Integration:**
   - Erkennt automatisch Links, die von `gallery-dl` unterstützt werden (z. B. Bildergalerien, Cumst/Onlyfans-Archive, Twitter, Instagram, etc.).
   - **Linkgrabber-Staging:** Neue Links landen zuerst im Linkgrabber. Ein Hintergrund-Scan ermittelt im Vorfeld die exakte Anzahl der Dateien sowie das **Gesamtdatenvolumen (in GB)**.
   - **Videos-Only Filter:** Ermöglicht das ausschließliche Herunterladen von Videos (`mp4`, `m4v`, `mkv`, `mov`, `webm`, etc.).
   - **Auto-Resume bei Neustart:** Der aktuelle Fortschritt wird unter `/output/gallery-dl/.tasks.json` auf dem NAS gespeichert. Startet der Server oder Container neu, werden laufende Downloads und Scans nahtlos fortgesetzt.

---

## 🛠️ Verwaltung & Befehle

### Container neu starten / Status prüfen:
```bash
# Vom Mac aus:
ssh -i ~/.ssh/id_ed25519_docker root@192.168.178.84 "docker restart jdownloader-webui"
ssh -i ~/.ssh/id_ed25519_docker root@192.168.178.84 "docker logs --tail 50 -f jdownloader-webui"
```

### WebUI im Browser öffnen:
```
http://192.168.178.84:8080
```

### Nutzung via CLI (`cli.py`):
```bash
# Links hinzufügen:
python cli.py add "https://example.com/datei1.zip"

# Downloads auflisten:
python cli.py list

# Status und Speed prüfen:
python cli.py status
```