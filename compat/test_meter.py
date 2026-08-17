import json
import os
import tempfile
import unittest

from meter import TurnMeter


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def sse(*events):
    return "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode("utf-8")


def read_snapshot(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class TurnMeterTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "meter.json")

    def test_counts_deltas_and_tracks_phase(self):
        meter = TurnMeter(path=self.path, clock=self.clock)
        meter.feed(sse({"type": "message_start", "message": {"usage": {"input_tokens": 14, "output_tokens": 0}}}))
        meter.feed(sse({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}))
        self.clock.advance(2.0)
        for word in ["用户", "要求", "我", "："]:
            meter.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": word}}))
            self.clock.advance(0.06)

        snapshot = read_snapshot(self.path)
        self.assertEqual(snapshot["phase"], "thinking")
        self.assertEqual(snapshot["output_tokens"], 4)
        self.assertEqual(snapshot["input_tokens"], 14)
        self.assertEqual(snapshot["t_first_token"], 1002.0)
        self.assertEqual(snapshot["t_update"], 1002.18)

    def test_finish_reconciles_with_final_usage(self):
        meter = TurnMeter(path=self.path, clock=self.clock)
        meter.feed(sse({"type": "message_start", "message": {"usage": {"input_tokens": 14}}}))
        meter.feed(sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
        self.clock.advance(1.0)
        meter.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "好"}}))
        meter.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "的"}}))
        self.clock.advance(4.0)
        meter.feed(sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"input_tokens": 20, "output_tokens": 80}}))
        meter.finish()

        snapshot = read_snapshot(self.path)
        self.assertEqual(snapshot["phase"], "done")
        self.assertEqual(snapshot["output_tokens"], 80)
        self.assertEqual(snapshot["input_tokens"], 20)
        last = snapshot["last_completed"]
        self.assertEqual(last["output_tokens"], 80)
        self.assertEqual(last["duration_s"], 5.0)
        self.assertEqual(last["generation_s"], 4.0)
        self.assertEqual(last["tok_s"], 20.0)

    def test_feeds_split_across_chunk_boundaries(self):
        meter = TurnMeter(path=self.path, clock=self.clock)
        raw = sse(
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "A"}},
        )
        cut = len(raw) // 2
        meter.feed(raw[:cut])
        meter.feed(raw[cut:])
        self.assertEqual(read_snapshot(self.path)["output_tokens"], 1)

    def test_ignores_non_sse_and_malformed_lines(self):
        meter = TurnMeter(path=self.path, clock=self.clock)
        meter.feed(b"event: ping\ndata: not json\ndata: [1, 2]\n\n")
        snapshot = read_snapshot(self.path)
        self.assertEqual(snapshot["output_tokens"], 0)
        self.assertEqual(snapshot["phase"], "prefill")

    def test_disabled_when_path_empty(self):
        meter = TurnMeter(path="", clock=self.clock)
        meter.feed(sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "A"}}))
        meter.finish()
        self.assertFalse(meter.enabled)

    def test_finish_without_tokens_skips_last_completed(self):
        meter = TurnMeter(path=self.path, clock=self.clock)
        meter.feed(sse({"type": "message_start", "message": {"usage": {"input_tokens": 5}}}))
        meter.finish()
        snapshot = read_snapshot(self.path)
        self.assertEqual(snapshot["phase"], "done")
        self.assertNotIn("last_completed", snapshot)


if __name__ == "__main__":
    unittest.main()
