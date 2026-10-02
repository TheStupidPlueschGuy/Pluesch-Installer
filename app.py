import os
import sys
import json
import time
import shutil
import platform
import threading
import traceback
import subprocess
import urllib.request
from flask import Flask, render_template, request, jsonify
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# ===== KONFIGURATION =====
LAUNCHER_VERSION = "1.0.0"  # bei jedem Release in build.yml als Tag mitgeben (z.B. v1.0.0)

# Mailbot-Backend (Account-System) - dasselbe, das auch admin.html/support.html nutzt.
MAILBOT_URL = "https://thestupidplueschguy.pythonanywhere.com"

# Eigenes Release-Repo des Launchers selbst (fürs Auto-Update, Feature #1).
LAUNCHER_RELEASES_API = "https://api.github.com/repos/TheStupidPlueschGuy/pluesch-installer-releases/releases/latest"

# Zielordner für alle installierten Plüsch-Produkte (kein Admin/UAC nötig, da im User-Profil).
INSTALL_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Plüsch Studios")

# ===== PRODUKT-REGISTRY (Feature #4: Mehrprodukt-Unterstützung) =====
# Neues Produkt hinzufügen = neuer Eintrag hier. Der Rest (Install/Update/
# Deinstall/Reparieren) funktioniert automatisch für jedes Produkt in dieser Liste.
PRODUCTS = [
    {
        "id": "pdl",
        "name": "Plüsch Downloader",
        "description": "Lädt Videos/Audio herunter und installiert sie lokal.",
        "releases_api": "https://api.github.com/repos/TheStupidPlueschGuy/plueschi-releases/releases/latest",
        "install_subdir": "Plüsch DL",
        "exe_name": "Plüsch DL.exe",
        "shortcut_name": "Plüsch Downloader.lnk",
    },
    # Künftige Produkte (z.B. Plüsch FM Client) kommen hier einfach dazu.
]

def _get_product(product_id):
    return next((p for p in PRODUCTS if p["id"] == product_id), None)

def _product_exe_path(product):
    return os.path.join(INSTALL_ROOT, product["install_subdir"], product["exe_name"])


def _download_with_retry(url, dest_path, progress_cb=None, max_retries=3, timeout=30):
    """
    Lädt eine Datei robust herunter - ersetzt urllib.request.urlretrieve, das
    weder einen User-Agent-Header setzt noch bei einem abgebrochenen Download
    (z.B. WinError 10054 'Verbindung vom Remotehost geschlossen', öfter bei
    GitHubs Release-CDN) automatisch erneut versucht. Schreibt in eine
    temporäre Datei und benennt erst nach vollständigem, erfolgreichem
    Download um, damit bei einem Abbruch nie eine halbe .exe liegen bleibt.

    progress_cb(downloaded_bytes, total_bytes) wird während des Downloads
    wiederholt aufgerufen, falls übergeben.
    """
    last_err = None
    tmp_path = dest_path + ".part"
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Pluesch-Studios-Launcher",
                "Accept": "application/octet-stream",
            })
            downloaded = 0
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                with open(tmp_path, "wb") as out:
                    while True:
                        chunk = resp.read(262144)  # 256 KB
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb(downloaded, total)
            shutil.move(tmp_path, dest_path)
            return
        except Exception as e:
            last_err = e
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            if attempt < max_retries:
                time.sleep(2 * attempt)  # kurz warten, bevor erneut versucht wird
    raise last_err

# install_progress / launcher_update_progress sind je Produkt- bzw. Launcher-weit,
# da pro Produkt immer nur eine Aktion gleichzeitig laufen kann.
install_progress = {}        # product_id -> {status, percent, message, exe_path}
launcher_update_progress = {"status": "idle", "percent": 0, "message": ""}


def get_appdata_dir():
    """Für Konfig-Kleinkram des Launchers selbst (z.B. gemerkten Login-Token)."""
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Plüsch Studios", "Launcher")
    os.makedirs(d, exist_ok=True)
    return d


def _get_config_file():
    return os.path.join(get_appdata_dir(), "session.json")


# ===== Installierte Versionen merken (Feature #3: Update-Badge) =====
# PDL selbst schreibt keine Versionsdatei - der Launcher merkt sich daher
# selbst, welche Version er zuletzt heruntergeladen hat, um sie mit dem
# neuesten GitHub-Release vergleichen zu können.
def _get_versions_file():
    return os.path.join(get_appdata_dir(), "installed_versions.json")

def _get_installed_versions():
    try:
        with open(_get_versions_file(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_installed_version(product_id, version):
    versions = _get_installed_versions()
    versions[product_id] = version
    try:
        with open(_get_versions_file(), "w", encoding="utf-8") as f:
            json.dump(versions, f)
    except Exception:
        pass


# ===== Lokale Launcher-Einstellungen (Feature #20: Backup/Restore, #18/#19 Flags) =====
# Bewusst eine eigene, kleine Datei getrennt von session.json (Login-Token) und
# installed_versions.json (Produkt-Zustand) - das hier sind reine Komfort-/
# Opt-in-Einstellungen, die man bedenkenlos exportieren/importieren kann, ohne
# dabei versehentlich Login-Daten mit rauszugeben.
DEFAULT_SETTINGS = {
    "crash_reports_enabled": False,
    "theme": "dark",
    "lang": "de",
    "last_seen_version": None,
}

def _get_settings_file():
    return os.path.join(get_appdata_dir(), "settings.json")

def _get_settings():
    try:
        with open(_get_settings_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_SETTINGS)
        merged.update(data or {})
        return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)

def _save_settings(settings):
    try:
        with open(_get_settings_file(), "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
    except Exception:
        pass

def _get_backups_dir():
    d = os.path.join(get_appdata_dir(), "backups")
    os.makedirs(d, exist_ok=True)
    return d


@app.route("/api/settings")
def settings_get():
    return jsonify({"success": True, "settings": _get_settings()})


@app.route("/api/settings", methods=["POST"])
def settings_set():
    data = request.json or {}
    settings = _get_settings()
    for key in DEFAULT_SETTINGS:
        if key in data:
            settings[key] = data[key]
    _save_settings(settings)
    return jsonify({"success": True, "settings": settings})


@app.route("/api/settings/backup", methods=["POST"])
def settings_backup():
    """Feature #20: speichert die aktuellen Einstellungen als Backup-Datei lokal ab."""
    settings = _get_settings()
    filename = f"settings_backup_{int(time.time())}.json"
    path = os.path.join(_get_backups_dir(), filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return jsonify({"success": True, "filename": filename, "path": path})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/backups")
def settings_backups_list():
    """Feature #20: listet vorhandene Backups auf, neuestes zuerst."""
    d = _get_backups_dir()
    try:
        files = [f for f in os.listdir(d) if f.endswith(".json")]
        files.sort(reverse=True)
        entries = []
        for f in files:
            full = os.path.join(d, f)
            entries.append({"filename": f, "mtime": os.path.getmtime(full)})
        return jsonify({"success": True, "backups": entries})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/settings/restore", methods=["POST"])
def settings_restore():
    """Feature #20: stellt ein vorhandenes lokales Backup wieder her."""
    data = request.json or {}
    filename = os.path.basename((data.get("filename") or "").strip())
    if not filename:
        return jsonify({"success": False, "error": "Kein Backup ausgewählt"}), 400
    path = os.path.join(_get_backups_dir(), filename)
    if not os.path.isfile(path):
        return jsonify({"success": False, "error": "Backup nicht gefunden"}), 404
    try:
        with open(path, "r", encoding="utf-8") as f:
            restored = json.load(f)
        settings = dict(DEFAULT_SETTINGS)
        settings.update(restored or {})
        _save_settings(settings)
        return jsonify({"success": True, "settings": settings})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ===== Crash-Reports, Opt-in (Feature #18) =====
def _send_crash_report(exc_text, context="Launcher"):
    """
    Best-effort: schickt einen anonymisierten Traceback ans Mailbot-Backend,
    aber NUR wenn der Nutzer das lokal per Schalter aktiviert hat. Keine
    Nutzerdaten (kein Username/Token/E-Mail) werden mitgeschickt - nur
    Traceback-Text, App-Name/Version und grobe OS-Info.
    """
    try:
        if not _get_settings().get("crash_reports_enabled"):
            return
        payload = json.dumps({
            "traceback": exc_text,
            "app": context,
            "version": LAUNCHER_VERSION,
            "os": f"{platform.system()} {platform.release()}",
        }).encode("utf-8")
        req = urllib.request.Request(
            MAILBOT_URL + "/api/crash-report",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Crash-Reporting darf selbst nie den Launcher crashen.


@app.errorhandler(Exception)
def _handle_uncaught(e):
    """
    Fängt unerwartete Fehler in jeder Route ab und meldet sie (opt-in) anonym.
    WICHTIG: Flasks eigene HTTPException (404 Not Found, 405 Method Not
    Allowed, ...) erbt ebenfalls von Exception - ohne diese Ausnahme würde
    JEDE falsche URL/Methode als "Interner Fehler" (500) getarnt und sogar
    einen (unnötigen) Crash-Report auslösen. Die gehören unverändert durch.
    """
    if isinstance(e, HTTPException):
        return e
    exc_text = traceback.format_exc()
    app.logger.error(exc_text)
    _send_crash_report(exc_text, context="Launcher-Backend")
    return jsonify({"success": False, "error": "Interner Fehler"}), 500


@app.route("/api/crash-report/manual", methods=["POST"])
def crash_report_manual():
    """Erlaubt dem Frontend, einen im JS gefangenen Fehler (z.B. unhandledrejection) zu melden."""
    data = request.json or {}
    exc_text = (data.get("traceback") or "")[:4000]
    if not exc_text:
        return jsonify({"success": False, "error": "Kein Traceback"}), 400
    _send_crash_report(exc_text, context="Launcher-Frontend")
    return jsonify({"success": True})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/session/saved")
def saved_session():
    """Erlaubt dem Frontend, einen zuvor gespeicherten Login wiederherzustellen."""
    try:
        with open(_get_config_file(), "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({})


@app.route("/api/session/save", methods=["POST"])
def save_session():
    data = request.json or {}
    try:
        with open(_get_config_file(), "w", encoding="utf-8") as f:
            json.dump(data, f)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/session/clear", methods=["POST"])
def clear_session():
    try:
        if os.path.exists(_get_config_file()):
            os.remove(_get_config_file())
    except Exception:
        pass
    return jsonify({"ok": True})


# ===== PRODUKTE: LISTE + STATUS =====

@app.route("/api/products")
def products_list():
    """
    Liste aller bekannten Produkte inkl. aktuellem Installationsstatus -
    Grundlage für Feature #4 (Mehrprodukt) und #5 (Deinstall/Reparieren),
    damit der Launcher nach einem Neustart weiß, was schon installiert ist.
    """
    installed_versions = _get_installed_versions()
    result = []
    for p in PRODUCTS:
        exe_path = _product_exe_path(p)
        installed = os.path.exists(exe_path)
        installed_version = installed_versions.get(p["id"]) if installed else None

        latest_version = None
        update_available = False
        if installed:
            # Best-effort - kein Internet darf die Produktliste nicht blockieren.
            try:
                req = urllib.request.Request(p["releases_api"], headers={"Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    release = json.loads(resp.read().decode("utf-8"))
                latest_version = release.get("tag_name", "")
                update_available = bool(latest_version) and latest_version != installed_version
            except Exception:
                pass

        result.append({
            "id": p["id"],
            "name": p["name"],
            "description": p["description"],
            "installed": installed,
            "exe_path": exe_path if installed else None,
            "installed_version": installed_version,
            "latest_version": latest_version,
            "update_available": update_available,
        })
    return jsonify({"success": True, "products": result})


# ===== INSTALLATION / REPARATUR (generisch für alle Produkte) =====

@app.route("/api/install/progress")
def install_progress_route():
    product_id = request.args.get("product_id", "")
    return jsonify(install_progress.get(product_id, {"status": "idle", "percent": 0, "message": ""}))


@app.route("/api/install/start", methods=["POST"])
def install_start():
    """
    Dient sowohl für Erstinstallation als auch für 'Reparieren' (Feature #5) -
    lädt einfach die aktuellste Version erneut herunter und überschreibt.
    """
    data = request.json or {}
    product_id = data.get("product_id", "")
    product = _get_product(product_id)
    if not product:
        return jsonify({"ok": False, "error": "Unbekanntes Produkt"}), 404

    # Schutz gegen Doppelklick auf Reparieren/Installieren (oder ein zweites
    # Browser-Fenster) - zwei gleichzeitige Downloads für dasselbe Produkt
    # würden sich sonst beide dieselbe .part-Datei teilen und sich gegenseitig
    # kaputt machen. Das Frontend sperrt die Buttons zusätzlich während der
    # Laufzeit, das hier ist die eigentliche, verlässliche Absicherung.
    current = install_progress.get(product_id)
    if current and current.get("status") in ("checking", "downloading", "shortcut"):
        return jsonify({"ok": False, "error": "Installation/Update für dieses Produkt läuft bereits."}), 409

    def do_install():
        install_progress[product_id] = {"status": "checking", "percent": 2, "message": "Suche neueste Version..."}
        try:
            req = urllib.request.Request(product["releases_api"], headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                release = json.loads(resp.read().decode("utf-8"))

            asset = next((a for a in release.get("assets", []) if a.get("name", "").endswith(".exe")), None)
            if not asset:
                install_progress[product_id] = {"status": "error", "percent": 0, "message": "Keine .exe-Datei im neuesten Release gefunden."}
                return

            asset_url = asset["browser_download_url"]
            version = release.get("tag_name", "")

            target_dir = os.path.join(INSTALL_ROOT, product["install_subdir"])
            os.makedirs(target_dir, exist_ok=True)
            exe_path = os.path.join(target_dir, product["exe_name"])

            install_progress[product_id] = {"status": "downloading", "percent": 5, "message": f"Lade {product['name']} herunter..."}

            def report(downloaded, total):
                if total > 0:
                    pct = 5 + min(85, int(downloaded / total * 85))
                    install_progress[product_id]["percent"] = pct
                    install_progress[product_id]["message"] = f"Lade herunter... {pct}%"

            _download_with_retry(asset_url, exe_path, progress_cb=report)

            install_progress[product_id] = {"status": "shortcut", "percent": 95, "message": "Erstelle Verknüpfung..."}
            _create_shortcut(exe_path, product["shortcut_name"])
            _save_installed_version(product_id, version)

            install_progress[product_id] = {
                "status": "done", "percent": 100,
                "message": f"{product['name']} {version} ist installiert!",
                "exe_path": exe_path,
            }
            _tray_notify(f"{product['name']} {version} ist bereit.")

        except Exception as e:
            install_progress[product_id] = {"status": "error", "percent": 0, "message": f"Fehler: {str(e)}"}

    threading.Thread(target=do_install, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/install/uninstall", methods=["POST"])
def install_uninstall():
    """Feature #5: entfernt Installationsordner + Startmenü-Verknüpfung eines Produkts."""
    data = request.json or {}
    product_id = data.get("product_id", "")
    product = _get_product(product_id)
    if not product:
        return jsonify({"ok": False, "error": "Unbekanntes Produkt"}), 404

    # Gleicher Grund wie bei /api/install/start: nicht mitten in einem
    # laufenden Download/Update den Zielordner wegreißen.
    current = install_progress.get(product_id)
    if current and current.get("status") in ("checking", "downloading", "shortcut"):
        return jsonify({"ok": False, "error": "Installation/Update läuft gerade - bitte warten."}), 409

    try:
        target_dir = os.path.join(INSTALL_ROOT, product["install_subdir"])
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)

        start_menu = os.path.join(
            os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"
        )
        shortcut_path = os.path.join(start_menu, product["shortcut_name"])
        if os.path.exists(shortcut_path):
            os.remove(shortcut_path)

        install_progress.pop(product_id, None)
        versions = _get_installed_versions()
        versions.pop(product_id, None)
        try:
            with open(_get_versions_file(), "w", encoding="utf-8") as f:
                json.dump(versions, f)
        except Exception:
            pass

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/install/launch", methods=["POST"])
def install_launch():
    data = request.json or {}
    exe_path = data.get("exe_path", "")
    if exe_path and os.path.exists(exe_path):
        subprocess.Popen([exe_path])
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "exe nicht gefunden"}), 404


def _create_shortcut(exe_path, shortcut_name):
    """
    Legt eine Startmenü-Verknüpfung per PowerShell an (WScript.Shell COM-Objekt) -
    braucht keine zusätzliche pywin32/winshell-Abhängigkeit im Build.
    """
    try:
        start_menu = os.path.join(
            os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"
        )
        shortcut_path = os.path.join(start_menu, shortcut_name)
        ps_script = f'''
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{exe_path}"
$Shortcut.WorkingDirectory = "{os.path.dirname(exe_path)}"
$Shortcut.Save()
'''
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=15,
        )
    except Exception:
        pass  # Verknüpfung ist ein Nice-to-have, darf die Installation nicht blockieren


# ===== LAUNCHER-SELBST-UPDATE (Feature #1) =====
# Gleiches Prinzip wie bei Plüsch DL: onefile-exe, per .bat ersetzt, kein
# Admin/UAC noetig, da alles im User-Profil (%LOCALAPPDATA%) liegt.

@app.route("/api/launcher/version")
def launcher_version():
    return jsonify({"version": LAUNCHER_VERSION})


@app.route("/api/launcher/check-update")
def launcher_check_update():
    try:
        req = urllib.request.Request(LAUNCHER_RELEASES_API, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            release = json.loads(resp.read().decode("utf-8"))

        # .lstrip("v") strippt nur Kleinbuchstaben - ein Tag wie "V.0.0.2" (Großbuchstabe
        # oder zusätzliche Punkte beim Release-Erstellen vertippt) blieb sonst unverändert
        # und wurde dann als "vV.0.0.2" angezeigt. Erst alle führenden v/V entfernen, dann
        # überzählige Punkte am Anfang (falls z.B. "V.0.0.2" statt "v0.0.2" getaggt wurde).
        latest_tag = (release.get("tag_name") or "").lstrip("vV").lstrip(".")
        asset = next((a for a in release.get("assets", []) if a.get("name", "").endswith(".exe")), None)

        update_available = bool(latest_tag) and latest_tag != LAUNCHER_VERSION and asset is not None
        return jsonify({
            "success": True,
            "current_version": LAUNCHER_VERSION,
            "latest_version": latest_tag,
            "update_available": update_available,
            "asset_url": asset["browser_download_url"] if (update_available and asset) else None,
            "asset_name": asset["name"] if (update_available and asset) else None,
        })
    except Exception as e:
        # Kein Internet oder GitHub nicht erreichbar - Launcher soll trotzdem normal starten.
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/launcher/update/progress")
def launcher_update_progress_route():
    return jsonify(launcher_update_progress)


@app.route("/api/launcher/update/start", methods=["POST"])
def launcher_update_start():
    if not getattr(sys, 'frozen', False):
        return jsonify({"ok": False, "error": "Updates sind nur in der gebauten .exe verfügbar, nicht im Dev-Modus."}), 400

    data       = request.json or {}
    asset_url  = data.get("asset_url", "")
    asset_name = data.get("asset_name", "Plüsch Studios Launcher.exe")

    if not asset_url:
        return jsonify({"ok": False, "error": "Keine Download-URL"}), 400

    def do_update():
        global launcher_update_progress
        try:
            current_exe = sys.executable
            exe_dir     = os.path.dirname(current_exe)
            new_exe     = os.path.join(exe_dir, asset_name + ".new")
            updater_bat = os.path.join(exe_dir, "_launcher_updater.bat")

            launcher_update_progress = {"status": "downloading", "percent": 0, "message": "Lade neue Version herunter..."}

            def report(downloaded, total):
                if total > 0:
                    pct = min(95, int(downloaded / total * 100))
                    launcher_update_progress["percent"] = pct
                    launcher_update_progress["message"] = f"Lade herunter... {pct}%"

            _download_with_retry(asset_url, new_exe, progress_cb=report)

            launcher_update_progress = {"status": "installing", "percent": 97, "message": "Installiere Update..."}

            bat_content = f"""@echo off
timeout /t 2 /nobreak >nul
move /y "{new_exe}" "{current_exe}"
start "" "{current_exe}"
del "%~f0"
"""
            with open(updater_bat, "w", encoding="utf-8") as f:
                f.write(bat_content)

            launcher_update_progress = {"status": "done", "percent": 100, "message": "Update installiert! Launcher startet neu..."}
            _tray_notify("Update installiert - Launcher startet neu.")
            time.sleep(1.5)

            subprocess.Popen(
                updater_bat, shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            time.sleep(0.5)
            os._exit(0)

        except Exception as e:
            launcher_update_progress = {"status": "error", "percent": 0, "message": f"Fehler: {str(e)}"}

    threading.Thread(target=do_update, daemon=True).start()
    return jsonify({"ok": True})


# ===== SYSTEM-TRAY (Feature #9) =====
# Optional - pystray/Pillow sind nicht zwingend installiert (z.B. im Dev-Modus
# ohne die zusätzlichen Pakete). Fehlt eins davon, läuft der Launcher ganz
# normal weiter, nur ohne Tray-Icon/Minimieren/Benachrichtigungen.
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

tray_icon = None


def _make_tray_image():
    """Einfacher Platzhalter-Icon (Lila Kreis mit 'P') bis es ein echtes Logo gibt."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(124, 58, 237, 255))
    d.text((24, 18), "P", fill=(255, 255, 255, 255))
    return img


def _tray_notify(message, title="Plüsch Studios Launcher"):
    """Best-effort Desktop-Benachrichtigung - still scheitern, nie den Hauptablauf stören."""
    try:
        if HAS_TRAY and tray_icon:
            tray_icon.notify(message, title)
    except Exception:
        pass


def _show_window():
    if window:
        window.show()


def _quit_from_tray():
    try:
        if tray_icon:
            tray_icon.stop()
    except Exception:
        pass
    os._exit(0)


def start_tray():
    global tray_icon
    if not HAS_TRAY:
        return
    menu = pystray.Menu(
        pystray.MenuItem("Öffnen", lambda: _show_window(), default=True),
        pystray.MenuItem("Beenden", lambda: _quit_from_tray()),
    )
    tray_icon = pystray.Icon("pluesch_launcher", _make_tray_image(), "Plüsch Studios Launcher", menu)
    tray_icon.run()


def _on_window_closing():
    """
    Schließen-Klick minimiert in den Tray statt die App zu beenden (wenn ein
    Tray-Icon verfügbar ist) - echtes Beenden geht dann nur noch über
    'Beenden' im Tray-Menü. Rückgabe False verhindert das normale Schließen.
    """
    if HAS_TRAY and tray_icon:
        window.hide()
        return False
    return True


# ===== DOWNLOAD-HISTORIE (Feature #17, Basis für #11) =====
# PDL speichert seine Historie lokal unter ~/.plueschi_downloader_history.json,
# unabhängig davon ob PDL gerade läuft. Der Launcher liest die Datei einfach
# direkt mit - braucht dafür keine neue Schnittstelle in PDL selbst.
def _pdl_history_file():
    return os.path.join(os.path.expanduser("~"), ".plueschi_downloader_history.json")


@app.route("/api/history")
def history_route():
    try:
        with open(_pdl_history_file(), "r", encoding="utf-8") as f:
            entries = json.load(f)
    except Exception:
        entries = []
    return jsonify({"success": True, "entries": entries})


# ===== MIT WINDOWS STARTEN (Feature #14) =====
# Registry-Eintrag im User-Scope (HKCU) - braucht kein Admin/UAC.
AUTOSTART_KEY_NAME = "PlueschStudiosLauncher"

def _autostart_registry_path():
    try:
        import winreg
        return winreg, winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run"
    except ImportError:
        return None, None, None


@app.route("/api/launcher/autostart")
def autostart_status():
    winreg, hkey, path = _autostart_registry_path()
    if not winreg:
        return jsonify({"success": False, "error": "Nur unter Windows verfügbar"})
    try:
        with winreg.OpenKey(hkey, path, 0, winreg.KEY_READ) as key:
            try:
                winreg.QueryValueEx(key, AUTOSTART_KEY_NAME)
                return jsonify({"success": True, "enabled": True})
            except FileNotFoundError:
                return jsonify({"success": True, "enabled": False})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/launcher/autostart", methods=["POST"])
def autostart_set():
    winreg, hkey, path = _autostart_registry_path()
    if not winreg:
        return jsonify({"success": False, "error": "Nur unter Windows verfügbar"}), 400

    data = request.json or {}
    enabled = bool(data.get("enabled"))
    try:
        with winreg.OpenKey(hkey, path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                exe_path = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
                winreg.SetValueEx(key, AUTOSTART_KEY_NAME, 0, winreg.REG_SZ, f'"{exe_path}" --minimized')
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_KEY_NAME)
                except FileNotFoundError:
                    pass
        return jsonify({"success": True, "enabled": enabled})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/shutdown", methods=["POST"])
def shutdown():
    def kill():
        time.sleep(0.3)
        if window:
            window.destroy()
        os._exit(0)
    threading.Thread(target=kill, daemon=True).start()
    return jsonify({"ok": True})


# ===== PYWEBVIEW START =====

window = None


def start_flask():
    app.run(debug=False, port=5050, use_reloader=False, threaded=True)


if __name__ == "__main__":
    try:
        if getattr(sys, 'frozen', False):
            os.chdir(os.path.dirname(sys.executable))
        else:
            os.chdir(os.path.dirname(os.path.abspath(__file__)))

        import webview

        flask_thread = threading.Thread(target=start_flask, daemon=True)
        flask_thread.start()

        for _ in range(20):
            try:
                urllib.request.urlopen("http://localhost:5050", timeout=1)
                break
            except Exception:
                time.sleep(0.5)

        # Für den Autostart-Fall (Feature #14): mit --minimized gestartet (z.B.
        # durch den Registry-Eintrag) direkt in den Tray starten statt das
        # Fenster zu zeigen - braucht dafür ein Tray-Icon zum späteren Öffnen.
        start_hidden = "--minimized" in sys.argv and HAS_TRAY

        window = webview.create_window(
            title="Plüsch Studios Launcher",
            url="http://localhost:5050",
            width=1000,
            height=680,
            min_size=(820, 560),
            resizable=True,
            text_select=False,
            confirm_close=False,
            hidden=start_hidden,
        )

        if HAS_TRAY:
            window.events.closing += _on_window_closing
            threading.Thread(target=start_tray, daemon=True).start()

        webview.start(debug=False)
        os._exit(0)

    except ImportError:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5050")).start()
        app.run(debug=False, port=5050, threaded=True)

    except Exception:
        # Feature #18: Absturz vor/während des Fensterstarts (außerhalb jeder
        # Flask-Route, daher nicht vom @app.errorhandler oben abgedeckt) -
        # bestmöglich anonym melden, falls der Nutzer das aktiviert hat.
        _send_crash_report(traceback.format_exc(), context="Launcher-Start")
        raise
