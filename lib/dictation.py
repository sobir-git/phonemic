"""Local socket bridge for laptop dictation."""

import asyncio
import json
import os


class Dictation:
    """One local control connection owns one phone recording."""

    def __init__(self, runtime=None):
        self.runtime = runtime
        self.reader = self.writer = None

    def _value(self, name, default):
        return getattr(self.runtime, name, default) if self.runtime else default

    async def receive(self, expected):
        while True:
            line = await self.reader.readline()
            if not line:
                raise RuntimeError("Laptop dictation disconnected")
            message = json.loads(line)
            if message.get("type") == "error":
                raise RuntimeError(message.get("message", "Laptop dictation failed"))
            if message.get("type") == expected:
                return message

    async def command(self, command):
        self.writer.write((json.dumps(command) + "\n").encode())
        await self.writer.drain()

    async def start(self):
        if self.writer:
            raise RuntimeError("Dictation is already recording")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/speech-to-text-{os.getuid()}")
        path = os.environ.get("STT_SOCKET_PATH", f"{runtime_dir}/speech-to-text/daemon.sock")
        try:
            async with asyncio.timeout(5):
                self.reader, self.writer = await asyncio.open_unix_connection(path)
                await self.receive("state")
                await self.command({"cmd": "start_recording", "pipewire_node": self._value(
                    "SRC", os.environ.get("PM_SRC", os.environ.get("PM_SINK", "phonemic2") + "_src"))})
                await self.receive("recording_started")
                await self.receive("audio_level")
        except Exception:
            await self.close()
            raise

    async def finish(self, abort=False):
        if not self.writer:
            raise RuntimeError("No mobile dictation is recording")
        try:
            async with asyncio.timeout(3):
                if not abort:
                    latency = self._value("LATENCY", int(os.environ.get("PM_LATENCY_MS", "20")))
                    await asyncio.sleep(max(0.15, latency / 1000 * 2))
                await self.command({"cmd": "abort_recording" if abort else "stop_recording"})
                await self.receive("recording_stopped")
        finally:
            await self.close()

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
        self.reader = self.writer = None
