"""Connection-local audio negotiation and Opus/PCM frame handling."""

import ctypes
import ctypes.util


_OPUS = None


def opus_library():
    """Load libopus lazily so PCM-only installations keep working."""
    global _OPUS
    if _OPUS is False:
        return None
    if _OPUS is not None:
        return _OPUS
    try:
        name = ctypes.util.find_library("opus") or "libopus.so.0"
        lib = ctypes.CDLL(name)
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                             ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decoder_destroy.restype = None
        lib.opus_packet_get_nb_samples.argtypes = [ctypes.c_void_p, ctypes.c_int32,
                                                   ctypes.c_int32]
        lib.opus_packet_get_nb_samples.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_decode.restype = ctypes.c_int
        _OPUS = lib
    except Exception:
        _OPUS = False
    return _OPUS if _OPUS is not False else None


class OpusDecoder:
    """Small raw-Opus decoder wrapper for one mono 48 kHz stream."""

    def __init__(self):
        lib = opus_library()
        if lib is None:
            raise RuntimeError("Opus is unavailable on this laptop")
        error = ctypes.c_int(0)
        self.lib = lib
        self.ptr = lib.opus_decoder_create(48000, 1, ctypes.byref(error))
        if not self.ptr or error.value < 0:
            raise RuntimeError("Could not create the Opus decoder")

    def decode(self, packet):
        if not packet or len(packet) > 1275:
            raise RuntimeError("Invalid Opus packet")
        raw = ctypes.create_string_buffer(packet)
        frames = self.lib.opus_packet_get_nb_samples(raw, len(packet), 48000)
        if frames != 960:
            raise RuntimeError("Opus packet is not 20 ms")
        pcm = (ctypes.c_int16 * 960)()
        decoded = self.lib.opus_decode(self.ptr, raw, len(packet), pcm, 960, 0)
        if decoded != 960:
            raise RuntimeError("Opus decoder rejected the packet")
        return bytes(pcm), decoded

    def close(self):
        if self.ptr:
            self.lib.opus_decoder_destroy(self.ptr)
            self.ptr = None


class AudioProtocolError(RuntimeError):
    """A client frame violated the audio protocol."""


class AudioSession:
    """Own codec, framing, stream, and sequence state for one browser socket."""

    def __init__(self, rate, decoder_factory, opus_available):
        self.rate = rate
        self.codec = "pcm"
        self.framing = "pcm"
        self.version = 2
        self.decoder = None
        self.active_stream = None
        self.expected_seq = 0
        self._decoder_factory = decoder_factory
        self._opus_available = opus_available

    def _capabilities(self, request_id, **extra):
        return {
            "type": "audio-capabilities",
            "v": self.version,
            "id": request_id,
            "codec": self.codec,
            "framing": self.framing,
            **extra,
        }

    def negotiate(self, config):
        """Select a codec and return ``(reply, sink_rate_changed)``."""
        request_id = config.get("id")
        requested = config.get("audio_codec", config.get("codec", "pcm"))
        if requested not in ("opus", "pcm"):
            return self._capabilities(
                request_id, v=2, codec="pcm", framing="pcm",
                error="Unsupported audio codec",
            ), False
        if self.active_stream is not None:
            return self._capabilities(
                request_id, error="Audio stream is active",
            ), False

        requested_version = config.get("audio_v", 2)
        requested_framing = config.get(
            "framing", "bare" if requested_version == 3 else "header"
        )
        if requested_framing not in ("bare", "header", "pcm"):
            return self._capabilities(
                request_id, v=2, codec="pcm", framing="pcm",
                error="Unsupported audio framing",
            ), False

        selected_codec = "opus" if requested == "opus" and self._opus_available() else "pcm"
        requested_rate = config.get("rate", self.rate)
        if selected_codec == "opus" and requested_framing in ("bare", "header"):
            selected_rate = 48000
            selected_framing = "bare" if requested_version == 3 and requested_framing == "bare" else "header"
            selected_version = 3 if selected_framing == "bare" else 2
        elif type(requested_rate) is int and 8000 <= requested_rate <= 48000:
            selected_codec = "pcm"
            selected_rate = requested_rate
            selected_framing = "pcm"
            selected_version = 2
        else:
            return self._capabilities(
                request_id, v=2, codec="pcm", framing="pcm",
                error="Invalid sample rate",
            ), False

        # The original receiver replaces the sink for every accepted
        # negotiation, even when the selected rate did not change.
        restart_sink = True
        self.close_decoder()
        self.codec = selected_codec
        self.rate = selected_rate
        self.framing = selected_framing
        self.version = selected_version
        return self._capabilities(
            request_id,
            rate=self.rate,
            channels=1,
            packet_samples=960 if self.codec == "opus" else 0,
        ), restart_sink

    def start_stream(self, start):
        stream = start.get("stream") if isinstance(start, dict) else None
        framing = start.get("framing") if isinstance(start, dict) else None
        rate = start.get("rate") if isinstance(start, dict) else None
        if (
            self.codec != "opus"
            or self.active_stream is not None
            or type(stream) is not int
            or not 0 < stream <= 2**32 - 1
            or framing != self.framing
            or rate != self.rate
        ):
            return {
                "type": "audio-ready", "v": self.version, "stream": stream,
                "error": "Opus stream unavailable",
            }
        try:
            decoder = self._decoder_factory()
        except RuntimeError as error:
            return {
                "type": "audio-ready", "v": self.version, "stream": stream,
                "error": str(error),
            }
        self.decoder = decoder
        self.active_stream = stream
        self.expected_seq = 0
        return {
            "type": "audio-ready", "v": self.version, "stream": stream,
            "codec": "opus", "framing": self.framing,
        }

    def end_stream(self, end):
        stream = end.get("stream") if isinstance(end, dict) else None
        last_seq = end.get("lastSeq") if isinstance(end, dict) else None
        packet_count = end.get("packetCount") if isinstance(end, dict) else None
        if (
            self.codec != "opus"
            or stream != self.active_stream
            or type(stream) is not int
            or type(last_seq) is not int
            or type(packet_count) is not int
            or last_seq != self.expected_seq - 1
            or packet_count != self.expected_seq
        ):
            return {
                "type": "audio-ended", "v": self.version, "stream": stream,
                "error": "Invalid Opus stream completion",
            }
        self.close_decoder()
        self.active_stream = None
        return {"type": "audio-ended", "v": self.version, "stream": stream}

    def decode(self, message, budget):
        """Decode one binary frame, charging the supplied PCM byte budget."""
        if self.codec == "opus":
            stream, sequence, payload = self._opus_frame(message)
            if self.decoder is None or stream != self.active_stream or sequence != self.expected_seq:
                raise AudioProtocolError("Unexpected Opus frame")
            try:
                pcm, samples = self.decoder.decode(payload)
            except RuntimeError as error:
                raise AudioProtocolError(str(error)) from error
            if not budget.take(len(pcm)):
                raise AudioProtocolError("Audio rate limit exceeded")
            self.expected_seq += 1
            return pcm, samples

        if len(message) % 2 or not budget.take(len(message)):
            raise AudioProtocolError("Audio rate limit exceeded")
        return message, len(message) // 2

    def _opus_frame(self, message):
        if self.framing == "bare":
            if not 1 <= len(message) <= 1275:
                raise AudioProtocolError("Invalid Opus frame size")
            return self.active_stream, self.expected_seq, message
        if len(message) < 13 or len(message) > 12 + 1275 or message[:3] != b"PM\x02" or message[3] != 1:
            raise AudioProtocolError("Invalid Opus frame")
        stream = int.from_bytes(message[4:8], "big")
        sequence = int.from_bytes(message[8:12], "big")
        return stream, sequence, message[12:]

    def close_decoder(self):
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None

    def close(self):
        self.close_decoder()
