import logging
import os
import subprocess
import tempfile
import threading
import urllib.request
from typing import Callable, List, Optional

from linux_voice_assistant.player.base import AudioPlayer
from linux_voice_assistant.player.state import PlayerState


class AlsaPlayer(AudioPlayer):
    """Audio player using ALSA (aplay) for lightweight headless playback.

    Replaces the libmpv-based player with subprocess calls to aplay,
    avoiding the heavy mpv/libmpv dependency on headless devices like the Pi Zero 2W.

    Supported formats:
    - WAV: played directly via aplay
    - FLAC: decoded via `flac -d -c` piped to aplay (requires the flac package)
    - HTTP/HTTPS URLs: downloaded to a temp file, then played by detected format

    Volume control is applied via amixer (best-effort; silently skipped if unavailable).
    Pause/resume use SIGSTOP/SIGCONT on the aplay process.
    """

    def __init__(self, device: Optional[str] = None) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._device = device
        self._state: PlayerState = PlayerState.IDLE
        self._state_lock = threading.Lock()
        self._procs: List[subprocess.Popen] = []
        self._done_callback: Optional[Callable[[], None]] = None
        self._stop_event = threading.Event()
        self._user_volume: float = 100.0
        self._duck_factor: float = 1.0

    # -------- Playback control --------

    def play(
        self,
        url: str,
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = True,
    ) -> None:
        self._stop_procs(invoke_callback=False)
        with self._state_lock:
            self._done_callback = done_callback
            self._state = PlayerState.LOADING
            self._stop_event.clear()
        threading.Thread(target=self._play_thread, args=(url,), daemon=True).start()

    def pause(self) -> None:
        import signal

        with self._state_lock:
            if self._state != PlayerState.PLAYING:
                return
            procs = self._procs[:]
            self._state = PlayerState.PAUSED
        for proc in procs:
            try:
                os.kill(proc.pid, signal.SIGSTOP)
            except ProcessLookupError:
                pass

    def resume(self) -> None:
        import signal

        with self._state_lock:
            if self._state != PlayerState.PAUSED:
                return
            procs = self._procs[:]
            self._state = PlayerState.PLAYING
        for proc in procs:
            try:
                os.kill(proc.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def stop(self, for_replacement: bool = False) -> None:
        self._stop_procs(invoke_callback=not for_replacement)

    def state(self) -> PlayerState:
        with self._state_lock:
            return self._state

    # -------- Volume / Ducking --------

    def set_volume(self, volume: float) -> None:
        with self._state_lock:
            self._user_volume = max(0.0, min(100.0, float(volume)))
        self._apply_volume()

    def duck(self, factor: float = 0.5) -> None:
        with self._state_lock:
            self._duck_factor = max(0.0, min(1.0, float(factor)))
        self._apply_volume()

    def unduck(self) -> None:
        with self._state_lock:
            self._duck_factor = 1.0
        self._apply_volume()

    # -------- Internal helpers --------

    def _play_thread(self, url: str) -> None:
        tmp_path: Optional[str] = None
        try:
            # Download HTTP(S) URLs to a temp file before playing
            if url.startswith(("http://", "https://")):
                with self._state_lock:
                    self._state = PlayerState.LOADING
                with urllib.request.urlopen(url, timeout=10) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    suffix = ".flac" if "flac" in content_type else ".wav"
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp.write(resp.read())
                        tmp_path = tmp.name
                play_path = tmp_path
            else:
                play_path = url

            if self._stop_event.is_set():
                return

            ext = os.path.splitext(play_path)[1].lower()
            completed = self._play_flac(play_path) if ext == ".flac" else self._play_wav(play_path)

            if completed:
                callback = None
                with self._state_lock:
                    self._state = PlayerState.IDLE
                    callback = self._done_callback
                    self._done_callback = None
                if callback:
                    try:
                        callback()
                    except Exception:
                        self._log.exception("Error in done_callback")

        except Exception:
            self._log.exception("Error during playback of %s", url)
            with self._state_lock:
                self._state = PlayerState.ERROR
                self._done_callback = None
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            with self._state_lock:
                self._procs = []

    def _play_wav(self, path: str) -> bool:
        """Play a WAV (or any aplay-compatible) file. Returns True on natural completion."""
        cmd = ["aplay"]
        if self._device:
            cmd.extend(["-D", self._device])
        cmd.append(path)

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self._state_lock:
            self._procs = [proc]
            self._state = PlayerState.PLAYING

        proc.wait()
        with self._state_lock:
            self._procs = []
        return not self._stop_event.is_set() and proc.returncode == 0

    def _play_flac(self, path: str) -> bool:
        """Decode FLAC via flac and pipe to aplay. Returns True on natural completion.

        Requires the `flac` package to be installed (apt install flac).
        """
        flac_proc = subprocess.Popen(
            ["flac", "-d", "-c", "--silent", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        aplay_cmd = ["aplay"]
        if self._device:
            aplay_cmd.extend(["-D", self._device])
        aplay_proc = subprocess.Popen(
            aplay_cmd,
            stdin=flac_proc.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        flac_proc.stdout.close()  # Allow flac_proc to receive SIGPIPE if aplay exits early

        with self._state_lock:
            self._procs = [flac_proc, aplay_proc]
            self._state = PlayerState.PLAYING

        aplay_proc.wait()
        flac_proc.wait()
        with self._state_lock:
            self._procs = []
        return not self._stop_event.is_set() and aplay_proc.returncode == 0

    def _stop_procs(self, invoke_callback: bool = True) -> None:
        callback = None
        procs = []
        with self._state_lock:
            self._stop_event.set()
            procs = self._procs[:]
            self._procs = []
            if invoke_callback:
                callback = self._done_callback
            self._done_callback = None
            self._state = PlayerState.IDLE

        for proc in reversed(procs):  # terminate aplay before flac
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass

        if callback:
            try:
                callback()
            except Exception:
                self._log.exception("Error in stop callback")

    def _apply_volume(self) -> None:
        with self._state_lock:
            effective = int(self._user_volume * self._duck_factor)
        try:
            subprocess.run(
                ["amixer", "-q", "sset", "Master", f"{effective}%"],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            pass  # amixer not available on this system
