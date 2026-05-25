"""
Кроссплатформенная запись микрофона для агента.

Используем sounddevice (обёртка над PortAudio). 16kHz mono int16 — это формат,
который Whisper всё равно ресемплит у себя, так что пишем сразу в нём — экономим
трафик и место.

Каждый CHUNK_SECONDS секунд накопленный PCM упаковывается в WAV-чанк и кладётся
в очередь для отправки. Финальный (неполный) чанк добавляется при stop().
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import wave

log = logging.getLogger("agent")

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # int16
CHUNK_SECONDS = 30
CHUNK_BYTES = SAMPLE_RATE * CHUNK_SECONDS * SAMPLE_WIDTH_BYTES  # ~960 KB


class AudioRecorder:
    """
    Записывает микрофон в фоновом потоке (callback от PortAudio).
    Внешний код вызывает start(meeting_id), потом периодически drain(),
    и stop() когда встреча кончилась.
    """

    def __init__(self) -> None:
        self._stream = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._chunks: queue.Queue[tuple[int, bytes]] = queue.Queue()
        self._chunk_index = 0
        self.meeting_id: int | None = None

    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self, meeting_id: int) -> bool:
        if self._stream is not None:
            return True  # уже пишет
        try:
            import numpy as np  # noqa: F401  # нужен sounddevice
            import sounddevice as sd  # type: ignore
        except ImportError:
            log.warning("sounddevice/numpy не установлены — запись микрофона отключена")
            return False

        self.meeting_id = meeting_id
        self._chunk_index = 0
        self._buffer.clear()
        # очистим возможные старые чанки от прошлой встречи
        while not self._chunks.empty():
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                break

        def _callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("audio status: %s", status)
            # indata: float32 [-1.0..1.0]. Конвертим в int16 PCM.
            int16 = (indata * 32767).astype(np.int16)
            data = int16.tobytes()
            with self._lock:
                self._buffer.extend(data)
                while len(self._buffer) >= CHUNK_BYTES:
                    pcm = bytes(self._buffer[:CHUNK_BYTES])
                    del self._buffer[:CHUNK_BYTES]
                    self._finalize_chunk(pcm)

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                callback=_callback,
                blocksize=0,  # пусть PortAudio сам выберет
            )
            self._stream.start()
            log.info("audio: запись началась для meeting_id=%d", meeting_id)
            return True
        except Exception as e:
            log.warning("audio: не удалось открыть микрофон: %s", e)
            self._stream = None
            return False

    def stop(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as e:
            log.warning("audio: ошибка stop: %s", e)
        finally:
            self._stream = None
        # финальный чанк — то что не успело заполниться до 30 секунд
        with self._lock:
            if self._buffer:
                pcm = bytes(self._buffer)
                self._buffer.clear()
                if len(pcm) > SAMPLE_RATE * SAMPLE_WIDTH_BYTES:  # >1 секунда
                    self._finalize_chunk(pcm)
        log.info("audio: запись остановлена, meeting_id=%s", self.meeting_id)

    def _finalize_chunk(self, pcm: bytes) -> None:
        """Внутри _lock. Упаковываем PCM в WAV и кладём в очередь на отправку."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH_BYTES)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        self._chunks.put((self._chunk_index, buf.getvalue()))
        self._chunk_index += 1

    def drain(self) -> list[tuple[int, bytes]]:
        """Забирает все накопленные чанки. Возвращает [(chunk_index, wav_bytes), ...]."""
        out: list[tuple[int, bytes]] = []
        while True:
            try:
                out.append(self._chunks.get_nowait())
            except queue.Empty:
                break
        return out
