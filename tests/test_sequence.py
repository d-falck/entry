import asyncio
import unittest
from unittest.mock import AsyncMock
from handler import run_sequence

class SequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_order(self):
        actions = []
        session = AsyncMock()
        for name in ('answer', 'play', 'close'):
            getattr(session, name).side_effect = lambda n=name: actions.append(n)
        async def unlock():
            actions.append('unlock')
            return True
        await run_sequence(session, unlock, should_unlock=True)
        self.assertEqual(actions, ['answer', 'play', 'unlock', 'close'])

    async def test_failure_never_unlocks(self):
        for stage in ('answer', 'play'):
            session, unlock = AsyncMock(), AsyncMock()
            getattr(session, stage).side_effect = RuntimeError('failure')
            with self.assertRaises(RuntimeError):
                await run_sequence(session, unlock, should_unlock=True)
            unlock.assert_not_awaited()
            session.close.assert_awaited_once()

    async def test_default_does_not_unlock(self):
        session, unlock = AsyncMock(), AsyncMock()
        await run_sequence(session, unlock)
        unlock.assert_not_awaited()

    async def test_timeout_cleans_up(self):
        session, unlock = AsyncMock(), AsyncMock()
        session.answer.side_effect = lambda: None
        async def block():
            await asyncio.Event().wait()
        session.play.side_effect = block
        with self.assertRaises(TimeoutError):
            await run_sequence(session, unlock, should_unlock=True, timeout=.01)
        unlock.assert_not_awaited()
        session.close.assert_awaited_once()

class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_waits_for_connection_and_finishes(self):
        import tempfile
        import wave
        from pathlib import Path
        from handler import MessageTrack
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'test.wav'
            with wave.open(str(path), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(b'\x01\x00' * 960)
            track = MessageTrack(path)
            self.assertEqual(bytes((await track.recv()).planes[0]), b'\0' * 1920)
            self.assertFalse(track.done.is_set())
            track.ready.set()
            self.assertEqual(bytes((await track.recv()).planes[0]), b'\x01\x00' * 960)
            self.assertFalse(track.done.is_set())
            await track.recv()
            self.assertTrue(track.done.is_set())
            track.stop()
