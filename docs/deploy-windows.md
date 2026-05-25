# Развёртывание агента на Windows-ноутбук менеджера

## Что делает установщик

1. Проверяет/устанавливает Python 3.12 (через winget)
2. Скачивает файлы агента в `C:\Program Files\office-monitoring\`
3. Создаёт venv с обходом SOCKS-прокси (флаг `--proxy ""`)
4. Регистрирует Scheduled Task «OfficeMonitoringAgent» с автозапуском при логине пользователя
5. Защищает папку через ACL — обычный пользователь не может удалить файлы
6. Запускает агент сразу

## Установка одной командой

На целевом Windows-ноутбуке открой **PowerShell от имени администратора** (правый клик → Run as Administrator) и выполни:

```powershell
irm https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent/installer.ps1 -UseBasicParsing | iex
```

Скрипт сам всё сделает за 1-3 минуты. В конце выведет статус задачи и команды для управления.

## Проверка что агент работает

```powershell
Get-ScheduledTask -TaskName OfficeMonitoringAgent
```

Состояние должно быть **Running**.

Лог:

```powershell
Get-Content 'C:\ProgramData\office-monitoring\agent.log' -Tail 20 -Wait
```

Через ~30 секунд после установки на дашборде http://89.22.225.88/ ноутбук появится в списке агентов со статусом online.

## Что менеджер увидит

- Иконки в трее **нет** (агент работает скрыто)
- В Task Manager → Details есть процесс `python.exe` (но без админ-прав остановить нельзя)
- В Task Manager → Services → задача `\OfficeMonitoringAgent` (скрыта от обычных пользователей)
- Файлы в `C:\Program Files\office-monitoring\` — без прав админа удалить нельзя

## Что менеджер НЕ сможет сделать

- Остановить агент без админ-прав
- Удалить файлы агента
- Помешать автостарту при логине

## Что менеджер сможет (известные дыры)

- **Завершить процесс через Task Manager** (если он запущен под его учёткой) — но Scheduled Task сразу его перезапустит (RestartCount=9999, RestartInterval=1 минута)
- **Удалить с админ-правами** — против админа защиты нет

## Деинсталляция

```powershell
irm https://raw.githubusercontent.com/dogmat1910-tech/office-monitoring/main/agent/uninstall.ps1 -UseBasicParsing | iex
```

## Если что-то пошло не так

Логи задачи:

```powershell
Get-ScheduledTask -TaskName OfficeMonitoringAgent | Get-ScheduledTaskInfo
```

Лог самого агента:

```powershell
Get-Content 'C:\ProgramData\office-monitoring\agent.log' -Tail 100
```

## Юридическая сторона

Перед установкой убедись что у менеджера подписаны:
- Согласие на обработку персональных данных (включая аудиозапись и активность на ПК)
- Пункт в трудовом договоре о мониторинге рабочего оборудования

Без этого запуск незаконен по 152-ФЗ и 138 УК (нарушение тайны переговоров).
