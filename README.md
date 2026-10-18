# 4K Wallpaper Changer

A small tray app that changes your desktop wallpaper automatically using images from [4kwallpapers.com](https://4kwallpapers.com), or loops over wallpapers you liked. Dark, modern UI built with CustomTkinter.

> Unofficial personal project. It is not affiliated with 4kwallpapers.com. Wallpapers belong to their respective owners.

## Features

- **Two modes**
  - **Auto**: random wallpaper from selected categories, or from the Recent / Popular / Featured / Random feeds.
  - **Liked loop**: cycles through the wallpapers you liked and ticked. Files are stored locally, so nothing is re-downloaded and it works offline.
- **Offline fallback**: when the site is unreachable, Auto mode loops over the last 20 or 40 saved wallpapers.
- **History navigation**
  - **Prev**: go back to the previous wallpaper.
  - **Next**: go forward in history if you went back, otherwise fetch a new wallpaper.
  - **Hold Next**: a fill animation plays on the button, then it jumps to the latest wallpaper. Pressing Next after that fetches a new one.
- **Likes**: heart any wallpaper from the Home tab or the tray menu. Liked files are copied to a separate folder.
- **Bring your own wallpapers**: drop images into the liked folder and they are added to the liked list automatically (see below).
- **Per-mode timers** (minutes), pause/resume, tray menu, optional start with Windows.
- **Polite to the site**: requests are rate limited (configurable delay).

## Screens

| Tab | What it does |
| --- | --- |
| Home | Current wallpaper, Prev / Next, Like, mode switch, timer |
| Liked | Your liked wallpapers, tick the ones used by the loop, open the page, remove |
| Recent | The newest saved wallpapers, set one or like it |
| Settings | Source, categories, search depth, how many recent files to keep, autostart |

## Install

### Windows installer (recommended)

1. Download `4KWallpaperChanger_Setup.exe` from the [Releases](../../releases) page.
2. Run it. No admin rights are needed. You can choose a desktop shortcut and "Start with Windows".
3. To remove it, use **Settings > Apps > 4K Wallpaper Changer > Uninstall**. The uninstaller removes the autostart entry and asks whether to also delete settings and saved images.

### Run from source

Requires Python 3.10+ (developed on 3.13).

```bash
pip install -r requirements.txt
python wallpaper_changer.py
```

Command line options:

```text
python wallpaper_changer.py                 # open the window + tray icon
pythonw wallpaper_changer.py --hidden       # tray only (Windows, no console)
python wallpaper_changer.py --once          # change the wallpaper once and exit
python wallpaper_changer.py --debug <url>   # print download candidates of a wallpaper page
```

## Adding your own wallpapers to the liked list

Put image files (`.jpg`, `.jpeg`, `.png`, `.webp`) in:

```text
%USERPROFILE%\Pictures\4KWallpaperChanger\liked
```

(the 📁 button on the Liked tab opens this folder). The app picks them up on start, when you open the Liked tab, and before each liked-loop change.

If the file keeps the original 4kwallpapers name, for example `brabus-bodo-1920x1080-27175.jpg`, the app extracts the slug (`brabus-bodo`) and ID (`27175`) from it. The filename does not contain the category, so the app finds the page `https://4kwallpapers.com/<category>/brabus-bodo-27175.html` by trying the categories in the background. The result is saved, so this happens once per file. The ↗ button on the card then opens the page. Files with other names are still added, just without a page link.

Note: the ♥ button on a liked card removes it from the list **and deletes the file** from the liked folder.

## Where data is stored

| What | Location |
| --- | --- |
| Settings, history, likes, thumbnails | `%USERPROFILE%\.4kwall\` |
| Recent wallpapers | `%USERPROFILE%\Pictures\4KWallpaperChanger\recent` |
| Liked wallpapers | `%USERPROFILE%\Pictures\4KWallpaperChanger\liked` |

## Build the .exe yourself

On Windows, with Python and (optionally) [Inno Setup 6](https://jrsoftware.org/isdl.php) installed:

```bat
build_exe.bat
```

This installs the dependencies, creates the icon, builds `dist\4KWallpaperChanger\4KWallpaperChanger.exe` with PyInstaller, and, if Inno Setup is found, builds `Output\4KWallpaperChanger_Setup.exe` from `installer.iss`.

Tips:

- Quit the app (tray menu > Quit) before building, otherwise Windows may lock files in `dist`.
- Keep other files out of `dist\4KWallpaperChanger`, because the installer packs that whole folder.

## Project files

```text
wallpaper_changer.py   the app
requirements.txt       Python dependencies
build_exe.bat          builds the exe and the installer
installer.iss          Inno Setup script (install, autostart, uninstall)
make_icon.py           generates app.ico used by the build
```

## Platform notes

Windows is the main target and the only platform where the installer, autostart, and the packaged exe are provided. The wallpaper setter also has macOS and Linux (GNOME / `feh`) code paths, but they are less tested.

## License

Add your license here (for example MIT).
