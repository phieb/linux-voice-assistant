import logging
import os
import shlex
import subprocess
import threading
import urllib.request
from typing import IO, Callable, List, Optional

from linux_voice_assistant.player.base import AudioPlayer
from linux_voice_assistant.player.state import PlayerState


class AlsaPlayer(AudioPlayer):
    """Audio player using ALSA (aplay) for lightweight headless playback."""

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

    def play(
        self,
        url: str,
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = True,
    ) -> None:
        if stop_first:
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

    def _play_thread(self, url: str) -> None:
        try:
            if url.startswith(("http://", "https://")):
                self._stream_url(url)
            else:
                # Local file
                ext = os.path.splitext(url)[1].lower()
                if ext == ".flac":
                    self._play_flac(url)
                else:
                    self._play_wav(url)

            # Check if playback finished naturally
            if not self._stop_event.is_set():
                self._invoke_done_callback()

        except Exception:
            self._log.exception("Error during playback of %s", url)
            with self._state_lock:
                self._state = PlayerState.ERROR
                self._done_callback = None
        finally:
            with self._state_lock:
                self._procs = []
                self._state = PlayerState.IDLE

    def _stream_url(self, url: str) -> None:
        """Stream audio from a URL to aplay, decoding if necessary."""
        with urllib.request.urlopen(url, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "audio/wav")
            decoder_cmd: Optional[List[str]] = None
            
            aplay_cmd = ["aplay"]
            if self._device:
                aplay_cmd.extend(["-D", self._device])

            if "flac" in content_type:
                decoder_cmd = ["flac", "-d", "-c", "--silent", "-"]
            #elif "mpeg" in content_type or "mp3" in content_type:
            else:
                rate = "22050"
                decoder_cmd = ["mpg123", "--rate",rate, "--mono", "-s", "-"]
                aplay_cmd.extend(["-r", rate, "-c", "1", "-f", "S16_LE"])


            decoder_proc = None
            if decoder_cmd:
                decoder_proc = subprocess.Popen(
                    decoder_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                aplay_stdin = decoder_proc.stdout
            else: # WAV or other direct format
                aplay_stdin = subprocess.PIPE
            
            aplay_proc = subprocess.Popen(
                aplay_cmd,
                stdin=aplay_stdin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            if decoder_proc and decoder_proc.stdout:
                decoder_proc.stdout.close()
            
            procs = [p for p in [decoder_proc, aplay_proc] if p]
            with self._state_lock:
                self._procs = procs
                self._state = PlayerState.PLAYING
            
            # Get the correct stdin to write to
            stream_stdin = (decoder_proc.stdin if decoder_proc else aplay_proc.stdin)
            if not stream_stdin:
                return # Should not happen

            # Stream from URL to decoder/player
            while not self._stop_event.is_set():
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    stream_stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    break # Player process probably died
            
            if stream_stdin:
                stream_stdin.close()
            
            # Wait for processes to finish
            for proc in procs:
                try:
                    proc.wait()
                except Exception:
                    pass


    def _play_wav(self, path: str) -> None:
        cmd = ["aplay"]
        if self._device:
            cmd.extend(["-D", self._device])
        cmd.append(path)

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with self._state_lock:
            self._procs = [proc]
            self._state = PlayerState.PLAYING

        proc.wait()

    def _play_flac(self, path: str) -> None:
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
        if flac_proc.stdout:
            flac_proc.stdout.close()

        with self._state_lock:
            self._procs = [flac_proc, aplay_proc]
            self._state = PlayerState.PLAYING

        aplay_proc.wait()
        flac_proc.wait()

    def _stop_procs(self, invoke_callback: bool = True) -> None:
        with self._state_lock:
            self._stop_event.set()
            procs = self._procs[:]
            self._procs = []
            
            if self._state == PlayerState.IDLE:
                # Nothing to do
                return

            self._state = PlayerState.IDLE
            
        for proc in reversed(procs):
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            except Exception:
                pass
        
        if invoke_callback:
            self._invoke_done_callback()

    def _invoke_done_callback(self) -> None:
        callback = None
        with self._state_lock:
            callback = self._done_callback
            self._done_callback = None
        
        if callback:
            try:
                callback()
            except Exception:
                self._log.exception("Error in done_callback")

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
            pass
