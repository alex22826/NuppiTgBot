"""Run the Django portal and Telegram bot in one Render web service.

Both processes receive the same environment, so BOT_DB_PATH points to the
single SQLite database on the service's persistent disk.  Keeping them in one
service is important because a Render disk cannot be attached to two services.

The portal is the critical process.  A Telegram polling failure must never
take the website down: the supervisor keeps Gunicorn running and restarts only
the bot with bounded exponential backoff.  If Gunicorn itself exits, Render is
allowed to restart the whole service in the usual way.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
PORTAL_DIR = PROJECT_DIR / "django_portal"
PYTHON = sys.executable
LOGGER = logging.getLogger("cloud_service")
BOT_RESTART_INITIAL_DELAY_SECONDS = 2.0
BOT_RESTART_MAX_DELAY_SECONDS = 60.0
BOT_STABLE_RUNTIME_SECONDS = 120.0


def is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    data_dir = Path(environment.get("RENDER_DISK_PATH", "/var/data"))

    if environment.get("RENDER"):
        environment.setdefault("BOT_DB_PATH", str(data_dir / "medical_bot.db"))
        environment.setdefault("EXCEL_REPORTS_DIR", str(data_dir / "excel_files"))
        environment.setdefault("DJANGO_DB_PATH", str(data_dir / "portal.sqlite3"))
        environment.setdefault("DJANGO_DEBUG", "0")

    return environment


def run_portal_setup(environment: dict[str, str]) -> None:
    manage = str(PORTAL_DIR / "manage.py")
    for arguments in (("migrate", "--noinput"), ("collectstatic", "--noinput")):
        subprocess.run(
            [PYTHON, manage, *arguments],
            cwd=PORTAL_DIR,
            env=environment,
            check=True,
        )

    username = environment.get("DJANGO_SUPERUSER_USERNAME", "").strip()
    password = environment.get("DJANGO_SUPERUSER_PASSWORD", "")
    if not username or not password:
        return

    bootstrap_code = (
        "import os; "
        "from django.contrib.auth import get_user_model; "
        "U=get_user_model(); name=os.environ['DJANGO_SUPERUSER_USERNAME']; "
        "password=os.environ['DJANGO_SUPERUSER_PASSWORD']; "
        "user,created=U.objects.get_or_create(username=name, "
        "defaults={'is_staff': True, 'is_superuser': True}); "
        "created and user.set_password(password); "
        "created and user.save()"
    )
    subprocess.run(
        [PYTHON, manage, "shell", "-c", bootstrap_code],
        cwd=PORTAL_DIR,
        env=environment,
        check=True,
    )


def service_commands(environment: dict[str, str]) -> list[list[str]]:
    port = environment.get("PORT", "10000")
    commands = [
        [
            PYTHON,
            "-m",
            "gunicorn",
            "--chdir",
            str(PORTAL_DIR),
            "medical_portal.wsgi:application",
            "--bind",
            f"0.0.0.0:{port}",
            "--workers",
            "1",
            "--threads",
            "4",
            "--timeout",
            "120",
        ]
    ]
    if not is_truthy(environment.get("DISABLE_TELEGRAM_BOT")):
        commands.append([PYTHON, str(PROJECT_DIR / "bot.py")])
    return commands


def positive_float_setting(
        environment: dict[str, str], name: str, default: float,
        *, minimum: float = 0.1, maximum: float = 3600.0) -> float:
    """Read a bounded positive float without making startup fragile."""
    raw_value = (environment.get(name) or "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        LOGGER.warning("%s=%r не является числом; использую %s", name, raw_value, default)
        return default
    if value < minimum or value > maximum:
        LOGGER.warning(
            "%s=%r вне допустимого диапазона %s..%s; использую %s",
            name, raw_value, minimum, maximum, default,
        )
        return default
    return value


def start_child(
        command: list[str], environment: dict[str, str], role: str,
) -> subprocess.Popen:
    """Start one supervised child process with website-friendly priority."""
    LOGGER.info("Запуск процесса %s", role)
    process_options = {}
    if role == "telegram-bot" and os.name == "posix":
        # Render runs the Telegram worker beside Gunicorn on one CPU.  A lower
        # CPU priority keeps the portal responsive during image processing.
        process_options["preexec_fn"] = lambda: os.nice(10)
    return subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        env=environment,
        **process_options,
    )


def stop_processes(processes) -> None:
    processes = [process for process in processes if process is not None]
    for process in processes:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + 20
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    environment = command_environment()
    run_portal_setup(environment)

    commands = service_commands(environment)
    portal_command = commands[0]
    bot_command = commands[1] if len(commands) > 1 else None
    portal_process: subprocess.Popen | None = None
    bot_process: subprocess.Popen | None = None
    bot_started_at: float | None = None
    next_bot_restart_at: float | None = None
    bot_restart_initial_delay = positive_float_setting(
        environment,
        "BOT_RESTART_INITIAL_DELAY_SECONDS",
        BOT_RESTART_INITIAL_DELAY_SECONDS,
    )
    bot_restart_max_delay = positive_float_setting(
        environment,
        "BOT_RESTART_MAX_DELAY_SECONDS",
        BOT_RESTART_MAX_DELAY_SECONDS,
        minimum=bot_restart_initial_delay,
    )
    bot_stable_runtime = positive_float_setting(
        environment,
        "BOT_STABLE_RUNTIME_SECONDS",
        BOT_STABLE_RUNTIME_SECONDS,
    )
    bot_restart_delay = bot_restart_initial_delay
    stopping = False

    def request_stop(signum, _frame) -> None:
        nonlocal stopping
        LOGGER.info("Получен сигнал %s, останавливаю сайт и бота", signum)
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        portal_process = start_child(portal_command, environment, "django-portal")
        if bot_command is not None:
            bot_process = start_child(bot_command, environment, "telegram-bot")
            bot_started_at = time.monotonic()

        while not stopping:
            portal_return_code = portal_process.poll()
            if portal_return_code is not None:
                LOGGER.error(
                    "Сайт завершился с кодом %s; Render перезапустит сервис",
                    portal_return_code,
                )
                return portal_return_code or 1

            now = time.monotonic()
            if bot_process is not None:
                bot_return_code = bot_process.poll()
                if bot_return_code is not None:
                    runtime = max(0.0, now - (bot_started_at or now))
                    if runtime >= bot_stable_runtime:
                        bot_restart_delay = bot_restart_initial_delay
                    LOGGER.error(
                        "Telegram-бот завершился с кодом %s после %.1f с; "
                        "сайт продолжает работать, перезапуск бота через %.1f с",
                        bot_return_code,
                        runtime,
                        bot_restart_delay,
                    )
                    bot_process = None
                    bot_started_at = None
                    next_bot_restart_at = now + bot_restart_delay
                    bot_restart_delay = min(
                        bot_restart_max_delay,
                        bot_restart_delay * 2,
                    )
            elif bot_command is not None and (
                    next_bot_restart_at is None or now >= next_bot_restart_at):
                bot_process = start_child(bot_command, environment, "telegram-bot")
                bot_started_at = now
                next_bot_restart_at = None
            time.sleep(1)
        return 0
    finally:
        stop_processes((bot_process, portal_process))


if __name__ == "__main__":
    raise SystemExit(main())
