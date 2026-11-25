"""Нативная служба Windows через pywin32.

Режимы (см. main.py):
    --install-service   зарегистрировать службу MailWorker
    --remove-service    удалить службу
    --service           точка входа для SCM (запускается службой)

PyInstaller-сборка вызывает этот же модуль; на Linux pywin32 отсутствует,
поэтому импорт защищён.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from .errors import ConfigError

logger = logging.getLogger(__name__)

SERVICE_NAME = "MailWorker"
DISPLAY_NAME = "MailWorker — обработка почты"
DESCRIPTION = "Читает почтовый ящик, пишет документы в журнал CARD и архив SCANNED."

try:  # pywin32 есть только на Windows
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    _HAS_PYWIN32 = True
except ImportError:  # pragma: no cover - зависит от платформы
    _HAS_PYWIN32 = False

_CONFIG_PATH = ""


def _require_pywin32() -> None:
    if not _HAS_PYWIN32:
        raise ConfigError(
            "Для службы Windows нужен pywin32: pip install pywin32 "
            "(в собранном exe он уже включён)"
        )


def _service_args(config_path: str) -> str:
    """Аргументы, которые SCM передаст при старте службы."""
    config = str(Path(config_path).resolve())
    if getattr(sys, "frozen", False):
        return '--service --config "' + config + '"'
    script = str(Path(sys.argv[0]).resolve())
    return '"' + script + '" --service --config "' + config + '"'


if _HAS_PYWIN32:

    class MailWorkerService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._app = None

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._app is not None:
                try:
                    self._app.stop()
                except Exception:  # noqa: BLE001
                    logger.exception("Ошибка остановки службы")
            win32event.SetEvent(self._stop_event)

        def SvcShutdown(self):
            self.SvcStop()

        def SvcDoRun(self):
            from .cli import build_processor
            from .config import AppConfig
            from .logging_setup import setup_logging
            from .service import MailWorkerService as AppService

            try:
                cfg = AppConfig.load(_CONFIG_PATH)
                setup_logging(cfg.logging)
                app = AppService(
                    cfg,
                    build_processor(cfg),
                    processor_factory=lambda: build_processor(cfg),
                )
                self._app = app
                self.ReportServiceStatus(win32service.SERVICE_RUNNING)
                app.run_forever()
            except Exception:  # noqa: BLE001
                logger.exception("Служба MailWorker остановилась из-за ошибки")
            finally:
                self.ReportServiceStatus(win32service.SERVICE_STOPPED)
                win32event.SetEvent(self._stop_event)


def run_service(config_path: str) -> int:
    """Точка входа, которую вызывает SCM (флаг --service)."""
    _require_pywin32()
    global _CONFIG_PATH
    _CONFIG_PATH = str(Path(config_path).resolve())
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(MailWorkerService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0


def install_service(config_path: str) -> int:
    """Регистрирует службу с автозапуском и восстановлением при падении."""
    _require_pywin32()
    config = str(Path(config_path).resolve())
    if not Path(config).is_file():
        raise ConfigError("Файл настроек не найден: " + config)
    # Конфиг может быть ещё не заполнен (установщик ставит службу до правки).
    # Регистрируем всегда, а запускаем только если настройки уже валидны.
    from .config import AppConfig

    config_ready = True
    try:
        AppConfig.load(config)
    except Exception as exc:  # noqa: BLE001
        config_ready = False
        print("Служба будет установлена, но не запущена: " + str(exc))

    exe = sys.executable

    def _install() -> None:
        win32serviceutil.InstallService(
            "processor.winservice.MailWorkerService",
            SERVICE_NAME,
            DISPLAY_NAME,
            startType=win32service.SERVICE_AUTO_START,
            exeName=exe,
            exeArgs=_service_args(config),
            description=DESCRIPTION,
            delayedstart=True,
        )

    try:
        try:
            _install()
        except Exception as exc:  # noqa: BLE001
            if "already" not in str(exc).lower() and "уже" not in str(exc).lower():
                raise
            # Обновление: переустанавливаем службу с теми же данными.
            try:
                win32serviceutil.StopService(SERVICE_NAME)
            except Exception:  # noqa: BLE001
                pass
            try:
                win32serviceutil.RemoveService(SERVICE_NAME)
            except Exception:  # noqa: BLE001
                pass
            _install()
        # Восстановление: три перезапуска с интервалом 5 секунд.
        subprocess.run(
            [
                "sc.exe",
                "failure",
                SERVICE_NAME,
                "reset=",
                "86400",
                "actions=",
                "restart/5000/restart/5000/restart/5000",
            ],
            check=False,
            capture_output=True,
        )
        if config_ready:
            win32serviceutil.StartService(SERVICE_NAME)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            "Не удалось установить службу (нужны права администратора): " + str(exc)
        ) from exc
    if config_ready:
        print("Служба " + SERVICE_NAME + " установлена и запущена.")
    else:
        print(
            "Служба " + SERVICE_NAME + " установлена. Заполните config.ini "
            "и выполните: sc start " + SERVICE_NAME
        )
    return 0


def remove_service() -> int:
    _require_pywin32()
    try:
        win32serviceutil.StopService(SERVICE_NAME)
    except Exception:  # noqa: BLE001
        pass
    try:
        win32serviceutil.RemoveService(SERVICE_NAME)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            "Не удалось удалить службу " + SERVICE_NAME + ": " + str(exc)
        ) from exc
    print("Служба " + SERVICE_NAME + " удалена.")
    return 0
