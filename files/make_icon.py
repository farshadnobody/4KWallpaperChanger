"""Creates app.ico from the tray icon drawing (used by build_exe.bat)."""
from wallpaper_changer import make_icon_image

img = make_icon_image().resize((256, 256))
img.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("app.ico created")
