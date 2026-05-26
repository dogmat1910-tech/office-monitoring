# Override pyinstaller-hooks-contrib's hook-webrtcvad.
# Тот хук падает с PackageNotFoundError, потому что у нас установлен
# webrtcvad-wheels (distribution name != module name 'webrtcvad').
# Модуль webrtcvad всё равно собирается анализом импортов.
datas = []
