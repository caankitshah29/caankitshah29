"""Tiny wrapper around pygame's music player with an accurate position clock."""
from __future__ import annotations

import os
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
import pygame  # noqa: E402


class AudioPlayer:
    def __init__(self):
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        self._loaded = None
        self._started_at = 0.0
        self._paused_at = None
        self._paused_total = 0.0
        self._duration_ms = 0.0
        self.volume = 1.0

    # ------------------------------------------------------------------
    def play(self, path: str, duration_ms: float):
        self.unload()
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(self.volume)
        pygame.mixer.music.play()
        self._loaded = path
        self._duration_ms = duration_ms
        self._started_at = time.perf_counter()
        self._paused_at = None
        self._paused_total = 0.0

    def pause(self):
        if self._loaded and self._paused_at is None:
            pygame.mixer.music.pause()
            self._paused_at = time.perf_counter()

    def resume(self):
        if self._loaded and self._paused_at is not None:
            pygame.mixer.music.unpause()
            self._paused_total += time.perf_counter() - self._paused_at
            self._paused_at = None

    def unload(self):
        if self._loaded:
            try:
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()  # release the file so it can be deleted
            except Exception:
                pass
        self._loaded = None
        self._paused_at = None

    stop = unload

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, v))
        try:
            pygame.mixer.music.set_volume(self.volume)
        except Exception:
            pass

    # ------------------------------------------------------------------
    @property
    def loaded(self) -> bool:
        return self._loaded is not None

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    def position_ms(self) -> float:
        if not self._loaded:
            return 0.0
        now = self._paused_at if self._paused_at is not None else time.perf_counter()
        return (now - self._started_at - self._paused_total) * 1000.0

    def finished(self) -> bool:
        if not self._loaded or self._paused_at is not None:
            return False
        if pygame.mixer.music.get_busy():
            return False
        # get_busy can be False for a moment right after play(); wait a bit.
        return self.position_ms() > 150

    def quit(self):
        self.unload()
        try:
            pygame.mixer.quit()
        except Exception:
            pass
