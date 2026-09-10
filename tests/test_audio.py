import unittest

from lib.audio import AudioProtocolError, AudioSession


class Budget:
    def __init__(self, capacity):
        self.capacity = capacity

    def take(self, amount=1):
        if amount > self.capacity:
            return False
        self.capacity -= amount
        return True


class Decoder:
    def __init__(self):
        self.closed = False
        self.packets = []

    def decode(self, packet):
        self.packets.append(packet)
        return b"\x00\x00" * 960, 960

    def close(self):
        self.closed = True


class AudioSessionTests(unittest.TestCase):
    def setUp(self):
        self.decoders = []

        def make_decoder():
            decoder = Decoder()
            self.decoders.append(decoder)
            return decoder

        self.session = AudioSession(48000, make_decoder, lambda: True)

    def test_negotiation_preserves_wire_shape_and_restarts_sink(self):
        response, restart = self.session.negotiate({
            "audio_v": 3, "audio_codec": "opus", "framing": "bare", "id": 7,
        })
        self.assertTrue(restart)
        self.assertEqual(response, {
            "type": "audio-capabilities", "v": 3, "id": 7,
            "codec": "opus", "framing": "bare", "rate": 48000,
            "channels": 1, "packet_samples": 960,
        })

    def test_bare_opus_frames_use_implicit_stream_and_sequence(self):
        self.session.negotiate({"audio_v": 3, "audio_codec": "opus", "framing": "bare"})
        self.assertEqual(self.session.start_stream({"stream": 12, "framing": "bare", "rate": 48000})["type"], "audio-ready")
        pcm, samples = self.session.decode(b"packet", Budget(1920))
        self.assertEqual((len(pcm), samples), (1920, 960))
        self.assertEqual(self.session.end_stream({"stream": 12, "lastSeq": 0, "packetCount": 1})["type"], "audio-ended")
        self.assertTrue(self.decoders[0].closed)

    def test_header_opus_frames_reject_wrong_sequence(self):
        self.session.negotiate({"audio_v": 2, "audio_codec": "opus", "framing": "header"})
        self.session.start_stream({"stream": 9, "framing": "header", "rate": 48000})
        frame = b"PM\x02\x01" + (9).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"packet"
        with self.assertRaisesRegex(AudioProtocolError, "Unexpected Opus frame"):
            self.session.decode(frame, Budget(1920))

    def test_pcm_frames_charge_bytes_and_reject_odd_lengths(self):
        response, _ = self.session.negotiate({"audio_codec": "pcm", "rate": 16000})
        self.assertEqual(response["rate"], 16000)
        budget = Budget(4)
        self.assertEqual(self.session.decode(b"\x00\x00\x01\x00", budget)[1], 2)
        with self.assertRaisesRegex(AudioProtocolError, "Audio rate limit exceeded"):
            self.session.decode(b"\x00", budget)

    def test_invalid_completion_does_not_close_active_decoder(self):
        self.session.negotiate({"audio_codec": "opus", "framing": "header"})
        self.session.start_stream({"stream": 1, "framing": "header", "rate": 48000})
        response = self.session.end_stream({"stream": 1, "lastSeq": 0, "packetCount": 1})
        self.assertEqual(response["error"], "Invalid Opus stream completion")
        self.assertFalse(self.decoders[0].closed)
