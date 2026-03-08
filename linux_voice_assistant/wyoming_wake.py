"""Wyoming protocol wake word detection client.

Implements the minimal Wyoming wire format needed to stream audio to a
wyoming-microwakeword (or compatible) service and receive detections:

  Client → Server:  {"type": "describe"}\n
  Server → Client:  {"type": "info", ...}\n
  Client → Server:  {"type": "run-detection"}\n
  Client → Server:  {"type": "audio-chunk", "data": {...}, "data_length": N}\n<N bytes PCM>
  Server → Client:  {"type": "detection", "data": {"name": "okay_nabu", ...}}\n
"""

import json
import logging
import select
import socket
import threading
import time
from queue import Empty, Queue
from typing import Callable, List, Optional

_LOGGER = logging.getLogger(__name__)

_RECONNECT_DELAY = 5.0
_AUDIO_QUEUE_MAXSIZE = 50


class WakeWordProxy:
    """Duck-typed object passed to satellite.wakeup() for Wyoming detections.

    satellite.wakeup() expects Union[MicroWakeWord, OpenWakeWord] but only
    accesses the .wake_word attribute, so this lightweight stand-in suffices.
    """

    def __init__(self, wake_word: str) -> None:
        self.wake_word = wake_word


class WyomingWakeClient:
    """Client for a Wyoming-protocol wake word detection service.

    Connects to a service like wyoming-microwakeword via TCP, streams
    16 kHz mono S16_LE PCM audio, and calls on_detection(name) when a
    wake word is detected.  Automatically reconnects on failure.
    """

    def __init__(
        self,
        host: str,
        port: int,
        wake_word_names: List[str],
        on_detection: Callable[[str], None],
    ) -> None:
        self._host = host
        self._port = port
        self._wake_word_names = wake_word_names
        self._on_detection = on_detection
        self._audio_queue: Queue[Optional[bytes]] = Queue(maxsize=_AUDIO_QUEUE_MAXSIZE)
        self._thread = threading.Thread(target=self._run, daemon=True, name="wyoming-wake")
        self._stopped = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped = True
        self._audio_queue.put(None)  # unblock the thread

    def send_audio(self, audio_chunk: bytes) -> None:
        """Queue an audio chunk for streaming.  Silently drops chunk if queue is full."""
        try:
            self._audio_queue.put_nowait(audio_chunk)
        except Exception:
            pass  # queue full — drop chunk

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stopped:
            try:
                self._connect_and_run()
            except Exception:
                _LOGGER.exception("Wyoming wake word client error")
            if not self._stopped:
                _LOGGER.info("Reconnecting to Wyoming wake word service in %.0fs", _RECONNECT_DELAY)
                time.sleep(_RECONNECT_DELAY)

    def _connect_and_run(self) -> None:
        _LOGGER.info("Connecting to Wyoming wake word service at %s:%d", self._host, self._port)
        with socket.create_connection((self._host, self._port), timeout=10.0) as sock:
            _LOGGER.info("Connected to Wyoming wake word service")

            # Wyoming handshake:
            #   1. Client → describe
            #   2. Server → info
            #   3. Client → run-detection  (no names — microwakeword uses server-side config)
            sock.settimeout(10.0)
            self._send_event(sock, {"type": "describe"})

            recv_buf = b""
            while True:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("Wyoming service closed connection during handshake")
                recv_buf += data
                newline_pos = recv_buf.find(b"\n")
                if newline_pos >= 0:
                    line = recv_buf[:newline_pos]
                    recv_buf = recv_buf[newline_pos + 1:]
                    try:
                        event = json.loads(line)
                        _LOGGER.debug("Wyoming handshake event: %s", event.get("type"))
                        # skip binary payload of info if any
                        data_length = event.get("data_length", 0)
                        if data_length > 0 and len(recv_buf) >= data_length:
                            recv_buf = recv_buf[data_length:]
                    except json.JSONDecodeError:
                        pass
                    break  # consumed info; proceed

            # Use blocking socket for sends; use select() for non-blocking reads.
            sock.setblocking(True)

            # run-detection: no names field — microwakeword activates all loaded models
            self._send_event(sock, {"type": "run-detection"})

            while not self._stopped:
                # Send up to 10 queued audio chunks per cycle to keep latency low
                for _ in range(10):
                    try:
                        chunk = self._audio_queue.get_nowait()
                    except Empty:
                        break
                    if chunk is None:
                        return  # stop() was called
                    self._send_audio_chunk(sock, chunk)

                # Non-blocking check for incoming messages (detections, errors, …)
                readable, _, _ = select.select([sock], [], [], 0.0)
                if readable:
                    data = sock.recv(4096)
                    if not data:
                        _LOGGER.warning("Wyoming service closed connection")
                        return
                    recv_buf += data
                    recv_buf = self._process_incoming(recv_buf)

    def _send_event(self, sock: socket.socket, event: dict) -> None:
        sock.sendall((json.dumps(event) + "\n").encode())

    def _send_audio_chunk(self, sock: socket.socket, chunk: bytes) -> None:
        header = (
            json.dumps({
                "type": "audio-chunk",
                "data": {"rate": 16000, "width": 2, "channels": 1},
                "data_length": len(chunk),
            })
            + "\n"
        )
        sock.sendall(header.encode() + chunk)

    def _process_incoming(self, buf: bytes) -> bytes:
        """Parse and dispatch Wyoming events from the receive buffer.

        Returns the unconsumed remainder of buf (partial message waiting for
        more data).
        """
        while True:
            newline_pos = buf.find(b"\n")
            if newline_pos < 0:
                break

            line = buf[:newline_pos]
            after_line = buf[newline_pos + 1:]

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                _LOGGER.warning("Wyoming: invalid JSON received: %s", line[:200])
                buf = after_line
                continue

            # Skip binary payload if present (e.g. audio responses)
            data_length = event.get("data_length", 0)
            if data_length > 0:
                if len(after_line) < data_length:
                    break  # wait for more data; buf unchanged
                after_line = after_line[data_length:]

            buf = after_line  # commit consumption of this full message

            event_type = event.get("type", "")
            if event_type == "detection":
                name = (event.get("data") or {}).get("name", "")
                if not name and self._wake_word_names:
                    name = self._wake_word_names[0]
                _LOGGER.info("Wyoming wake word detected: %s", name)
                self._on_detection(name)
            elif event_type == "error":
                text = (event.get("data") or {}).get("text", "unknown")
                _LOGGER.error("Wyoming service error: %s", text)
            elif event_type == "info":
                _LOGGER.debug("Wyoming service info: %s", event.get("data"))
            else:
                _LOGGER.debug("Wyoming event: %s", event_type)

        return buf
