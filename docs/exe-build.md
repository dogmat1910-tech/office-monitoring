# Сборка Windows-агента в .exe

## Как работает

GitHub Actions при каждом `push` в `main`, затронувшем `agent/**`, запускает
`windows-latest` runner и собирает два `.exe` через PyInstaller:
- `office-monitoring-agent.exe` — сам агент
- `office-monitoring-watchdog.exe` — сторожевой процесс

Артефакты публикуются в **Actions → Workflow run → Artifacts** (хранятся 30 дней).
Также можно собрать вручную через `workflow_dispatch`.

## Где скачать готовый exe

1. Открыть https://github.com/dogmat1910-tech/office-monitoring/actions
2. Выбрать последний успешный run `Build Windows Agent`
3. В разделе **Artifacts** скачать ZIP

Для боевой раскатки имеет смысл **создавать GitHub Release** — тогда `.exe`
автоматически прикрепится к релизу и будет доступен по постоянной ссылке
`https://github.com/.../releases/latest/download/office-monitoring-agent.exe`.

## Что внутри exe

PyInstaller с `--onefile` собирает:
- Python 3.12 интерпретатор
- Все зависимости из requirements.txt
- Все .py-модули агента

Размер итогового `office-monitoring-agent.exe` — около **70-100 MB**.

При запуске exe распаковывается во временную папку `%TEMP%\_MEIxxxxxx`,
оттуда выполняется. Это нормальное поведение `--onefile`.

## Альтернатива: --onedir (быстрее старт)

Если 1-2 секунды распаковки при старте критичны, можно сменить на `--onedir`:
получится папка с `.exe` + DLL. Запуск быстрее, но это уже не один файл,
а ~200 МБ файлов.

## Подпись (обязательно перед прод-раскаткой)

Без подписи Windows SmartScreen покажет «защитил ваш ПК» при первом запуске.
Антивирусы могут блочить.

Решение:
1. Купить **EV Code Signing Certificate** (~30-50 тыс ₽/год)
2. В GitHub Actions добавить шаг подписи через `signtool.exe` с этим сертификатом
3. Использовать GitHub Secrets для хранения cert + password

Пример шага (когда будет cert):
```yaml
- name: Sign agent.exe
  shell: pwsh
  run: |
    signtool sign /f $env:CERT_PATH /p $env:CERT_PASSWORD `
      /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
      agent/dist/office-monitoring-agent.exe
  env:
    CERT_PATH: ${{ secrets.WINDOWS_CERT_PATH }}
    CERT_PASSWORD: ${{ secrets.WINDOWS_CERT_PASSWORD }}
```

Пока без подписи — для тестов норм, для прода нужна.

## Обновление installer.ps1 под .exe

Когда .exe будет в Releases, installer.ps1 будет качать его одним curl'ом
вместо клонирования .py файлов + установки Python + venv + pip.
Это сильно упростит установку (~10 секунд вместо ~3 минут) и избавит от
проблем с SOCKS-прокси для pip.

Этот переход сделаем после первого успешного билда + теста на реальной
Windows-машине.
