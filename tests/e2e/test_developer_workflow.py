from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request, urlopen

from websockets.sync.client import connect

from voxink_dev.supervisor import HelperConfig, HelperSupervisor


class DeveloperWorkflowTests(unittest.TestCase):
    def test_supervised_microphone_protocol_saves_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = HelperSupervisor(
                HelperConfig(
                    model_path=root / "fake-model",
                    output_root=root / "output",
                    readiness_timeout=10,
                    service_module="tests.fixtures.fake_transcription_helper",
                )
            )
            supervisor.start()
            try:
                with urlopen(f"{supervisor.base_url}/ui/", timeout=2) as response:
                    self.assertIn(b"VoxInk", response.read())
                request = Request(
                    f"{supervisor.base_url}/v1/ui/bootstrap",
                    method="POST",
                    headers={"X-VoxInk-Bootstrap": supervisor.ui_bootstrap_secret},
                )
                with urlopen(request, timeout=2) as response:
                    browser_token = json.load(response)["token"]
                self.assertEqual(supervisor.auth_secret, browser_token)

                session_id = "supervised-microphone"
                url = (
                    f"ws://127.0.0.1:{supervisor.port}/v1/sessions/"
                    f"{session_id}/stream"
                )
                with connect(
                    url,
                    additional_headers={
                        "Authorization": f"Bearer {browser_token}"
                    },
                ) as websocket:
                    websocket.send(
                        json.dumps(
                            {
                                "type": "stream.start",
                                "schema_version": 1,
                                "session_id": session_id,
                                "stream_id": "microphone-1",
                                "source": "microphone",
                                "audio": {
                                    "encoding": "pcm_s16le",
                                    "sample_rate_hz": 16000,
                                    "channels": 1,
                                },
                            }
                        )
                    )
                    self.assertEqual("stream.ready", json.loads(websocket.recv())["type"])
                    pcm = bytes(3200)
                    websocket.send(
                        json.dumps(
                            {
                                "type": "audio.chunk",
                                "sequence": 0,
                                "stream_id": "microphone-1",
                                "start_ms": 0,
                                "end_ms": 100,
                                "byte_length": len(pcm),
                                "finalization_reason": "silence",
                            }
                        )
                    )
                    websocket.send(pcm)
                    event = json.loads(websocket.recv())
                    self.assertEqual("transcript.final", event["type"])
                    websocket.send(
                        json.dumps(
                            {
                                "type": "stream.stop",
                                "stream_id": "microphone-1",
                                "at_ms": 100,
                            }
                        )
                    )
                    self.assertEqual("stream.stopped", json.loads(websocket.recv())["type"])
            finally:
                supervisor.stop()

            transcript = next(
                (root / "output" / "supervised-microphone").glob("transcript-part-*.md")
            )
            content = transcript.read_text()
            self.assertIn("[MIC]", content)
            self.assertIn("Supervised microphone transcription works.", content)


if __name__ == "__main__":
    unittest.main()
