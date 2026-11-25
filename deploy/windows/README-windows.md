# MailWorker на Windows Server 2012/2012 R2

Служба Windows: опрос ящика, запись в Firebird, архив SCANNED на SMB-шару,
веб-панель. Docker не нужен.

## Требования

- Windows Server 2012 R2 — Python 3.9 x64;
- Windows Server 2012 (без R2) — Python 3.8 x64;
- права администратора для установки службы;
- доступ к SMB-шаре под учётной записью службы;
- `fbclient.dll` той же разрядности, что Python/exe;
- для `.doc` — `antiword.exe` (или LibreOffice/Office);
- для сборки — PyInstaller и pywin32.

## Сборка

На Windows-машине с нужной версией Python:

```powershell
py -3.8 -m pip install -r deploy\windows\requirements-win.txt
.\deploy\windows\build.ps1 -Python "py" -PythonVersion "-3.8" -FbClient "C:\Firebird\fbclient.dll" -Antiword "C:\tools\antiword.exe"
```

Результат: `dist\MailWorkerService\MailWorkerService.exe` и папка `_internal`.

## Установка

1. Соберите установщик: откройте `deploy\windows\MailWorker.iss` в Inno Setup 6 и нажмите Build.
2. Запустите `MailWorkerSetup.exe` от администратора.
3. Отредактируйте `C:\ProgramData\MailWorker\config.ini` (почта, база, SMB, пароль панели).
4. Перезапустите службу:

```cmd
sc stop MailWorker
sc start MailWorker
sc query MailWorker
```

Панель: `http://IP-сервера:4000`.

## Учётная запись службы

По умолчанию — LocalSystem. Если шара требует доменную учётку:

```cmd
sc config MailWorker obj= "ASUP\sfs" password= "пароль"
sc stop MailWorker
sc start MailWorker
```

Учётка должна иметь доступ к `\\srv-sadko-db\SadkoAI` и к папке
`C:\ProgramData\MailWorker` (на чтение/запись).

## Конфиг

- `[archive] transport = auto` — на Windows архив пишется по UNC-пути
  `\\srv-sadko-db\SadkoAI\FILES\<CARD.ID>\S<SCANNED.ID><ext>`.
- `smbclient.exe` не нужен.
- `[ui] host = 0.0.0.0`, `port = 4000`, пароль обязателен.
- `[processing] state_path` и `[logging] file` — в `C:\ProgramData\MailWorker`.

## Обновление

1. `sc stop MailWorker`.
2. Замените файлы в `C:\Program Files\MailWorker` (или установите новый Setup).
3. `sc start MailWorker`.
4. `sc query MailWorker` и `http://IP:4000`.

Конфиг и данные в `ProgramData` не удаляются.

## Диагностика

```cmd
sc query MailWorker
type C:\ProgramData\MailWorker\logs\mailworker.log
MailWorkerService.exe --check --config C:\ProgramData\MailWorker\config.ini
```

Если служба не стартует, посмотрите Event Viewer → Windows Logs → Application.

## Ограничения

- Windows Server 2012 и Python 3.8/3.9 уже без поддержки — фиксируйте версии.
- `.doc` требует `antiword.exe` или Office/LibreOffice.
- Панель работает по HTTP; снаружи ставьте HTTPS-reverse-proxy.