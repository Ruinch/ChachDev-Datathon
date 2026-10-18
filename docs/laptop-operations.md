# Локальный кабинет и Qwen до приёмки распознавания

На этом ноутбуке оба сервиса уже работают в фоне. Публичный адрес: https://wadags-laptop.tail32787c.ts.net/ . Funnel направлен только на 8000; локальный разметчик на 8766 не публикуется. Не запускайте второй экземпляр поверх работающих процессов.

Все команды выполнять из корня проекта. Текущий профиль — `C:\Users\bagwa\Documents\ForteBankDatathon\jury`; запускатели находят его через локальный `.local_data/laptop-profile-path.txt`. Это устраняет обнаруженное перенаправление AppData внутри Codex. Прежний профиль сохранён, ключ остался тем же и находится только в `service.env`; файлы видео и ключ в команды не вставлять. Для применения изменений к работающему серверу владелец использует `.\scripts\restart-laptop.ps1`.

Перед перезапуском завершите оценочные прогоны и дождитесь свободного API. Владелец сначала выполняет `.\scripts\restart-laptop.ps1 -CheckOnly`, затем `.\scripts\restart-laptop.ps1`: проверяются процесс API и модель на loopback 8091, точный путь runtime, закреплённые пути весов/проектора, alias и время создания процесса. После остановки API скрипт ждёт штатного завершения модели; если вручную восстановленная модель осталась, повторно проверяет PID и время создания и останавливает только тот же процесс, после чего запускает оба сервиса заново. Неизвестный или заменившийся процесс не завершается; при конфликте запуск останавливается для ручной проверки. `run-laptop.ps1` и Startup по-прежнему не переиспользуют чужую модель автоматически. Эти проверки не подтверждают холодный старт или полный цикл анализа за 300 секунд.

Проверить среду, закреплённые веса и runtime без запуска процессов и скачиваний:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-jury.ps1 -CheckOnly -InterfaceOnly -StartModel
```

Запустить локальный кабинет на `http://127.0.0.1:8000/` и Qwen на `127.0.0.1:8091`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start-jury.ps1 -InterfaceOnly -StartModel
```

Оставить окно открытым. Остановка этого запускателя останавливает только созданный им runtime. Если модель уже отвечает на 8091, запускатель откажет; после проверки её весов и параметров можно явно добавить `-UseExistingModel`. Этот внешний процесс запускатель не останавливает. Одновременно второй кабинет на той же базе не запускать.

`-InterfaceOnly -StartModel` поднимает оба процесса при отключённых правилах, но не разрешает анализ с непринятым каталогом: API честно остаётся `ready=false`. Обычный запуск без `-InterfaceOnly`, включая `-CheckOnly`, сохраняет требование включённых проверенных правил.

В текущем профиле отдельно включён [экспериментальный разбор](experimental-review.md): сайт использует `/api/v1/review`, а готовность этого пути показывает `review.ready`. Он сохраняет наблюдения и исходные кадры без подтверждённого вердикта. Строгий `/analyze` не разблокируется этой настройкой.

## Автозапуск после входа владельца

**Фактический способ на этом ноутбуке — ярлык текущего пользователя Startup**, а не Планировщик:

```text
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ForteBankDatathonLocal.lnk
```

Он запускает `scripts/run-laptop.ps1`. Ярлык реально запущен: Qwen и FastAPI восстановились, runtime ready и публичная главная HTTP 200 проверены. Windows ещё не перезагружалась. Перед перезагрузкой экспортируйте разметку.

После остановки серверов повторный запуск возможен так:

```powershell
$link = Join-Path ([Environment]::GetFolderPath('Startup')) 'ForteBankDatathonLocal.lnk'
Start-Process -FilePath $link -WindowStyle Hidden
```

Автозагрузка настроена на вход владельца; реальную перезагрузку после смены профиля ещё нужно проверить. Работа до входа и автоматический health-watchdog не настроены. Выход из Windows завершает пользовательские серверы. Чтобы отключить автозагрузку, уберите этот ярлык из Startup. Для фонового запуска окно терминала держать открытым не требуется.

Созданная диагностическая задача Планировщика возвращала 1 даже для простого `cmd echo`, не связанного с ML. Точная причина не установлена. Задача архивирована в приватный `jury/logs/task-scheduler-diagnostic.xml` и удалена. Следующий блок сохранён только как альтернативная инструкция для другой исправной Windows; на текущем ноутбуке он не принят.

Следующий блок — инструкция владельцу. Подготовка документа не регистрирует задачу. Закрыть прежние ручные запуски перед первым запуском задачи; автоматическое повторное использование неизвестной модели запрещено.

```powershell
$project = (Get-Location).Path
$launcher = Join-Path $project 'scripts\run-laptop.ps1'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Откройте PowerShell из корня проекта.' }
$taskName = 'ForteBankDatathonLocal'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Задача уже существует. Сначала проверьте её параметры.' }
$owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launcher + '"'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $owner
$principal = New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$definition = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Register-ScheduledTask -TaskName $taskName -InputObject $definition
# Только после остановки прежних ручных запусков:
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTaskInfo -TaskName $taskName
```

Задача запускается после входа владельца Windows; работа до входа и после выхода этим способом не обеспечивается. Перезапуск срабатывает после завершения процесса с ошибкой; зависший процесс отдельным наблюдателем не контролируется. Проверить восстановление реальной перезагрузкой. Работа Tailscale Funnel и режим питания настраиваются отдельно по [инструкции ноутбука](jury-laptop.md).

Обёртка `scripts/run-laptop.ps1` вызывает `start-jury.ps1 -InterfaceOnly -StartModel` и сохраняет каждый запуск в новую закрытую папку `jury\logs\startup-<время>-<id>`. В ней `transcript.log` содержит ход запуска обёртки, `stdout.log` и `stderr.log` — вывод серверного запускателя. При ошибке возвращается код 1 для перезапуска задачи. Ключ остаётся в файле профиля и не передаётся в аргументах. Обёртка проверяет разрешения профиля и логов перед записью, не меняя ACL. Предварительная проверка обёртки без старта серверов: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run-laptop.ps1 -CheckOnly`.

Остановить или отключить эту задачу:

```powershell
Stop-ScheduledTask -TaskName 'ForteBankDatathonLocal'
Disable-ScheduledTask -TaskName 'ForteBankDatathonLocal'
```

Перед повторным запуском проверить, что порты 8000 и 8091 свободны; при принудительной остановке дерева процесса runtime может остаться. Завершать только проверенный процесс этого проекта по его PID, не все Python или llama.cpp на машине.
