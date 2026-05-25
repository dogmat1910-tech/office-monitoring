# Установка агента на Windows-ноут (Шаг 1: heartbeat)

Эта инструкция для **разового тестового запуска** на одном корп. ноуте, чтобы убедиться, что агент соединяется с сервером и шлёт heartbeats. Авто-старт как Windows Service + защита от выключения — будут на Шаге 8.

## Что должно получиться в конце

В окне PowerShell бежит лог вида `heartbeat ok: ...`. На сервере по адресу http://89.22.225.88/agents виден один агент со статусом `"online": true`.

---

## 1. Установить Python 3.12 (если ещё нет)

Открой **PowerShell** (не от админа) и проверь, есть ли Python:

```powershell
python --version
```

Если выводит `Python 3.12.x` или `Python 3.13.x` — переходи к шагу 2. Если ошибка «не найден» — ставь через winget:

```powershell
winget install --id Python.Python.3.12 -e --source winget
```

После установки **закрой и открой PowerShell заново**, и снова проверь:

```powershell
python --version
```

---

## 2. Создать рабочую папку

```powershell
mkdir C:\office-monitoring
```

```powershell
cd C:\office-monitoring
```

---

## 3. Скачать файл агента

```powershell
curl.exe -o agent.py https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent/agent.py
```

> ⚠️ Репозиторий пока **приватный** — эта команда не сработает без токена. Временное решение: попроси Claude (меня) прислать содержимое `agent.py` сюда в чат, скопируй в файл вручную через `notepad agent.py`. На Шаге 8 сделаем нормальный установщик через релизы GitHub.

---

## 4. Создать виртуальное окружение и поставить зависимости

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

Если PowerShell выдаст ошибку про `ExecutionPolicy` — выполни один раз:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

И снова `.\.venv\Scripts\Activate.ps1`.

Теперь в начале строки должно появиться `(.venv)`. Дальше ставим httpx:

```powershell
pip install httpx
```

---

## 5. Запустить агент

В том же окне PowerShell (с активным `.venv`):

```powershell
$env:OM_SERVER_URL = "http://89.22.225.88"
```

```powershell
python agent.py
```

Должно появиться:

```
2026-05-25 ... [INFO] agent starting: version=0.1.0 os=Windows-... hostname=... username=... agent_id=... server=http://89.22.225.88
2026-05-25 ... [INFO] heartbeat ok: {'status': 'ok', 'server_time': '...'}
```

Каждые 30 секунд — новая строка `heartbeat ok`.

---

## 6. Проверить на сервере, что агент виден

В любом браузере открой:

http://89.22.225.88/agents

Должен быть JSON-массив с твоим ноутом и `"online": true`.

---

## Как остановить

В окне PowerShell нажми `Ctrl+C`.

## Известные ограничения этого шага

- Агент работает только пока открыто окно PowerShell.
- При закрытии окна или перезагрузке — выключается.
- Это нормально для Шага 1. Auto-start, защита от выключения и иконка в трее — на Шаге 8.
