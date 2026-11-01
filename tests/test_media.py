"""Exercise the actual encoder/WebRTC path using two local peers, not Ring."""
import asyncio
from array import array
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from handler import CallSession

class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_wav_crosses_real_webrtc_connection(self):
        remote = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        remote.addTrack(AudioStreamTrack())
        heard = asyncio.Event()
        drains = []

        @remote.on('track')
        def on_track(track):
            async def consume():
                try:
                    while True:
                        frame = await track.recv()
                        if max(array('h', bytes(frame.planes[0]))) > 100:
                            heard.set()
                except Exception:
                    pass
            drains.append(asyncio.create_task(consume()))

        class Signaling:
            def __init__(self, *args, on_message_callback, **kwargs):
                self.callback = on_message_callback
            async def generate(self, sdp):
                await remote.setRemoteDescription(RTCSessionDescription(sdp=sdp,type='offer'))
                await remote.setLocalDescription(await remote.createAnswer())
                self.callback(SimpleNamespace(error_code=None, answer=remote.localDescription.sdp, candidate=None))
            async def close(self):
                pass

        try:
            with tempfile.TemporaryDirectory() as temp:
                audio = Path(temp)/'message.wav'
                with wave.open(str(audio),'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(48000)
                    # Alternating square wave gives non-zero decoded audio.
                    output.writeframes((b'\x00\x10'*48+b'\x00\xf0'*48)*200)
                # This sandbox prohibits interface enumeration; restrict both test peers
                # to loopback instead of querying host network interfaces.
                with patch('handler.RingWebRtcStream',Signaling), patch('aioice.ice.get_host_addresses',return_value=['127.0.0.1']):
                    session = CallSession(None,42,audio)
                    try:
                        await session.answer()
                        await session.play()
                        await asyncio.wait_for(heard.wait(),3)
                    finally:
                        await session.close()
        finally:
            for task in drains: task.cancel()
            await asyncio.gather(*drains,return_exceptions=True)
            await remote.close()
