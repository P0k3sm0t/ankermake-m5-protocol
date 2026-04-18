"""
MjpegHub — on-demand MJPEG fanout service for the printer camera.

Architecture:
  VideoQueue (existing H.264 notify stream)
      └── MjpegHub (1x ffmpeg per printer, on-demand)
              ├── /api/camera/stream client A
              └── /api/camera/stream client B

The hub subscribes to VideoQueue via tap() and pipes raw H.264 bytes into a
single ffmpeg process.  It parses JPEG frames from ffmpeg stdout and fans them
out to all registered consumer queues.

Lifecycle:
  - ffmpeg starts when the first consumer subscribes
  - ffmpeg stops when the last consumer leaves
  - If VideoQueue stops or ffmpeg crashes, all consumers receive None (sentinel)
    and the hub stops itself cleanly.
"""
import logging
import os
import queue
import subprocess
import threading
from typing import Optional

from ..lib.service import Service, ServiceRestartSignal, RunState
from .. import app

log = logging.getLogger(__name__)

_PIPE_WRITE_TIMEOUT = 2.0  # seconds to wait when writing H.264 data to ffmpeg stdin
_FRAME_QUEUE_MAX = 4       # per-consumer queue depth (drop oldest on overflow)


class MjpegHub(Service):
    """Single-ffmpeg MJPEG hub that fans video frames to multiple HTTP consumers."""

    def __init__(self, printer_index: int = 0):
        self.printer_index = 0 if printer_index is None else int(printer_index)
        self._lock = threading.Lock()
        self._consumers: list[queue.Queue] = []
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._ffmpeg_path: Optional[str] = None
        super().__init__()

    @property
    def name(self) -> str:
        return f"MjpegHub[{self.printer_index}]"

    @property
    def consumer_count(self) -> int:
        with self._lock:
            return len(self._consumers)

    def subscribe(self) -> queue.Queue:
        """Register a new consumer.  Returns a Queue that receives JPEG bytes.

        None is pushed as a sentinel when the stream ends or ffmpeg dies.
        Starts ffmpeg on first subscription.
        """
        q: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_MAX)
        with self._lock:
            self._consumers.append(q)
            first = len(self._consumers) == 1

        log.info("%s: consumer subscribed (total=%d)", self.name, self.consumer_count)

        if first:
            self._start_ffmpeg()

        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        """Deregister a consumer.  Stops ffmpeg when no consumers remain."""
        with self._lock:
            try:
                self._consumers.remove(q)
            except ValueError:
                pass
            remaining = len(self._consumers)

        log.info("%s: consumer unsubscribed (remaining=%d)", self.name, remaining)

        if remaining == 0:
            self._stop_ffmpeg()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_ffmpeg_path(self) -> Optional[str]:
        """Resolve ffmpeg binary path the same way the rest of the app does."""
        import shutil
        return shutil.which("ffmpeg")

    def _build_video_url(self) -> str:
        """Build the internal /video URL for this printer index."""
        host = "127.0.0.1"
        port = os.getenv("FLASK_PORT", "4470")
        url = f"http://{host}:{port}/video?for_timelapse=1&printer_index={self.printer_index}"
        # Include API key if the endpoint requires auth
        api_key = app.config.get("api_key") if app else None
        if api_key:
            url += f"&apikey={api_key}"
        return url

    def _start_ffmpeg(self) -> None:
        """Spawn the ffmpeg subprocess and the reader thread."""
        ffmpeg = self._get_ffmpeg_path()
        if not ffmpeg:
            log.error("%s: ffmpeg not found — cannot start MJPEG hub", self.name)
            self._send_sentinel()
            return

        video_url = self._build_video_url()
        cmd = [
            ffmpeg, "-loglevel", "error", "-nostdin",
            "-f", "h264", "-i", video_url,
            "-an", "-sn", "-dn",
            "-r", "5",
            "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "pipe:1",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.error("%s: failed to start ffmpeg: %s", self.name, exc)
            self._send_sentinel()
            return

        self._proc = proc
        t = threading.Thread(
            target=self._reader_loop,
            args=(proc,),
            daemon=True,
            name=f"mjpeghub-reader-{self.printer_index}",
        )
        self._reader_thread = t
        t.start()
        log.info("%s: ffmpeg started (pid=%d)", self.name, proc.pid)

    def _stop_ffmpeg(self) -> None:
        """Terminate the ffmpeg subprocess."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except OSError:
            pass
        log.info("%s: ffmpeg stopped", self.name)

    def _send_sentinel(self) -> None:
        """Push None sentinel to all consumers to signal end-of-stream."""
        with self._lock:
            consumers = list(self._consumers)
        for q in consumers:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass

    def _fan_out(self, frame: bytes) -> None:
        """Deliver a JPEG frame to all registered consumers, dropping oldest on overflow."""
        with self._lock:
            consumers = list(self._consumers)
        for q in consumers:
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                except queue.Empty:
                    pass
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        """Background thread: parse JPEG frames from ffmpeg stdout and fan out."""
        # Import here to avoid circular import at module level
        from ..camera import iter_mjpeg_frames

        log.info("%s: reader thread started", self.name)
        try:
            for frame in iter_mjpeg_frames(proc):
                self._fan_out(frame)
                # Stop if proc was replaced (new subscription cycle started fresh proc)
                if self._proc is not proc:
                    break
        except Exception as exc:
            log.warning("%s: reader loop error: %s", self.name, exc)
        finally:
            log.info("%s: reader thread exiting", self.name)
            self._send_sentinel()

    # ------------------------------------------------------------------
    # Service lifecycle — this service is mostly passive (no worker_run loop
    # driving real work).  The actual work happens in _reader_loop threads
    # and subscribe/unsubscribe calls from Flask request threads.
    # ------------------------------------------------------------------

    def worker_init(self) -> None:
        log.info("%s: worker_init", self.name)

    def worker_start(self) -> None:
        log.info("%s: worker_start", self.name)

    def worker_run(self, timeout: float) -> None:
        # Nothing to poll — just idle.  The reader thread and consumer
        # threads drive all real activity.
        self.idle(timeout=timeout)

    def worker_stop(self) -> None:
        log.info("%s: worker_stop — terminating ffmpeg and clearing consumers", self.name)
        self._send_sentinel()
        self._stop_ffmpeg()
        with self._lock:
            self._consumers.clear()
