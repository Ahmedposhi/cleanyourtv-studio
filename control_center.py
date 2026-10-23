#!/usr/bin/env python3
"""CleanYourTV v2.7 Control Center — Local macOS/Linux Web UI for Android TV customization.

Serves an Apple TV-style dark configuration studio on http://127.0.0.1:8765 using only the
Python standard library. Reads and writes themes/custom/config.json, applies themes over ADB
via tools/theme.sh, captures live TV screenshots via tools/tv.py, checks drift via tools/check.py,
and restores to stock via tools/restore.sh.
"""
import argparse
import datetime
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

def _resolve_repo_dir(repo_override=None) -> Path:
    candidates = []
    if repo_override:
        candidates.append(Path(repo_override).expanduser())
    env_dir = os.environ.get("CLEANYOURTV_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    here = Path(__file__).resolve().parent
    candidates.extend([
        here,
        here.parent,
        Path.cwd() / "cleanyourtv",
        Path.home() / "cleanyourtv",
    ])
    for candidate in candidates:
        if (candidate / "themes").is_dir() and (candidate / "tools").is_dir():
            return candidate.resolve()
    if repo_override:
        return Path(repo_override).expanduser().resolve()
    return here.parent


REPO_DIR = _resolve_repo_dir()
TOOLS_DIR = REPO_DIR / "tools"
THEMES_DIR = REPO_DIR / "themes"
CUSTOM_THEME_DIR = THEMES_DIR / "custom"
BACKUP_DIR = Path(os.environ.get("CLEANYOURTV_STATE_DIR", str(Path.home() / ".cleanyourtv")))
STATE_FILE = BACKUP_DIR / "studio-state.json"
LIVE_SCREENSHOT_FILE = BACKUP_DIR / "live-tv-screen.png"
BACKUP_JSON = BACKUP_DIR / "tv-packages-backup.json"
F1_CALENDAR_JSON = TOOLS_DIR / "f1_calendar.json"

VENV_PY = TOOLS_DIR / "venv" / "bin" / "python"
if not VENV_PY.exists():
    VENV_PY = Path(sys.executable)


def configure_repo_dir(repo_override=None):
    global REPO_DIR, TOOLS_DIR, THEMES_DIR, CUSTOM_THEME_DIR, F1_CALENDAR_JSON, VENV_PY
    REPO_DIR = _resolve_repo_dir(repo_override)
    TOOLS_DIR = REPO_DIR / "tools"
    THEMES_DIR = REPO_DIR / "themes"
    CUSTOM_THEME_DIR = THEMES_DIR / "custom"
    F1_CALENDAR_JSON = TOOLS_DIR / "f1_calendar.json"
    venv_candidate = TOOLS_DIR / "venv" / "bin" / "python"
    VENV_PY = venv_candidate if venv_candidate.exists() else Path(sys.executable)


LOCK = threading.Lock()

DEFAULT_TV_HOST = os.environ.get("TV_HOST", "192.0.2.10")

TV_DEVICES = [
    {
        "id": "living-room",
        "name": "Living Room TV",
        "host": DEFAULT_TV_HOST,
        "model": "Android TV / Google TV",
        "debloat_list": "examples/sony-bravia-4k-gb/debloat.txt",
    },
    {
        "id": "bedroom-tv",
        "name": "Bedroom TV",
        "host": "192.0.2.11",
        "model": "Android TV / Google TV",
        "debloat_list": "examples/sony-bravia-4k-gb/debloat.txt",
    },
]

THEME_CATALOG = [
    {
        "id": "default",
        "title": "Default (Pure Black)",
        "badge": "OLED / Minimal",
        "description": "The clean reference look: pure #000000 black background, 4-column dock at the bottom, zero clutter.",
    },
    {
        "id": "glass",
        "title": "Glass & Cinematic",
        "badge": "Featured v2.7",
        "description": "Soft teal & violet navy wallpaper, Cinematic Top Shelf, frosted-glass dock, icon-only pills, and horizon app-color glow.",
    },
    {
        "id": "aurora",
        "title": "Aurora Borealis",
        "badge": "Atmosphere",
        "description": "Deep northern-lights teal & violet gradient on dark navy (#070A14), darker at the bottom so the dock pops.",
    },
    {
        "id": "ruska",
        "title": "Ruska (Finnish Autumn)",
        "badge": "70% Translucent Dock",
        "description": "Finnish autumn dusk over a spruce & birch lake. 70% translucent dock lets the lake reflection shine through.",
    },
    {
        "id": "scenes",
        "title": "Scenes (14 Landscapes)",
        "badge": "Hourly Rotation",
        "description": "14 dark illustrated landscapes in wallpapers/ that rotate automatically every 60 minutes.",
    },
    {
        "id": "sweden",
        "title": "Sweden (Nordic Photos)",
        "badge": "6 Columns + Glass",
        "description": "Curated dusk photos from Abisko & Stockholm with captions, 6-column frosted-glass dock at 50% opacity, and fading rows.",
    },
    {
        "id": "six-columns",
        "title": "Six Columns Compact",
        "badge": "High Density",
        "description": "Six smaller tiles per row with one extra app grid row visible below the dock on pure black.",
    },
    {
        "id": "dussehra",
        "title": "Dussehra Festival",
        "badge": "Festive Skin",
        "description": "Warm marigold & gold accent glow (#FFB23C) with festive header icons (diya Wi-Fi, bell Inputs, marigold Settings).",
    },
]

COMMON_APPS = [
    {"pkg": "com.netflix.ninja", "label": "Netflix", "category": "Streaming"},
    {"pkg": "com.google.android.youtube.tv", "label": "YouTube", "category": "Streaming"},
    {"pkg": "com.amazon.amazonvideo.livingroom", "label": "Prime Video", "category": "Streaming"},
    {"pkg": "com.disney.disneyplus", "label": "Disney+", "category": "Streaming"},
    {"pkg": "com.apple.atve.androidtv.appletv", "label": "Apple TV", "category": "Streaming"},
    {"pkg": "com.wbd.stream", "label": "Max (HBO)", "category": "Streaming"},
    {"pkg": "com.plexapp.android", "label": "Plex", "category": "Media & Local"},
    {"pkg": "tv.twitch.android.app", "label": "Twitch", "category": "Streaming"},
    {"pkg": "com.spotify.tv.android", "label": "Spotify", "category": "Music"},
    {"pkg": "org.xbmc.kodi", "label": "Kodi", "category": "Media & Local"},
    {"pkg": "com.jellyfin.androidtv", "label": "Jellyfin", "category": "Media & Local"},
    {"pkg": "com.tailscale.ipn", "label": "Tailscale", "category": "Utilities"},
    {"pkg": "com.valor.mfc.droid.tvapp.generic", "label": "Media Center", "category": "Media & Local"},
    {"pkg": "in.startv.hotstar", "label": "Disney+ Hotstar", "category": "Streaming"},
    {"pkg": "com.sony.dtv.tvx", "label": "Sony Live TV / HDMI Inputs (tvx)", "category": "Sony System"},
    {"pkg": "com.sony.dtv.tvxlauncher.programguide", "label": "Sony Program Guide", "category": "Sony System"},
    {"pkg": "com.sony.dtv.osat.music", "label": "Sony Music Player", "category": "Sony System"},
    {"pkg": "com.sony.dtv.osat.album", "label": "Sony Photo Album", "category": "Sony System"},
    {"pkg": "com.sony.dtv.osat.video", "label": "Sony Video Player", "category": "Sony System"},
    {"pkg": "com.sony.dtv.timers", "label": "Sony Timers & Clock", "category": "Sony System"},
    {"pkg": "com.android.vending", "label": "Google Play Store", "category": "Google System"},
    {"pkg": "com.mediatek.wwtv.tvcenter", "label": "MediaTek TV Center", "category": "System"},
    {"pkg": "com.mitv.gallery", "label": "MiTV Gallery", "category": "System"},
]

DEFAULT_CONFIG = {
    "base_theme": "glass",
    "tv_host": DEFAULT_TV_HOST,
    # Look & Hero
    "cinematic_home": True,
    "cinematic_dots": True,
    "cinematic_white_focus": False,
    "cinematic_scrim": 100,
    "cinematic_art": 50,
    "greeting_name": "",
    "dock_glass": True,
    "dock_opacity": 100,
    "dock_even_padding": False,
    "background_color": "#090C14",
    "wallpaper": True,
    "wallpaper_dim": 0,
    "wallpaper_rotation_minutes": 60,
    "wallpaper_order": "shuffle",
    "wallpaper_captions": False,
    "wallpaper_true_color": True,
    "sky_intensity": 0,
    "ambient_glow": True,
    "app_glow": True,
    "glow_style": "horizon",
    "fade_rows": False,
    # Grid & Top Bar
    "columns": 4,
    "rows_below_dock": 0,
    "dock_row": True,
    "dock_title": True,
    "fill_dock": True,
    "always_show_names": False,
    "icon_only_buttons": True,
    "icon_only_inputs": False,
    "top_menu": False,
    "launcher_settings_button": False,
    "reorder_button": False,
    "show_network_pill": True,
    "show_inputs_pill": True,
    "wifi_status_color": True,
    "show_stats": False,
    # Behavior & Motion
    "app_library": False,
    "app_library_selection": False,
    "library_button": False,
    "tile_menu": True,
    "focus_overshoot": False,
    "reduced_motion": False,
    "animations": True,
    "advanced_effects": True,
    "boot_to_live_tv": False,
    "home_idle_clock_minutes": 10,
    "f1_race_mode": False,
    # Apps
    "dock_apps": [
        "com.netflix.ninja",
        "com.google.android.youtube.tv",
        "com.amazon.amazonvideo.livingroom",
        "com.disney.disneyplus",
        "com.apple.atve.androidtv.appletv",
        "com.wbd.stream",
    ],
    "hidden_apps": [
        "com.mediatek.wwtv.tvcenter",
        "com.mitv.gallery",
        "com.sony.dtv.tvx",
        "com.sony.dtv.tvxlauncher.programguide",
    ],
}

# Exact config.xml baseline values so switching themes resets unspecified keys cleanly
XML_BASELINE = {
    "cinematic_home": False,
    "cinematic_dots": True,
    "cinematic_white_focus": False,
    "cinematic_scrim": 100,
    "cinematic_art": 50,
    "greeting_name": "",
    "dock_glass": False,
    "dock_opacity": 100,
    "dock_even_padding": False,
    "background_color": "#000000",
    "wallpaper": False,
    "wallpaper_dim": 0,
    "wallpaper_rotation_minutes": 0,
    "wallpaper_order": "shuffle",
    "wallpaper_captions": False,
    "wallpaper_true_color": False,
    "sky_intensity": 0,
    "ambient_glow": True,
    "app_glow": False,
    "glow_style": "radial",
    "fade_rows": False,
    "columns": 4,
    "rows_below_dock": 0,
    "dock_row": True,
    "dock_title": True,
    "fill_dock": True,
    "always_show_names": False,
    "icon_only_buttons": False,
    "icon_only_inputs": False,
    "top_menu": False,
    "launcher_settings_button": False,
    "reorder_button": False,
    "show_network_pill": True,
    "show_inputs_pill": True,
    "wifi_status_color": False,
    "show_stats": False,
    "app_library": False,
    "app_library_selection": False,
    "library_button": False,
    "tile_menu": False,
    "focus_overshoot": True,
    "reduced_motion": False,
    "animations": True,
    "advanced_effects": False,
    "boot_to_live_tv": False,
    "home_idle_clock_minutes": 0,
    "f1_race_mode": False,
    "dock_apps": [
        "com.netflix.ninja",
        "com.amazon.amazonvideo.livingroom",
        "com.google.android.youtube.tv",
        "in.startv.hotstar",
    ],
    "hidden_apps": [
        "com.mediatek.wwtv.tvcenter",
        "com.mitv.gallery",
        "com.sony.dtv.tvx",
        "com.sony.dtv.tvxlauncher.programguide",
    ],
}


def load_theme_presets():
    out = []
    for item in THEME_CATALOG:
        tid = item["id"]
        tdir = THEMES_DIR / tid
        cfg_file = tdir / "config.json"
        raw_cfg = {}
        if cfg_file.is_file():
            try:
                raw_cfg = json.loads(cfg_file.read_text(encoding="utf-8-sig"))
            except Exception:
                raw_cfg = {}
        merged = dict(XML_BASELINE)
        for k, v in raw_cfg.items():
            merged[k] = v
        # Ensure Sony hidden_apps stay hidden unless user explicitly unchecks them
        hidden = list(merged.get("hidden_apps") or [])
        for sony_pkg in ("com.sony.dtv.tvx", "com.sony.dtv.tvxlauncher.programguide"):
            if sony_pkg not in hidden:
                hidden.append(sony_pkg)
        merged["hidden_apps"] = hidden
        merged["base_theme"] = tid
        merged["f1_race_mode"] = bool(raw_cfg.get("race_calendar"))
        has_single_wp = (tdir / "wallpaper.jpg").is_file()
        wp_dir = tdir / "wallpapers"
        wp_count = len([p for p in wp_dir.glob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]) if wp_dir.is_dir() else 0
        out.append({
            **item,
            "raw_config": raw_cfg,
            "merged_config": merged,
            "has_wallpaper_jpg": has_single_wp,
            "wallpapers_count": wp_count,
            "screenshot_url": f"/themes/{tid}/screenshot.jpg" if (tdir / "screenshot.jpg").is_file() else "",
        })
    return out


def discover_apps():
    apps_by_pkg = {a["pkg"]: dict(a, installed=False) for a in COMMON_APPS}
    installed_set = set()
    if BACKUP_JSON.is_file():
        try:
            b = json.loads(BACKUP_JSON.read_text(encoding="utf-8"))
            installed_set = set(b.get("enabled_packages") or [])
        except Exception:
            pass
    for pkg in installed_set:
        if pkg in apps_by_pkg:
            apps_by_pkg[pkg]["installed"] = True
        elif not pkg.startswith(("com.android.", "android", "com.qualcomm.", "com.mtk.", "com.realtek.")):
            # Include notable Sony/Google/third-party packages from the TV backup
            if pkg.startswith(("com.sony.dtv.", "com.google.android.")):
                short = pkg.split(".")[-1].replace("_", " ").title()
                apps_by_pkg[pkg] = {
                    "pkg": pkg,
                    "label": f"{short} ({pkg})",
                    "category": "Installed on TV",
                    "installed": True,
                }
            else:
                short = pkg.split(".")[-1].replace("_", " ").title()
                apps_by_pkg[pkg] = {
                    "pkg": pkg,
                    "label": f"{short} ({pkg})",
                    "category": "Installed on TV",
                    "installed": True,
                }
    # Sort: Common streaming/media first, then installed on TV
    priority_order = {a["pkg"]: idx for idx, a in enumerate(COMMON_APPS)}
    return sorted(
        apps_by_pkg.values(),
        key=lambda x: (0 if x["pkg"] in priority_order else 1, priority_order.get(x["pkg"], 999), x["label"].lower()),
    )


def load_saved_state():
    state = dict(DEFAULT_CONFIG)
    if STATE_FILE.is_file():
        try:
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update(saved)
        except Exception:
            pass
    return state


def save_state(state):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_f1_races():
    if F1_CALENDAR_JSON.is_file():
        try:
            data = json.loads(F1_CALENDAR_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "race_calendar" in data:
                return data["race_calendar"]
        except Exception:
            pass
    # Try fetching live via race_calendar.py if not cached
    try:
        import race_calendar
        req = race_calendar.urllib.request.Request(
            race_calendar.API.format(season="current"),
            headers={"User-Agent": "cleanyourtv"},
        )
        with race_calendar.urllib.request.urlopen(req, timeout=10) as resp:
            table = json.load(resp)["MRData"]["RaceTable"]
        now = datetime.datetime.now(datetime.timezone.utc)
        races = race_calendar.calendar(table["Races"], now)
        if races:
            F1_CALENDAR_JSON.write_text(json.dumps({"race_calendar": races}, indent=2), encoding="utf-8")
            return races
    except Exception:
        pass
    return []


def build_custom_theme_dir(ui_state):
    """Materialize themes/custom/ with config.json and wallpaper assets from base_theme."""
    if CUSTOM_THEME_DIR.exists():
        shutil.rmtree(CUSTOM_THEME_DIR)
    CUSTOM_THEME_DIR.mkdir(parents=True, exist_ok=True)

    base_theme = str(ui_state.get("base_theme") or "default")
    base_dir = THEMES_DIR / base_theme
    raw_base = {}
    if (base_dir / "config.json").is_file():
        try:
            raw_base = json.loads((base_dir / "config.json").read_text(encoding="utf-8-sig"))
        except Exception:
            raw_base = {}

    # Copy wallpaper assets from base_theme if wallpaper is enabled
    if ui_state.get("wallpaper", False) and base_dir.is_dir():
        if (base_dir / "wallpaper.jpg").is_file():
            shutil.copy2(base_dir / "wallpaper.jpg", CUSTOM_THEME_DIR / "wallpaper.jpg")
        if (base_dir / "wallpapers").is_dir():
            shutil.copytree(base_dir / "wallpapers", CUSTOM_THEME_DIR / "wallpapers")

    config = {}
    # Preserve festival keys if base_theme is a festive theme (like dussehra)
    for k in ("festival", "festival_until", "greeting", "accent_color", "wallpaper_moods"):
        if k in raw_base:
            config[k] = raw_base[k]

    # Map all CleanYourTV v2.7 keys cleanly
    bool_keys = (
        "cinematic_home",
        "cinematic_dots",
        "cinematic_white_focus",
        "dock_glass",
        "dock_even_padding",
        "wallpaper",
        "wallpaper_captions",
        "wallpaper_true_color",
        "ambient_glow",
        "app_glow",
        "fade_rows",
        "dock_row",
        "dock_title",
        "fill_dock",
        "always_show_names",
        "icon_only_buttons",
        "icon_only_inputs",
        "top_menu",
        "launcher_settings_button",
        "reorder_button",
        "show_network_pill",
        "show_inputs_pill",
        "wifi_status_color",
        "show_stats",
        "app_library",
        "app_library_selection",
        "library_button",
        "tile_menu",
        "focus_overshoot",
        "reduced_motion",
        "animations",
        "advanced_effects",
        "boot_to_live_tv",
    )
    for k in bool_keys:
        if k in ui_state:
            config[k] = bool(ui_state[k])

    int_keys = (
        ("columns", 1, 8, 4),
        ("rows_below_dock", 0, 3, 0),
        ("dock_opacity", 30, 100, 100),
        ("wallpaper_dim", 0, 90, 0),
        ("wallpaper_rotation_minutes", 0, 1440, 0),
        ("sky_intensity", 0, 100, 0),
        ("home_idle_clock_minutes", 0, 1440, 0),
        ("cinematic_scrim", 0, 100, 100),
        ("cinematic_art", 0, 100, 50),
    )
    for k, lo, hi, default in int_keys:
        if k in ui_state:
            try:
                val = int(ui_state[k])
                val = max(lo, min(hi, val))
            except Exception:
                val = default
            config[k] = val

    str_keys = ("greeting_name", "background_color", "glow_style", "wallpaper_order")
    for k in str_keys:
        if k in ui_state and ui_state[k] is not None:
            config[k] = str(ui_state[k]).strip()

    if "dock_apps" in ui_state and isinstance(ui_state["dock_apps"], list):
        config["dock_apps"] = [str(x).strip() for x in ui_state["dock_apps"] if str(x).strip()]
    if "hidden_apps" in ui_state and isinstance(ui_state["hidden_apps"], list):
        config["hidden_apps"] = [str(x).strip() for x in ui_state["hidden_apps"] if str(x).strip()]

    # Always keep live_tv_package set for Sony BRAVIA TVs
    config["live_tv_package"] = "com.sony.dtv.tvx"

    if ui_state.get("f1_race_mode"):
        races = load_f1_races()
        if races:
            config["race_calendar"] = races

    (CUSTOM_THEME_DIR / "config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return config


def run_cmd(args, tv_host, timeout=120):
    env = os.environ.copy()
    env["TV_HOST"] = tv_host
    proc = subprocess.run(
        args,
        cwd=str(REPO_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, output.strip()


def capture_live_screenshot(tv_host, keys=None):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [str(VENV_PY), "-I", str(TOOLS_DIR / "tv.py"), "--screenshot", str(LIVE_SCREENSHOT_FILE)]
    if keys:
        cmd.extend(keys)
    cmd.extend(["--wait", "1.8"])
    rc, out = run_cmd(cmd, tv_host, timeout=45)
    return rc == 0, out


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CleanYourTV v2.7 — Control Center</title>
<style>
  :root {
    --bg: #07090f;
    --bg-card: rgba(18, 23, 36, 0.78);
    --bg-card-hover: rgba(26, 33, 52, 0.9);
    --bg-input: rgba(10, 14, 24, 0.85);
    --border: rgba(255, 255, 255, 0.09);
    --border-active: rgba(94, 234, 212, 0.65);
    --text: #f3f6fc;
    --text-dim: #94a3b8;
    --accent: #38bdf8;
    --accent-teal: #2dd4bf;
    --accent-violet: #818cf8;
    --danger: #f43f5e;
    --success: #34d399;
    --warn: #fbbf24;
    --radius: 16px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0 0 110px 0;
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Inter", system-ui, sans-serif;
    background:
      radial-gradient(circle at 15% -5%, rgba(45, 212, 191, 0.14), transparent 42%),
      radial-gradient(circle at 85% 0%, rgba(129, 140, 248, 0.14), transparent 45%),
      var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  header {
    position: sticky;
    top: 0;
    z-index: 40;
    backdrop-filter: blur(22px) saturate(160%);
    -webkit-backdrop-filter: blur(22px) saturate(160%);
    background: rgba(7, 9, 15, 0.82);
    border-bottom: 1px solid var(--border);
    padding: 14px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .brand-logo {
    width: 40px;
    height: 40px;
    border-radius: 11px;
    background: linear-gradient(135deg, #2dd4bf 0%, #38bdf8 50%, #818cf8 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    font-size: 18px;
    color: #06101e;
    box-shadow: 0 6px 20px rgba(56, 189, 248, 0.28);
  }
  .brand h1 {
    margin: 0;
    font-size: 19px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
  .brand p {
    margin: 2px 0 0 0;
    font-size: 12.5px;
    color: var(--text-dim);
  }
  .tv-selector-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .tv-select-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    padding: 6px 12px;
    border-radius: 12px;
  }
  .status-dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--success);
    box-shadow: 0 0 10px var(--success);
  }
  select, input[type="text"], input[type="number"] {
    background: var(--bg-input);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 8px 11px;
    font-size: 13.5px;
    outline: none;
  }
  select:focus, input[type="text"]:focus, input[type="number"]:focus {
    border-color: var(--accent);
  }
  .tv-select-wrap select {
    border: none;
    background: transparent;
    padding: 4px 6px;
    font-weight: 600;
    font-size: 13.5px;
    cursor: pointer;
  }
  .btn {
    border: 1px solid var(--border);
    background: var(--bg-card);
    color: var(--text);
    padding: 9px 15px;
    border-radius: 11px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.16s ease;
    display: inline-flex;
    align-items: center;
    gap: 7px;
  }
  .btn:hover:not(:disabled) {
    background: var(--bg-card-hover);
    border-color: rgba(255,255,255,0.22);
    transform: translateY(-1px);
  }
  .btn:disabled {
    opacity: 0.5;
    cursor: wait;
  }
  .btn-primary {
    background: linear-gradient(135deg, #2dd4bf 0%, #38bdf8 100%);
    color: #041019;
    border: none;
    font-weight: 700;
    box-shadow: 0 6px 20px rgba(45, 212, 191, 0.28);
  }
  .btn-primary:hover:not(:disabled) {
    background: linear-gradient(135deg, #5eead4 0%, #7dd3fc 100%);
    box-shadow: 0 8px 25px rgba(45, 212, 191, 0.4);
  }
  .btn-danger {
    background: rgba(244, 63, 94, 0.14);
    color: #fda4af;
    border-color: rgba(244, 63, 94, 0.35);
  }
  .btn-danger:hover:not(:disabled) {
    background: rgba(244, 63, 94, 0.26);
  }
  .container {
    max-width: 1380px;
    margin: 22px auto;
    padding: 0 26px;
    display: grid;
    grid-template-columns: 1fr 410px;
    gap: 24px;
    align-items: start;
  }
  @media (max-width: 1080px) {
    .container { grid-template-columns: 1fr; }
  }
  .section-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
    margin-bottom: 20px;
    backdrop-filter: blur(14px);
  }
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    gap: 12px;
  }
  .section-header h2 {
    margin: 0;
    font-size: 16.5px;
    font-weight: 700;
    letter-spacing: -0.01em;
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .section-header span.sub {
    font-size: 12.5px;
    color: var(--text-dim);
  }
  .theme-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(225px, 1fr));
    gap: 14px;
  }
  .theme-card {
    background: rgba(11, 15, 25, 0.78);
    border: 1.5px solid var(--border);
    border-radius: 13px;
    overflow: hidden;
    cursor: pointer;
    transition: all 0.18s ease;
    display: flex;
    flex-direction: column;
    position: relative;
  }
  .theme-card:hover {
    border-color: rgba(255, 255, 255, 0.28);
    transform: translateY(-2px);
  }
  .theme-card.active {
    border-color: var(--accent-teal);
    box-shadow: 0 0 0 2px rgba(45, 212, 191, 0.22), 0 10px 28px rgba(0, 0, 0, 0.45);
  }
  .theme-thumb {
    width: 100%;
    aspect-ratio: 16 / 9;
    object-fit: cover;
    background: #05070b;
    display: block;
  }
  .theme-body {
    padding: 11px 13px 13px;
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }
  .theme-title-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    margin-bottom: 5px;
  }
  .theme-title {
    font-weight: 700;
    font-size: 14px;
  }
  .theme-badge {
    font-size: 10.5px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 99px;
    background: rgba(56, 189, 248, 0.14);
    color: #7dd3fc;
    white-space: nowrap;
  }
  .theme-desc {
    font-size: 12px;
    color: var(--text-dim);
    line-height: 1.42;
    margin: 0 0 10px 0;
  }
  .theme-actions {
    display: flex;
    gap: 7px;
  }
  .theme-actions .btn {
    padding: 5px 10px;
    font-size: 11.5px;
    border-radius: 8px;
    flex: 1;
    justify-content: center;
  }
  .controls-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 12px;
  }
  .control-item {
    background: rgba(11, 15, 25, 0.65);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .control-info {
    flex: 1;
  }
  .control-info .label {
    font-size: 13.5px;
    font-weight: 600;
    margin-bottom: 2px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .control-info .desc {
    font-size: 11.5px;
    color: var(--text-dim);
    line-height: 1.35;
  }
  /* iOS / Apple TV style switch */
  .switch {
    position: relative;
    display: inline-block;
    width: 44px;
    height: 25px;
    flex-shrink: 0;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background-color: rgba(255,255,255,0.16);
    transition: .2s;
    border-radius: 99px;
  }
  .slider:before {
    position: absolute;
    content: "";
    height: 19px;
    width: 19px;
    left: 3px;
    bottom: 3px;
    background-color: white;
    transition: .2s;
    border-radius: 50%;
  }
  input:checked + .slider {
    background: linear-gradient(135deg, #2dd4bf, #38bdf8);
  }
  input:checked + .slider:before {
    transform: translateX(19px);
    background-color: #041019;
  }
  .range-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .range-wrap input[type="range"] {
    width: 95px;
    accent-color: var(--accent-teal);
  }
  .range-val {
    font-size: 12.5px;
    font-weight: 700;
    color: var(--accent-teal);
    min-width: 36px;
    text-align: right;
  }
  .apps-columns {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }
  @media (max-width: 720px) {
    .apps-columns { grid-template-columns: 1fr; }
  }
  .app-list-box {
    background: rgba(11, 15, 25, 0.65);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px;
    max-height: 340px;
    overflow-y: auto;
  }
  .app-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 7px 9px;
    border-radius: 8px;
    font-size: 12.5px;
    gap: 8px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .app-row:last-child { border-bottom: none; }
  .app-row:hover { background: rgba(255,255,255,0.04); }
  .app-name {
    font-weight: 600;
    color: var(--text);
  }
  .app-pkg {
    font-size: 11px;
    color: var(--text-dim);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .order-btns {
    display: flex;
    gap: 4px;
  }
  .order-btns button {
    background: rgba(255,255,255,0.08);
    border: none;
    color: var(--text);
    border-radius: 5px;
    padding: 2px 6px;
    cursor: pointer;
    font-size: 11px;
  }
  .order-btns button:hover { background: rgba(255,255,255,0.18); }
  .right-rail {
    position: sticky;
    top: 84px;
  }
  .tv-preview-frame {
    width: 100%;
    aspect-ratio: 16 / 9;
    background: #04060a;
    border-radius: 12px;
    border: 2px solid rgba(255,255,255,0.13);
    overflow: hidden;
    position: relative;
    box-shadow: 0 14px 36px rgba(0,0,0,0.65);
  }
  .tv-preview-frame img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .remote-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 7px;
    margin-top: 12px;
  }
  .log-box {
    background: #05070c;
    border: 1px solid var(--border);
    border-radius: 11px;
    padding: 11px 13px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11.5px;
    color: #cbd5e1;
    max-height: 220px;
    overflow-y: auto;
    white-space: pre-wrap;
    line-height: 1.45;
  }
  .bottom-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    z-index: 50;
    backdrop-filter: blur(22px) saturate(160%);
    -webkit-backdrop-filter: blur(22px) saturate(160%);
    background: rgba(9, 13, 22, 0.88);
    border-top: 1px solid var(--border);
    padding: 13px 28px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }
  .status-banner {
    font-size: 13px;
    color: var(--text-dim);
    display: flex;
    align-items: center;
    gap: 9px;
  }
  .pill-tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    background: rgba(45, 212, 191, 0.15);
    color: var(--accent-teal);
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-logo">TV</div>
    <div>
      <h1>CleanYourTV v2.7 Studio</h1>
      <p>Local macOS &amp; Linux Configuration &amp; Theme Control Center</p>
    </div>
  </div>

  <div class="tv-selector-bar">
    <div class="tv-select-wrap">
      <div class="status-dot" id="connDot"></div>
      <span style="font-size:12px;color:var(--text-dim);">Target TV:</span>
      <select id="tvHostSelect" onchange="onTvHostChange()"></select>
    </div>
    <button class="btn" onclick="checkHealth()" id="btnCheck">Check Health</button>
    <button class="btn" onclick="bounceCec()" id="btnBounceCec" title="Reset Sony Bravia Sync &amp; HDMI-CEC handshake (fixes Apple TV auto-wake)">⚡ Bounce HDMI-CEC</button>
    <button class="btn" onclick="resetTheme()" id="btnReset">Reset Theme to Default</button>
    <button class="btn btn-danger" onclick="restoreToStock()" id="btnRestore">Restore TV to Stock</button>
  </div>
</header>

<div class="container">
  <div class="left-col">

    <!-- 1. Visual Theme Picker -->
    <div class="section-card">
      <div class="section-header">
        <div>
          <h2>1. Choose a Base Theme</h2>
          <span class="sub">Click any theme card to load its settings below, or push it directly to the TV in one click.</span>
        </div>
        <span class="pill-tag" id="activeBaseBadge">Base: glass</span>
      </div>
      <div class="theme-grid" id="themeGrid"></div>
    </div>

    <!-- 2. Look, Wallpaper & Hero Shelf -->
    <div class="section-card">
      <div class="section-header">
        <div>
          <h2>2. Look, Hero Shelf &amp; Atmosphere</h2>
          <span class="sub">Cinematic top shelf, frosted glass dock, time-of-day sky gradient, and focus glow.</span>
        </div>
      </div>
      <div class="controls-grid">
        <div class="control-item">
          <div class="control-info">
            <div class="label">Cinematic Top Shelf</div>
            <div class="desc">Focused dock app's artwork fills the top shelf with dock dots &amp; "Last opened".</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_cinematic_home" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Hero Greeting Name</div>
            <div class="desc">Shown after GOOD MORNING / EVENING (leave blank for greeting only).</div>
          </div>
          <input type="text" id="cfg_greeting_name" placeholder="e.g. Alex" style="width:125px;" oninput="syncPreviewJson()">
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Frosted Glass Dock</div>
            <div class="desc">Render dock band as translucent frosted glass instead of solid dark slab.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_dock_glass" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Dock Opacity</div>
            <div class="desc">Let the wallpaper or lake reflection show through the dock band.</div>
          </div>
          <select id="cfg_dock_opacity" onchange="syncPreviewJson()">
            <option value="100">100% (Solid)</option>
            <option value="70">70% (Ruska)</option>
            <option value="50">50% (Sweden)</option>
            <option value="30">30% (Ultra Clear)</option>
          </select>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Show Theme Wallpaper</div>
            <div class="desc">Display the selected base theme's wallpaper or rotating landscape collection.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_wallpaper" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Wallpaper Dimming</div>
            <div class="desc">Darken bright wallpapers so white text and tiles stay crisp (0–90%).</div>
          </div>
          <div class="range-wrap">
            <input type="range" min="0" max="90" step="5" id="cfg_wallpaper_dim" oninput="updateRange('cfg_wallpaper_dim','val_wallpaper_dim','%');syncPreviewJson()">
            <span class="range-val" id="val_wallpaper_dim">0%</span>
          </div>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Time-of-Day Sky Gradient</div>
            <div class="desc">Dynamic dawn, day, sunset &amp; night horizon backdrop (`sky_intensity` 0–100%).</div>
          </div>
          <div class="range-wrap">
            <input type="range" min="0" max="100" step="5" id="cfg_sky_intensity" oninput="updateRange('cfg_sky_intensity','val_sky_intensity','%');syncPreviewJson()">
            <span class="range-val" id="val_sky_intensity">0%</span>
          </div>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Background Color</div>
            <div class="desc">Base canvas color behind wallpaper &amp; glow (`#000000` for pure OLED black).</div>
          </div>
          <input type="color" id="cfg_background_color" value="#090C14" onchange="syncPreviewJson()" style="width:46px;height:32px;border:none;background:transparent;cursor:pointer;">
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Ambient Dock Glow</div>
            <div class="desc">Soft color wash behind the dock matching the focused app (red for Netflix/YouTube).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_ambient_glow" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">App-Color Tile Focus Glow</div>
            <div class="desc">Every focused app tile glows in that app's brand color (`app_glow`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_app_glow" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Glow Shape (`glow_style`)</div>
            <div class="desc">Radial spotlight behind the tile or full-width horizon wash from the top edge.</div>
          </div>
          <select id="cfg_glow_style" onchange="syncPreviewJson()">
            <option value="radial">Radial</option>
            <option value="horizon">Horizon</option>
          </select>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Fade Scrolling Rows</div>
            <div class="desc">Grid rows softly fade out under the clock when scrolling (`fade_rows`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_fade_rows" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">32-bit True-Color Wallpaper</div>
            <div class="desc">Prevent gradient banding on dark night skies (`wallpaper_true_color`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_wallpaper_true_color" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Wallpaper Captions</div>
            <div class="desc">Show location/scene title under the top-right buttons for rotating themes.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_wallpaper_captions" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>
      </div>
    </div>

    <!-- 3. Grid Layout & Top Bar Pills -->
    <div class="section-card">
      <div class="section-header">
        <div>
          <h2>3. Grid Layout &amp; Top Bar Pills</h2>
          <span class="sub">Customize tile columns, rows under the dock, and top-right header pills.</span>
        </div>
      </div>
      <div class="controls-grid">
        <div class="control-item">
          <div class="control-info">
            <div class="label">Columns per Row</div>
            <div class="desc">Number of app tiles per row (4 = large 180dp tiles, 6 = compact tiles).</div>
          </div>
          <select id="cfg_columns" onchange="syncPreviewJson()">
            <option value="4">4 Columns (Standard)</option>
            <option value="5">5 Columns</option>
            <option value="6">6 Columns (Compact)</option>
            <option value="7">7 Columns</option>
            <option value="8">8 Columns</option>
          </select>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Rows Visible Below Dock</div>
            <div class="desc">0 keeps the dock at the bottom; 1–3 reveals app grid rows underneath.</div>
          </div>
          <select id="cfg_rows_below_dock" onchange="syncPreviewJson()">
            <option value="0">0 Rows (Dock at Bottom)</option>
            <option value="1">1 Row Below Dock</option>
            <option value="2">2 Rows Below Dock</option>
            <option value="3">3 Rows Below Dock</option>
          </select>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Dedicated Dock Row</div>
            <div class="desc">Show the dock in its own band (`dock_row`). Off = single unified grid.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_dock_row" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Large Dock Title</div>
            <div class="desc">Show the focused dock app's title in large type above the dock (`dock_title`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_dock_title" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Auto-Fill Empty Dock Slots</div>
            <div class="desc">Fill unused dock slots with the first grid apps so the dock has no gaps.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_fill_dock" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Always Show App Names</div>
            <div class="desc">Show dimmed app names under every tile, not just the focused tile.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_always_show_names" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Icon-Only Wi-Fi &amp; Settings</div>
            <div class="desc">Render Wi-Fi and Settings as sleek round icon buttons (`icon_only_buttons`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_icon_only_buttons" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Icon-Only Inputs Button</div>
            <div class="desc">Render the Inputs pill as a round icon button too (`icon_only_inputs`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_icon_only_inputs" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Single Gear Top Menu</div>
            <div class="desc">Fold Inputs, Wi-Fi &amp; Settings into one round gear menu (`top_menu`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_top_menu" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Green Online Wi-Fi Indicator</div>
            <div class="desc">Tint the Wi-Fi icon emerald green when connected to the internet.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_wifi_status_color" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Show Inputs Pill</div>
            <div class="desc">Show the HDMI / Live TV Inputs switcher pill in the top bar.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_show_inputs_pill" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Show Network Pill</div>
            <div class="desc">Show the Wi-Fi / Ethernet pill in the top bar.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_show_network_pill" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Show Reorder Button</div>
            <div class="desc">Dedicated two-arrow tile reorder button in the header (`reorder_button`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_reorder_button" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">System Stats Under Clock</div>
            <div class="desc">Show live CPU %, free RAM, and free storage line below the clock (`show_stats`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_show_stats" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>
      </div>
    </div>

    <!-- 4. Behavior, Motion & F1 Mode -->
    <div class="section-card">
      <div class="section-header">
        <div>
          <h2>4. Behavior, Motion &amp; F1 Race Weekend Mode</h2>
          <span class="sub">Fine-tune animations, long-press tile menu, idle clock screensaver, and F1 countdowns.</span>
        </div>
      </div>
      <div class="controls-grid">
        <div class="control-item">
          <div class="control-info">
            <div class="label">F1 Race Weekend Countdown</div>
            <div class="desc">Shows live/next F1 session countdown under the clock on race weekends (Austin GP this weekend!).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_f1_race_mode" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Long-Press OK Tile Menu</div>
            <div class="desc">Hold OK on any app tile for a quick popup: Move / Reorder, Hide, or App Info (`tile_menu`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_tile_menu" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">App Library Drawer</div>
            <div class="desc">Add an App Library drawer below the grid to browse &amp; toggle all installed apps.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_app_library" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Advanced Zoom &amp; Lift Effects</div>
            <div class="desc">Lifted focused tile, icon zoom, smooth ambient color &amp; app launch transitions.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_advanced_effects" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Focus Spring Overshoot</div>
            <div class="desc">Focused tile springs slightly past its target; off glides smoothly without bounce.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_focus_overshoot" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Reduced Motion (Fast GPU)</div>
            <div class="desc">Ultra-snappy &lt;90ms transitions with zero fade/bounce for maximum responsiveness.</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_reduced_motion" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Idle Clock Screensaver</div>
            <div class="desc">Minutes on Home before the drifting minimalist clock screensaver starts.</div>
          </div>
          <select id="cfg_home_idle_clock_minutes" onchange="syncPreviewJson()">
            <option value="0">Off (System Default)</option>
            <option value="5">5 Minutes</option>
            <option value="10">10 Minutes</option>
            <option value="15">15 Minutes</option>
            <option value="30">30 Minutes</option>
          </select>
        </div>

        <div class="control-item">
          <div class="control-info">
            <div class="label">Boot Straight to Live TV</div>
            <div class="desc">Open Sony Live TV (`com.sony.dtv.tvx`) automatically on first boot (`boot_to_live_tv`).</div>
          </div>
          <label class="switch"><input type="checkbox" id="cfg_boot_to_live_tv" onchange="syncPreviewJson()"><span class="slider"></span></label>
        </div>
      </div>
    </div>

    <!-- 5. Dock Apps & Hidden Apps -->
    <div class="section-card">
      <div class="section-header">
        <div>
          <h2>5. Dock Apps &amp; Hidden Apps</h2>
          <span class="sub">Check apps to pin to the bottom dock (and reorder with ↑/↓), or hide system clutter from the grid.</span>
        </div>
        <button class="btn" onclick="refreshInstalledApps()" id="btnScanApps">Scan Live Apps from TV</button>
      </div>
      <div class="apps-columns">
        <div>
          <div style="font-size:13px;font-weight:700;margin-bottom:8px;color:var(--accent-teal);">Pinned Dock Apps (in order)</div>
          <div class="app-list-box" id="dockAppsList"></div>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <input type="text" id="customDockPkg" placeholder="Add package (e.g. org.xbmc.kodi)" style="flex:1;">
            <button class="btn" onclick="addCustomDockApp()">+ Pin</button>
          </div>
        </div>
        <div>
          <div style="font-size:13px;font-weight:700;margin-bottom:8px;color:#fda4af;">Hidden Apps (excluded from grid)</div>
          <div class="app-list-box" id="hiddenAppsList"></div>
          <div style="display:flex;gap:8px;margin-top:8px;">
            <input type="text" id="customHiddenPkg" placeholder="Hide package name..." style="flex:1;">
            <button class="btn" onclick="addCustomHiddenApp()">+ Hide</button>
          </div>
        </div>
      </div>
    </div>

  </div>

  <!-- Right Rail: Live TV Screen & Remote Control + Config Preview -->
  <div class="right-rail">
    <div class="section-card">
      <div class="section-header">
        <div>
          <h2>Live TV Screen</h2>
          <span class="sub" id="screenshotTimestamp">Living Room TV</span>
        </div>
        <button class="btn" onclick="refreshScreenshot()" id="btnShot">Refresh Screen</button>
      </div>
      <div class="tv-preview-frame">
        <img id="liveTvImg" src="/api/screenshot?t=0" alt="Live TV Screen">
      </div>
      <div class="remote-grid">
        <button class="btn" onclick="sendKey('HOME')" style="justify-content:center;">Home</button>
        <button class="btn" onclick="sendKey('DPAD_UP')" style="justify-content:center;">▲ Up</button>
        <button class="btn" onclick="sendKey('BACK')" style="justify-content:center;">Back</button>
        <button class="btn" onclick="sendKey('DPAD_LEFT')" style="justify-content:center;">◀ Left</button>
        <button class="btn" onclick="sendKey('DPAD_CENTER')" style="justify-content:center;">OK</button>
        <button class="btn" onclick="sendKey('DPAD_RIGHT')" style="justify-content:center;">Right ▶</button>
        <button class="btn" onclick="sendKey('MENU')" style="justify-content:center;">Menu</button>
        <button class="btn" onclick="sendKey('DPAD_DOWN')" style="justify-content:center;">▼ Down</button>
        <button class="btn" onclick="sendKey('TV_INPUT')" style="justify-content:center;">Inputs</button>
      </div>
    </div>

    <div class="section-card">
      <div class="section-header">
        <h2>Activity &amp; ADB Output</h2>
        <span class="sub" id="statusPill">Ready</span>
      </div>
      <div class="log-box" id="logOutput">CleanYourTV v2.7 Control Center ready.
Select a theme or toggle any setting, then click "Apply Configuration to TV".</div>
    </div>

    <div class="section-card">
      <div class="section-header">
        <h2>Generated config.json</h2>
        <span class="sub">themes/custom/config.json</span>
      </div>
      <div class="log-box" id="jsonPreview" style="max-height:170px;color:#93c5fd;"></div>
    </div>
  </div>
</div>

<div class="bottom-bar">
  <div class="status-banner">
    <span class="pill-tag" id="footerTargetLabel">192.0.2.10</span>
    <span id="footerStatusText">Changes are applied live over ADB without rebuilding the APK.</span>
  </div>
  <div style="display:flex;gap:12px;">
    <button class="btn" onclick="refreshScreenshot()">Capture Screenshot</button>
    <button class="btn btn-primary" onclick="applyCustomConfig()" id="btnApply">Apply Configuration to TV</button>
  </div>
</div>

<script>
let STATE = {};
let THEMES = [];
let APPS = [];
let TVS = [];

const BOOL_IDS = [
  "cinematic_home", "dock_glass", "wallpaper", "ambient_glow", "app_glow",
  "fade_rows", "wallpaper_true_color", "wallpaper_captions", "dock_row",
  "dock_title", "fill_dock", "always_show_names", "icon_only_buttons",
  "icon_only_inputs", "top_menu", "wifi_status_color", "show_inputs_pill",
  "show_network_pill", "reorder_button", "show_stats", "f1_race_mode",
  "tile_menu", "app_library", "advanced_effects", "focus_overshoot",
  "reduced_motion", "boot_to_live_tv"
];

function updateRange(inputId, labelId, suffix) {
  const el = document.getElementById(inputId);
  const lbl = document.getElementById(labelId);
  if (el && lbl) lbl.textContent = el.value + suffix;
}

function logMsg(msg) {
  const box = document.getElementById("logOutput");
  const now = new Date().toLocaleTimeString();
  box.textContent = `[${now}] ${msg}\n\n` + box.textContent;
}

function setBusy(busy, label) {
  const ids = ["btnApply", "btnCheck", "btnBounceCec", "btnReset", "btnRestore", "btnShot", "btnScanApps"];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.disabled = busy;
  });
  document.getElementById("statusPill").textContent = label || (busy ? "Working..." : "Ready");
  document.getElementById("footerStatusText").textContent = label || "Ready.";
}

async function init() {
  const res = await fetch("/api/state");
  const data = await res.json();
  STATE = data.state;
  THEMES = data.themes;
  APPS = data.apps;
  TVS = data.tvs;

  const sel = document.getElementById("tvHostSelect");
  sel.innerHTML = "";
  TVS.forEach(tv => {
    const opt = document.createElement("option");
    opt.value = tv.host;
    opt.textContent = `${tv.name} (${tv.host} — ${tv.model})`;
    if (tv.host === STATE.tv_host) opt.selected = true;
    sel.appendChild(opt);
  });

  renderThemes();
  populateFormFromState();
  renderAppLists();
  syncPreviewJson();
}

function onTvHostChange() {
  STATE.tv_host = document.getElementById("tvHostSelect").value;
  document.getElementById("footerTargetLabel").textContent = STATE.tv_host;
  syncPreviewJson();
}

function renderThemes() {
  const grid = document.getElementById("themeGrid");
  grid.innerHTML = "";
  THEMES.forEach(t => {
    const card = document.createElement("div");
    card.className = "theme-card" + (STATE.base_theme === t.id ? " active" : "");
    card.onclick = () => selectThemePreset(t.id, false);
    card.innerHTML = `
      <img class="theme-thumb" src="${t.screenshot_url}" alt="${t.title}" loading="lazy">
      <div class="theme-body">
        <div>
          <div class="theme-title-row">
            <span class="theme-title">${t.title}</span>
            <span class="theme-badge">${t.badge}</span>
          </div>
          <p class="theme-desc">${t.description}</p>
        </div>
        <div class="theme-actions">
          <button class="btn" onclick="event.stopPropagation();selectThemePreset('${t.id}', false)">Load Settings</button>
          <button class="btn btn-primary" onclick="event.stopPropagation();selectThemePreset('${t.id}', true)">Apply Now</button>
        </div>
      </div>
    `;
    grid.appendChild(card);
  });
  document.getElementById("activeBaseBadge").textContent = "Base: " + (STATE.base_theme || "glass");
}

function selectThemePreset(themeId, applyImmediately) {
  const found = THEMES.find(x => x.id === themeId);
  if (!found) return;
  const keepHost = STATE.tv_host;
  const keepF1 = STATE.f1_race_mode;
  STATE = Object.assign({}, found.merged_config, {
    base_theme: themeId,
    tv_host: keepHost,
    f1_race_mode: keepF1 || Boolean(found.merged_config.f1_race_mode),
  });
  renderThemes();
  populateFormFromState();
  renderAppLists();
  syncPreviewJson();
  logMsg(`Loaded "${found.title}" preset into studio controls.`);
  if (applyImmediately) {
    applyCustomConfig();
  }
}

function populateFormFromState() {
  BOOL_IDS.forEach(k => {
    const el = document.getElementById("cfg_" + k);
    if (el) el.checked = Boolean(STATE[k]);
  });
  document.getElementById("cfg_greeting_name").value = STATE.greeting_name || "";
  document.getElementById("cfg_dock_opacity").value = String(STATE.dock_opacity ?? 100);
  document.getElementById("cfg_wallpaper_dim").value = String(STATE.wallpaper_dim ?? 0);
  updateRange("cfg_wallpaper_dim", "val_wallpaper_dim", "%");
  document.getElementById("cfg_sky_intensity").value = String(STATE.sky_intensity ?? 0);
  updateRange("cfg_sky_intensity", "val_sky_intensity", "%");
  document.getElementById("cfg_background_color").value = STATE.background_color || "#000000";
  document.getElementById("cfg_glow_style").value = STATE.glow_style || "radial";
  document.getElementById("cfg_columns").value = String(STATE.columns ?? 4);
  document.getElementById("cfg_rows_below_dock").value = String(STATE.rows_below_dock ?? 0);
  document.getElementById("cfg_home_idle_clock_minutes").value = String(STATE.home_idle_clock_minutes ?? 0);
  document.getElementById("footerTargetLabel").textContent = STATE.tv_host || "192.0.2.10";
}

function readFormIntoState() {
  BOOL_IDS.forEach(k => {
    const el = document.getElementById("cfg_" + k);
    if (el) STATE[k] = Boolean(el.checked);
  });
  STATE.greeting_name = document.getElementById("cfg_greeting_name").value.trim();
  STATE.dock_opacity = parseInt(document.getElementById("cfg_dock_opacity").value, 10) || 100;
  STATE.wallpaper_dim = parseInt(document.getElementById("cfg_wallpaper_dim").value, 10) || 0;
  STATE.sky_intensity = parseInt(document.getElementById("cfg_sky_intensity").value, 10) || 0;
  STATE.background_color = document.getElementById("cfg_background_color").value || "#000000";
  STATE.glow_style = document.getElementById("cfg_glow_style").value || "radial";
  STATE.columns = parseInt(document.getElementById("cfg_columns").value, 10) || 4;
  STATE.rows_below_dock = parseInt(document.getElementById("cfg_rows_below_dock").value, 10) || 0;
  STATE.home_idle_clock_minutes = parseInt(document.getElementById("cfg_home_idle_clock_minutes").value, 10) || 0;
  STATE.tv_host = document.getElementById("tvHostSelect").value || "192.0.2.10";
}

function renderAppLists() {
  const dockBox = document.getElementById("dockAppsList");
  const hiddenBox = document.getElementById("hiddenAppsList");
  dockBox.innerHTML = "";
  hiddenBox.innerHTML = "";

  const dockList = Array.isArray(STATE.dock_apps) ? STATE.dock_apps : [];
  const hiddenList = Array.isArray(STATE.hidden_apps) ? STATE.hidden_apps : [];

  // Ensure all apps in dockList or hiddenList exist in APPS
  const knownPkgs = new Set(APPS.map(a => a.pkg));
  dockList.concat(hiddenList).forEach(pkg => {
    if (!knownPkgs.has(pkg)) {
      knownPkgs.add(pkg);
      APPS.push({ pkg, label: pkg.split(".").pop(), category: "Custom", installed: true });
    }
  });

  // Sort dock box: pinned apps in exact dock order first, then unpinned apps
  const pinnedObjs = dockList.map(pkg => APPS.find(a => a.pkg === pkg) || { pkg, label: pkg });
  const unpinnedObjs = APPS.filter(a => !dockList.includes(a.pkg));

  pinnedObjs.forEach((app, idx) => {
    const row = document.createElement("div");
    row.className = "app-row";
    row.innerHTML = `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1;">
        <input type="checkbox" checked onchange="toggleDockApp('${app.pkg}', this.checked)">
        <div>
          <div class="app-name">#${idx + 1} ${app.label}</div>
          <div class="app-pkg">${app.pkg}</div>
        </div>
      </label>
      <div class="order-btns">
        <button onclick="moveDockApp(${idx}, -1)" title="Move Up">↑</button>
        <button onclick="moveDockApp(${idx}, 1)" title="Move Down">↓</button>
      </div>
    `;
    dockBox.appendChild(row);
  });

  unpinnedObjs.forEach(app => {
    const row = document.createElement("div");
    row.className = "app-row";
    row.innerHTML = `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1;">
        <input type="checkbox" onchange="toggleDockApp('${app.pkg}', this.checked)">
        <div>
          <div class="app-name">${app.label}</div>
          <div class="app-pkg">${app.pkg}</div>
        </div>
      </label>
    `;
    dockBox.appendChild(row);
  });

  // Sort hidden box: currently hidden first, then others
  const sortedForHidden = [...APPS].sort((a, b) => {
    const ah = hiddenList.includes(a.pkg) ? 0 : 1;
    const bh = hiddenList.includes(b.pkg) ? 0 : 1;
    return ah - bh || a.label.localeCompare(b.label);
  });

  sortedForHidden.forEach(app => {
    const isHidden = hiddenList.includes(app.pkg);
    const row = document.createElement("div");
    row.className = "app-row";
    row.innerHTML = `
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;flex:1;">
        <input type="checkbox" ${isHidden ? "checked" : ""} onchange="toggleHiddenApp('${app.pkg}', this.checked)">
        <div>
          <div class="app-name">${app.label}</div>
          <div class="app-pkg">${app.pkg}</div>
        </div>
      </label>
    `;
    hiddenBox.appendChild(row);
  });
}

function toggleDockApp(pkg, checked) {
  let list = Array.isArray(STATE.dock_apps) ? [...STATE.dock_apps] : [];
  if (checked && !list.includes(pkg)) list.push(pkg);
  if (!checked) list = list.filter(x => x !== pkg);
  STATE.dock_apps = list;
  renderAppLists();
  syncPreviewJson();
}

function moveDockApp(idx, dir) {
  let list = Array.isArray(STATE.dock_apps) ? [...STATE.dock_apps] : [];
  const target = idx + dir;
  if (target < 0 || target >= list.length) return;
  const tmp = list[idx];
  list[idx] = list[target];
  list[target] = tmp;
  STATE.dock_apps = list;
  renderAppLists();
  syncPreviewJson();
}

function addCustomDockApp() {
  const inp = document.getElementById("customDockPkg");
  const pkg = inp.value.trim();
  if (!pkg) return;
  inp.value = "";
  toggleDockApp(pkg, true);
}

function toggleHiddenApp(pkg, checked) {
  let list = Array.isArray(STATE.hidden_apps) ? [...STATE.hidden_apps] : [];
  if (checked && !list.includes(pkg)) list.push(pkg);
  if (!checked) list = list.filter(x => x !== pkg);
  STATE.hidden_apps = list;
  renderAppLists();
  syncPreviewJson();
}

function addCustomHiddenApp() {
  const inp = document.getElementById("customHiddenPkg");
  const pkg = inp.value.trim();
  if (!pkg) return;
  inp.value = "";
  toggleHiddenApp(pkg, true);
}

function syncPreviewJson() {
  readFormIntoState();
  const preview = Object.assign({}, STATE);
  delete preview.tv_host;
  document.getElementById("jsonPreview").textContent = JSON.stringify(preview, null, 2);
}

function reloadScreenshotImg() {
  const img = document.getElementById("liveTvImg");
  img.src = "/api/screenshot?t=" + Date.now();
  document.getElementById("screenshotTimestamp").textContent =
    `${STATE.tv_host} • Updated ${new Date().toLocaleTimeString()}`;
}

async function applyCustomConfig() {
  readFormIntoState();
  setBusy(true, "Pushing custom theme & configuration to TV...");
  logMsg(`Applying theme (base: ${STATE.base_theme}) to ${STATE.tv_host}...`);
  try {
    const res = await fetch("/api/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(STATE),
    });
    const data = await res.json();
    logMsg(data.output || (data.ok ? "Applied!" : "Failed."));
    if (data.screenshot_ok) reloadScreenshotImg();
  } catch (e) {
    logMsg("Error applying configuration: " + e);
  } finally {
    setBusy(false, "Configuration applied to TV!");
  }
}

async function resetTheme() {
  readFormIntoState();
  setBusy(true, "Resetting theme overlay to built-in config.xml...");
  logMsg(`Running tools/theme.sh --reset on ${STATE.tv_host}...`);
  try {
    const res = await fetch("/api/reset-theme", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host }),
    });
    const data = await res.json();
    logMsg(data.output || "Theme reset complete.");
    if (data.screenshot_ok) reloadScreenshotImg();
  } catch (e) {
    logMsg("Error: " + e);
  } finally {
    setBusy(false, "Reset complete.");
  }
}

async function refreshScreenshot() {
  readFormIntoState();
  setBusy(true, "Capturing live screenshot from TV...");
  try {
    const res = await fetch("/api/capture-screenshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host }),
    });
    const data = await res.json();
    if (data.ok) {
      reloadScreenshotImg();
      logMsg("Captured fresh screenshot from " + STATE.tv_host);
    } else {
      logMsg("Screenshot error: " + data.output);
    }
  } catch (e) {
    logMsg("Error: " + e);
  } finally {
    setBusy(false, "Ready");
  }
}

async function sendKey(keyName) {
  readFormIntoState();
  setBusy(true, `Sending ${keyName} to TV...`);
  try {
    const res = await fetch("/api/remote-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host, key: keyName }),
    });
    const data = await res.json();
    if (data.ok) {
      reloadScreenshotImg();
      logMsg(`Sent key ${keyName} and updated live screen.`);
    } else {
      logMsg(`Key error: ${data.output}`);
    }
  } catch (e) {
    logMsg("Error: " + e);
  } finally {
    setBusy(false, "Ready");
  }
}

async function checkHealth() {
  readFormIntoState();
  setBusy(true, "Running check.py drift & health check...");
  try {
    const res = await fetch("/api/check-health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host }),
    });
    const data = await res.json();
    logMsg(data.output);
  } catch (e) {
    logMsg("Error: " + e);
  } finally {
    setBusy(false, "Health check finished.");
  }
}

async function bounceCec() {
  readFormIntoState();
  setBusy(true, "Bouncing Sony Bravia Sync & HDMI-CEC handshake (~10s)...");
  logMsg(`Bouncing HDMI-CEC & BraviaSync on ${STATE.tv_host} and re-registering connected HDMI devices...`);
  try {
    const res = await fetch("/api/bounce-cec", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host }),
    });
    const data = await res.json();
    logMsg(data.output || (data.ok ? "HDMI-CEC bounce complete." : "HDMI-CEC bounce failed."));
  } catch (e) {
    logMsg("Error bouncing HDMI-CEC: " + e);
  } finally {
    setBusy(false, "HDMI-CEC handshake reset complete.");
  }
}

async function refreshInstalledApps() {
  readFormIntoState();
  setBusy(true, "Querying installed packages on TV...");
  try {
    const res = await fetch("/api/scan-apps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host }),
    });
    const data = await res.json();
    if (data.apps) {
      APPS = data.apps;
      renderAppLists();
      logMsg(`Scanned ${APPS.length} apps from ${STATE.tv_host}.`);
    }
  } catch (e) {
    logMsg("Error scanning apps: " + e);
  } finally {
    setBusy(false, "Ready");
  }
}

async function restoreToStock() {
  readFormIntoState();
  if (!confirm(`Are you sure you want to restore ${STATE.tv_host} back to the stock Google/Sony launcher and re-enable all bloatware packages?`)) {
    return;
  }
  setBusy(true, "Restoring TV to stock launcher & packages...");
  logMsg(`Running tools/restore.sh on ${STATE.tv_host}...`);
  try {
    const res = await fetch("/api/restore-stock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tv_host: STATE.tv_host }),
    });
    const data = await res.json();
    logMsg(data.output);
    if (data.screenshot_ok) reloadScreenshotImg();
  } catch (e) {
    logMsg("Error: " + e);
  } finally {
    setBusy(false, "Restore finished.");
  }
}

init();
</script>
</body>
</html>
"""


class ControlCenterHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path, content_type=None):
        if not path.is_file():
            self.send_error(404, "File not found")
            return
        data = path.read_bytes()
        ctype = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            raw = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if route == "/api/state":
            self._send_json({
                "state": load_saved_state(),
                "themes": load_theme_presets(),
                "apps": discover_apps(),
                "tvs": TV_DEVICES,
            })
            return

        if route == "/api/screenshot":
            if LIVE_SCREENSHOT_FILE.is_file():
                self._send_file(LIVE_SCREENSHOT_FILE, "image/png")
                return
            fallback = BACKUP_DIR / "tv-home.png"
            if fallback.is_file():
                self._send_file(fallback, "image/png")
                return
            theme_shot = THEMES_DIR / "glass" / "screenshot.jpg"
            self._send_file(theme_shot, "image/jpeg")
            return

        if route.startswith("/themes/"):
            rel = route[len("/themes/"):]
            if ".." in rel or rel.startswith("/"):
                self.send_error(403, "Forbidden")
                return
            target = (THEMES_DIR / rel).resolve()
            if not str(target).startswith(str(THEMES_DIR.resolve())):
                self.send_error(403, "Forbidden")
                return
            self._send_file(target)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path
        body = self._read_json_body()

        with LOCK:
            if route == "/api/apply":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                save_state(body)
                cfg = build_custom_theme_dir(body)
                rc, out = run_cmd([str(TOOLS_DIR / "theme.sh"), str(CUSTOM_THEME_DIR)], tv_host, timeout=120)
                shot_ok, shot_out = capture_live_screenshot(tv_host)
                self._send_json({
                    "ok": rc == 0,
                    "output": f"$ TV_HOST={tv_host} ./tools/theme.sh themes/custom\n{out}",
                    "screenshot_ok": shot_ok,
                    "config": cfg,
                })
                return

            if route == "/api/reset-theme":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                rc, out = run_cmd([str(TOOLS_DIR / "theme.sh"), "--reset"], tv_host, timeout=60)
                shot_ok, _ = capture_live_screenshot(tv_host)
                self._send_json({
                    "ok": rc == 0,
                    "output": f"$ TV_HOST={tv_host} ./tools/theme.sh --reset\n{out}",
                    "screenshot_ok": shot_ok,
                })
                return

            if route == "/api/capture-screenshot":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                shot_ok, out = capture_live_screenshot(tv_host)
                self._send_json({"ok": shot_ok, "output": out})
                return

            if route == "/api/remote-key":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                key = str(body.get("key") or "HOME").upper()
                if not re.fullmatch(r"[A-Z0-9_]+", key):
                    self._send_json({"ok": False, "output": "Invalid key name"}, status=400)
                    return
                shot_ok, out = capture_live_screenshot(tv_host, keys=[key])
                self._send_json({"ok": shot_ok, "output": out})
                return

            if route == "/api/check-health":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                debloat = str(REPO_DIR / "examples" / "sony-bravia-4k-gb" / "debloat.txt")
                rc, out = run_cmd(
                    [str(VENV_PY), "-I", str(TOOLS_DIR / "check.py"), debloat, "com.gathin.tvhome"],
                    tv_host,
                    timeout=45,
                )
                self._send_json({
                    "ok": rc == 0,
                    "output": f"$ TV_HOST={tv_host} tools/check.py examples/sony-bravia-4k-gb/debloat.txt com.gathin.tvhome\n{out}",
                })
                return

            if route == "/api/bounce-cec":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                cec_script = (
                    "settings put global hdmi_control_enabled 0; "
                    "settings put global hdmi_control_auto_wakeup_enabled 0; "
                    "settings put global hdmi_control_auto_device_off_enabled 0; "
                    "sleep 2; "
                    "am force-stop com.sony.dtv.braviasyncservice 2>/dev/null || true; "
                    "am force-stop com.sony.dtv.braviasyncmenu 2>/dev/null || true; "
                    "am force-stop com.sony.dtv.braviasyncsetting 2>/dev/null || true; "
                    "sleep 1; "
                    "settings put global hdmi_control_enabled 1; "
                    "sleep 2; "
                    "settings put global hdmi_control_auto_wakeup_enabled 1; "
                    "settings put global hdmi_control_auto_device_off_enabled 1; "
                    "pm enable com.sony.dtv.braviasyncservice >/dev/null 2>&1 || true; "
                    "pm enable com.sony.dtv.braviasyncmenu >/dev/null 2>&1 || true; "
                    "pm enable com.sony.dtv.braviasyncsetting >/dev/null 2>&1 || true; "
                    "am startservice -a com.sony.dtv.braviasyncservice.BraviaSyncService "
                    "-n com.sony.dtv.braviasyncservice/.BraviaSyncService >/dev/null 2>&1 || true; "
                    "am broadcast -n com.sony.dtv.braviasyncservice/.GlobalKeyReceiver >/dev/null 2>&1 || true; "
                    "am broadcast -n com.sony.dtv.braviasyncmenu/.RemoteKeyReceiver >/dev/null 2>&1 || true; "
                    "sleep 4; "
                    "dumpsys hdmi_control | head -n 32"
                )
                rc, out = run_cmd(
                    [str(VENV_PY), "-I", str(TOOLS_DIR / "tv.py"), cec_script],
                    tv_host,
                    timeout=45,
                )
                self._send_json({
                    "ok": rc == 0,
                    "output": f"HDMI-CEC & Bravia Sync bounced on {tv_host}:\n{out}",
                })
                return

            if route == "/api/scan-apps":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                rc, out = run_cmd(
                    [str(VENV_PY), "-I", str(TOOLS_DIR / "tv.py"), "pm list packages -e"],
                    tv_host,
                    timeout=30,
                )
                if rc == 0 and out:
                    pkgs = sorted(
                        line.split(":", 1)[1].strip()
                        for line in out.splitlines()
                        if line.startswith("package:")
                    )
                    if BACKUP_JSON.is_file():
                        try:
                            b = json.loads(BACKUP_JSON.read_text(encoding="utf-8"))
                            b["enabled_packages"] = pkgs
                            BACKUP_JSON.write_text(json.dumps(b, indent=2), encoding="utf-8")
                        except Exception:
                            pass
                self._send_json({"ok": rc == 0, "apps": discover_apps(), "output": out})
                return

            if route == "/api/restore-stock":
                tv_host = str(body.get("tv_host") or DEFAULT_TV_HOST)
                rc, out = run_cmd([str(TOOLS_DIR / "restore.sh")], tv_host, timeout=90)
                shot_ok, _ = capture_live_screenshot(tv_host)
                self._send_json({
                    "ok": rc == 0,
                    "output": f"$ TV_HOST={tv_host} ./tools/restore.sh\n{out}",
                    "screenshot_ok": shot_ok,
                })
                return

        self.send_error(404, "Not Found")


def main():
    parser = argparse.ArgumentParser(description="CleanYourTV v2.7 Studio & Control Center Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default 8765)")
    parser.add_argument("--repo", default=None, help="Path to local cleanyourtv repository checkout")
    args = parser.parse_args()

    if args.repo:
        configure_repo_dir(args.repo)

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), ControlCenterHandler)
    print(f"CleanYourTV v2.7 Control Center listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
