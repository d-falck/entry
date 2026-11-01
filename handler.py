"""Experimental standalone Ring Intercom Video call handler."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
from pathlib import Path
import time
import wave

from aiortc import AudioStreamTrack, RTCBundlePolicy, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from av import AudioFrame
from ring_doorbell import Auth, Requires2FAError, Ring
from ring_doorbell.webrtcstream import RingWebRtcStream


class MessageTrack(AudioStreamTrack):
    """Send paced silence until connected, then a mono PCM WAV, then silence."""
    def __init__(self, path: Path | None):
        super().__init__()
        self.rate = 48000
        self.samples = 960
        self.data = b''
        if path:
            with wave.open(str(path), 'rb') as wav:
                if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, self.rate, 'NONE'):
                    raise ValueError('Message must be mono 48 kHz 16-bit PCM WAV')
                if not 0 < wav.getnframes() <= self.rate * 30:
                    raise ValueError('Message must be between 0 and 30 seconds')
                self.data = wav.readframes(wav.getnframes())
        self.ready = asyncio.Event()
        self.done = asyncio.Event()
        self.offset = 0
        self.pts = 0
        self.started = None

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        await asyncio.sleep(max(0, self.started + self.pts / self.rate - time.monotonic()))
        size = self.samples * 2
        chunk = b''
        if self.ready.is_set():
            chunk = self.data[self.offset:self.offset + size]
            self.offset += len(chunk)
            # Set completion on the following frame so the last data frame is sent first.
            if not chunk:
                self.done.set()
        frame = AudioFrame(format='s16', layout='mono', samples=self.samples)
        frame.planes[0].update(chunk.ljust(size, b'\0'))
        from fractions import Fraction
        frame.sample_rate = self.rate
        frame.time_base = Fraction(1, self.rate)
        frame.pts = self.pts
        self.pts += self.samples
        return frame


async def run_sequence(session, unlock, *, should_unlock=False, timeout=60):
    """Do not unlock if answering or playback fails; always release the session."""
    try:
        async with asyncio.timeout(timeout):
            await session.answer()
            await session.play()
            if should_unlock and not await unlock():
                raise RuntimeError('Ring did not acknowledge unlock; no retry was attempted')
    finally:
        await session.close()


class CallSession:
    def __init__(self, ring, device_id, message):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[], bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
        self.track = MessageTrack(message)
        self.pc.addTrack(self.track)
        self.pc.addTransceiver('video', direction='recvonly')
        self.queue = asyncio.Queue()
        self.connected = asyncio.Event()
        self.failure = asyncio.get_running_loop().create_future()
        self.stream = RingWebRtcStream(ring, device_id, on_message_callback=self.queue.put_nowait, keep_alive_timeout=None)
        self.signaling_task = None
        self.drain_tasks = []

        @self.pc.on('connectionstatechange')
        def state_changed():
            if self.pc.connectionState == 'connected':
                self.connected.set()
            elif self.pc.connectionState in ('failed', 'closed'):
                if not self.failure.done():
                    self.failure.set_result(RuntimeError('WebRTC connection ended'))

        @self.pc.on('track')
        def track_received(track):
            async def drain():
                try:
                    while True:
                        await track.recv()
                except Exception:
                    if not self.failure.done():
                        self.failure.set_result(RuntimeError('Incoming media ended'))
            self.drain_tasks.append(asyncio.create_task(drain()))

    async def _wait(self, event, timeout):
        waiter = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait([waiter, self.failure], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if self.failure in done:
                raise self.failure.result()
            if waiter not in done:
                raise TimeoutError('Timed out waiting for Ring media')
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

    async def _signaling(self):
        pending = []
        try:
            while True:
                msg = await self.queue.get()
                if msg.error_code is not None:
                    raise RuntimeError('Ring closed the signaling session')
                if msg.answer:
                    await self.pc.setRemoteDescription(RTCSessionDescription(sdp=msg.answer, type='answer'))
                    for candidate in pending:
                        await self.pc.addIceCandidate(candidate)
                    pending.clear()
                elif msg.candidate:
                    candidate = candidate_from_sdp(msg.candidate.removeprefix('candidate:'))
                    candidate.sdpMLineIndex = msg.sdp_m_line_index or 0
                    if self.pc.remoteDescription:
                        await self.pc.addIceCandidate(candidate)
                    else:
                        pending.append(candidate)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self.failure.done():
                self.failure.set_result(RuntimeError('Ring signaling failed'))

    async def answer(self):
        self.signaling_task = asyncio.create_task(self._signaling())
        await self.pc.setLocalDescription(await self.pc.createOffer())
        await self.stream.generate(self.pc.localDescription.sdp)
        await self._wait(self.connected, 20)
        print('WebRTC connected (physical handset answer behavior needs device verification).')

    async def play(self):
        self.track.ready.set()
        await self._wait(self.track.done, 35)
        # Let the encoder/network drain before issuing unlock.
        await asyncio.sleep(0.5)
        if self.failure.done():
            raise self.failure.result()
        print('Outgoing audio transmitted; visitor audibility requires physical testing.')

    async def close(self):
        tasks = [t for t in [self.signaling_task, *self.drain_tasks] if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.stream.close(), 5)
        await self.pc.close()
        self.track.stop()


def save_token(path, token):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(token, handle)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def main(args):
    token_path = Path(args.token)
    token = json.loads(token_path.read_text()) if token_path.exists() else None
    auth = Auth('ring-handler-prototype/0.1', token, lambda value: save_token(token_path, value))
    try:
        if args.command == 'login':
            username = input('Ring email: ')
            password = getpass.getpass('Ring password: ')
            try:
                await auth.async_fetch_token(username, password)
            except Requires2FAError:
                await auth.async_fetch_token(username, password, input('Ring 2FA code: '))
            print('Token saved locally. Do not share the token file.')
            return
        if not token:
            raise ValueError('Run login first')
        ring = Ring(auth)
        await ring.async_create_session()
        await ring.async_update_data()
        devices = [d for d in ring.devices().other if d.kind == 'intercom_handset_video']
        if args.command == 'devices':
            for device in devices:
                print(f'{device.device_api_id}: {device.name} ({device.kind})')
            return
        device = next((d for d in devices if d.device_api_id == args.device), None)
        if device is None:
            raise ValueError('No matching Ring Intercom Video; run devices')
        # Validate audio before allocating a WebRTC connection.
        if args.message:
            MessageTrack(Path(args.message)).stop()
        session = CallSession(ring, device.device_api_id, Path(args.message) if args.message else None)
        await run_sequence(session, device.async_open_door, should_unlock=args.unlock)
        if args.unlock:
            print('Ring acknowledged unlock request; this does not verify physical door position.')
    finally:
        await auth.async_close()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token', default='data/ring-token.json')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('login')
    sub.add_parser('devices')
    call = sub.add_parser('call', help='Run once while someone is ringing the intercom')
    call.add_argument('--device', type=int, required=True)
    call.add_argument('--message', help='Optional mono 48kHz 16-bit PCM WAV, up to 30 seconds')
    call.add_argument('--unlock', action='store_true', help='Actually unlock after successful playback')
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except (Exception, KeyboardInterrupt) as exc:
        # Do not log library exception contents: they can include credentials/signaling URLs.
        print(f'Stopped: {type(exc).__name__}. No automatic retry. Check authentication, device ID and active call.')
        raise SystemExit(1) from None


if __name__ == '__main__':
    cli()
