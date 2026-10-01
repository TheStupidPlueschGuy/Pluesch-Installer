import os
import sys
import json
import time
import threading
import subprocess
import urllib.request
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ===== KONFIGURATION =====
# Mailbot-Backend (Account-System) - dasselbe, das auch admin.html/support.html nutzen.
MAILBOT_URL = "https://thestupidplueschguy.pythonanywhere.com"

# Woher der Installer die neueste Plüsch-Downloader-Version zieht.
RELEASES_API = "https://api.github.com/repos/TheStupidPlueschGuy/plueschi-releases/releases/latest"

# Zielordner für die Installation (kein Admin/UAC nötig, da im User-Profil).
INSTALL_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Plüsch Studios")

install_progress = {"status": "idle", "percent": 0, "message": ""}


def get_appdata_dir():
    """Für Konfig-Kleinkram des Installers selbst (z.B. gemerkten Login-Token)."""
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Plüsch Studios", "Installer")
    os.makedirs(d, exist_ok=True)
    return d


def _get_config_file():
    return os.path.join(get_appdata_dir(), "session.json")


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


# ===== INSTALLATION VON PLÜSCH DOWNLOADER =====

@app.route("/api/install/progress")
def install_progress_route():
    return jsonify(install_progress)


@app.route("/api/install/start", methods=["POST"])
def install_start():
    def do_install():
        global install_progress
        try:
            install_progress = {"status": "checking", "percent": 2, "message": "Suche neueste Version..."}

            req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                release = json.loads(resp.read().decode("utf-8"))

            # PDL-Release ist eine einzelne .exe (--onefile-Build)
            asset = next((a for a in release.get("assets", []) if a.get("name", "").endswith(".exe")), None)
            if not asset:
                install_progress = {"status": "error", "percent": 0, "message": "Keine .exe-Datei im neuesten Release gefunden."}
                return

            asset_url = asset["browser_download_url"]
            version = release.get("tag_name", "")

            target_dir = os.path.join(INSTALL_ROOT, "Plüsch DL")
            os.makedirs(target_dir, exist_ok=True)
            exe_path = os.path.join(target_dir, "Plüsch DL.exe")

            install_progress = {"status": "downloading", "percent": 5, "message": "Lade Plüsch Downloader herunter..."}

            def report(block, block_size, total):
                if total > 0:
                    pct = 5 + min(85, int(block * block_size / total * 85))
                    install_progress["percent"] = pct
                    install_progress["message"] = f"Lade herunter... {pct}%"

            urllib.request.urlretrieve(asset_url, exe_path, reporthook=report)

            install_progress = {"status": "shortcut", "percent": 95, "message": "Erstelle Verknüpfung..."}
            _create_shortcut(exe_path)

            install_progress = {
                "status": "done", "percent": 100,
                "message": f"Plüsch Downloader {version} ist installiert!",
                "exe_path": exe_path,
            }

        except Exception as e:
            install_progress = {"status": "error", "percent": 0, "message": f"Fehler: {str(e)}"}

    threading.Thread(target=do_install, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/install/launch", methods=["POST"])
def install_launch():
    data = request.json or {}
    exe_path = data.get("exe_path", "")
    if exe_path and os.path.exists(exe_path):
        subprocess.Popen([exe_path])
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "exe nicht gefunden"}), 404


def _create_shortcut(exe_path):
    """
    Legt eine Startmenü-Verknüpfung per PowerShell an (WScript.Shell COM-Objekt) -
    braucht keine zusätzliche pywin32/winshell-Abhängigkeit im Build.
    """
    try:
        start_menu = os.path.join(
            os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"
        )
        shortcut_path = os.path.join(start_menu, "Plüsch Downloader.lnk")
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
    app.run(debug=False, port=5050, use_reloader=False)


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

        window = webview.create_window(
            title="Plüsch App",
            url="http://localhost:5050",
            width=1000,
            height=680,
            min_size=(820, 560),
            resizable=True,
            text_select=False,
            confirm_close=False,
        )

        # gui='edgechromium' fest vorgegeben - vermeidet den pythonnet/winforms-
        # Fallback-Pfad, der im --onedir-Build crasht (siehe Plüsch DL app.py).
        webview.start(debug=False, gui='edgechromium')
        os._exit(0)

    except ImportError:
        import webbrowser
        threading.Timer(1.2, lambda: webbrowser.open("http://localhost:5050")).start()
        app.run(debug=False, port=5050)
