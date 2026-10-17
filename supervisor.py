"""
ProjectGabriel Supervisor
Runs main.py with auto-restart on crash.
Control panel (with music management) is started by main.py.
Uses the .venv virtual environment.
"""
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Get the project root directory
PROJECT_ROOT = Path(__file__).parent.absolute()
VENV_PYTHON = Path(sys.executable)

# ANSI codes
_RST = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_CYAN = "\033[96m"
_WHITE = "\033[97m"
_YELLOW = "\033[93m"
_RED = "\033[91m"


def _enable_ansi():
    """Enable ANSI escape code processing on Windows."""
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def _sup(msg, color=_DIM):
    """Print a supervisor message."""
    print(f"  {color}>> {msg}{_RST}")


class ProcessSupervisor:
    def __init__(self):
        self.processes = {}
        self.running = True
        self._stop_event = threading.Event()

    def start_process(self, name: str, script: str, restart_on_exit: bool = True):
        """Start a process and optionally auto-restart it."""
        def run():
            restart_delay = 1
            while self.running and not self._stop_event.is_set():
                _sup(f"Starting {name}...")
                process = None
                started_at = time.monotonic()

                try:
                    env = os.environ.copy()
                    env["PYTHONIOENCODING"] = "utf-8"
                    process = subprocess.Popen(
                        [str(VENV_PYTHON), script],
                        cwd=str(PROJECT_ROOT),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=env,
                    )
                    self.processes[name] = process

                    if process.stdout is not None:
                        for line in process.stdout:
                            sys.stdout.write(line)
                            sys.stdout.flush()

                    process.wait()
                    exit_code = process.returncode

                except Exception as exc:
                    _sup(f"{name} error: {exc}", _RED)
                    exit_code = -1

                finally:
                    if process is not None and self.processes.get(name) is process:
                        self.processes.pop(name, None)

                if not self.running or self._stop_event.is_set():
                    break

                if restart_on_exit:
                    if time.monotonic() - started_at >= 60:
                        restart_delay = 1
                    _sup(
                        f"{name} exited (code {exit_code}), restarting in {restart_delay}s...",
                        _YELLOW,
                    )
                    if self._stop_event.wait(restart_delay):
                        break
                    restart_delay = min(restart_delay * 2, 60)
                else:
                    _sup(f"{name} exited (code {exit_code})")
                    break

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        return thread

    def stop_all(self):
        """Stop all managed processes."""
        self.running = False
        self._stop_event.set()
        for name, process in list(self.processes.items()):
            if process and process.poll() is None:
                _sup(f"Stopping {name}...")
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def run(self):
        """Main supervisor loop."""
        _enable_ansi()

        import yaml
        app_name = "KYROSHIX-Remastered"
        try:
            with open(PROJECT_ROOT / "config.yml", encoding="utf-8") as config_file:
                config = yaml.safe_load(config_file)
            if isinstance(config, dict):
                app_name = config.get("app_name", app_name)
        except Exception:
            pass

        banner_width = 49
        spaced_name = " ".join(app_name.upper())
        title = f"P R O J E C T   {spaced_name}"
        subtitle = "Real-time VRChat AI"
        top_border = "\u2554" + "\u2550" * banner_width + "\u2557"
        bottom_border = "\u255a" + "\u2550" * banner_width + "\u255d"
        print()
        print(f"  {_CYAN}{top_border}{_RST}")
        print(f"  {_CYAN}\u2551{_RST}{_WHITE}{_BOLD}{title:^{banner_width}}{_RST}{_CYAN}\u2551{_RST}")
        print(f"  {_CYAN}\u2551{_RST}{_DIM}{subtitle:^{banner_width}}{_RST}{_CYAN}\u2551{_RST}")
        print(f"  {_CYAN}{bottom_border}{_RST}")
        print()

        # KYROSHIX BOT: request the configured VRChat world/instance before
        # starting the AI. This launcher stores no VRChat credentials.
        try:
            _sup("Opening configured VRChat instance...", _CYAN)
            autojoin = subprocess.run(
                [str(VENV_PYTHON), "vrchat_autojoin.py"],
                cwd=str(PROJECT_ROOT),
                check=False,
            )
            if autojoin.returncode != 0:
                _sup("VRChat autojoin returned an error; starting AI anyway.", _YELLOW)
        except Exception as exc:
            _sup(f"VRChat autojoin error: {exc}; starting AI anyway.", _YELLOW)

        # Start main application (control panel is started within main.py)
        self.start_process("main", "main.py", restart_on_exit=True)

        # Wait for Ctrl+C
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print()
            _sup("Shutting down...")
            self.stop_all()


def main():
    supervisor = ProcessSupervisor()

    # Handle termination signals
    def signal_handler(signal_number, current_frame):
        print()
        _sup("Received termination signal")
        supervisor.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    supervisor.run()


if __name__ == "__main__":
    main()
