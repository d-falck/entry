import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from worker import Store, Worker

class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.store.set_settings({'device_id':42,'message':'','audio_file':None,'armed_until':time.time()+60,'unlock':True})
        self.worker = Worker(self.store)
        self.worker.status = 'listening'
        self.worker.handle = AsyncMock()

    async def asyncTearDown(self):
        if self.worker.call_task: await self.worker.call_task
        self.store.db.close()
        self.temp.cleanup()

    def event(self, **updates):
        values = dict(kind='ding',doorbot_id=42,id=101,now=time.time())
        values.update(updates)
        return SimpleNamespace(**values)

    async def test_duplicate_not_replayed_after_restart(self):
        event = self.event()
        self.worker.on_event(event)
        await self.worker.call_task
        fresh = Worker(self.store)
        fresh.status = 'listening'
        fresh.handle = AsyncMock()
        fresh.on_event(event)
        self.assertIsNone(fresh.call_task)

    async def test_only_matching_fresh_dings(self):
        for event in [self.event(kind='intercom_unlock'),self.event(doorbot_id=99),self.event(now=time.time()-31),self.event(now=time.time()+10)]:
            self.worker.on_event(event)
        self.assertIsNone(self.worker.call_task)
        self.worker.on_event(self.event())
        await self.worker.call_task
        self.worker.handle.assert_awaited_once()

    async def test_disarmed_does_nothing(self):
        settings = self.store.settings()
        settings['armed_until'] = 0
        self.store.set_settings(settings)
        self.worker.on_event(self.event())
        self.assertIsNone(self.worker.call_task)

    async def test_busy_drops_second_call(self):
        blocker = asyncio.Event()
        async def block(*args): await blocker.wait()
        self.worker.handle.side_effect = block
        self.worker.on_event(self.event())
        original = self.worker.call_task
        self.worker.on_event(self.event(id=102))
        self.assertIs(self.worker.call_task,original)
        blocker.set()
        await original

    async def test_disarming_during_play_prevents_unlock(self):
        worker = Worker(self.store)
        device = SimpleNamespace(device_api_id=42,async_open_door=AsyncMock(return_value=True))
        worker.ring = SimpleNamespace(devices=lambda:SimpleNamespace(other=[device]))
        session = AsyncMock()
        async def disarm():
            settings = self.store.settings()
            settings['armed_until'] = 0
            self.store.set_settings(settings)
        session.play.side_effect = disarm
        self.store.reserve('42:101')
        with patch('worker.CallSession',return_value=session):
            await worker.handle('42:101',self.store.settings())
        device.async_open_door.assert_not_awaited()
        session.close.assert_awaited_once()
        self.assertEqual(self.store.recent()[0]['status'],'failed')

class DirectEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlock_only_does_not_create_audio_session(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp))
            settings={'device_id':42,'message':'ignored','audio_file':None,'armed_until':time.time()+60,'unlock':True,'mode':'unlock_only'}
            store.set_settings(settings);store.reserve('42:1')
            worker=Worker(store)
            device=SimpleNamespace(device_api_id=42,async_open_door=AsyncMock(return_value=True))
            worker.ring=SimpleNamespace(devices=lambda:SimpleNamespace(other=[device]))
            with patch('worker.CallSession') as media:
                await worker.handle('42:1',settings)
            media.assert_not_called();device.async_open_door.assert_awaited_once()
            self.assertEqual(store.recent()[0]['status'],'unlock_acknowledged')
            store.db.close()

    async def test_saved_window_unlocks_without_manual_timer(self):
        with tempfile.TemporaryDirectory() as temp:
            store=Store(Path(temp));now=time.time()
            settings={'device_id':42,'armed_until':0,'unlock':True,'mode':'unlock_only','windows':[{'id':'w','kind':'once','enabled':True,'start':now-10,'end':now+60}]}
            store.set_settings(settings);store.reserve('42:2')
            worker=Worker(store);device=SimpleNamespace(device_api_id=42,async_open_door=AsyncMock(return_value=True))
            worker.ring=SimpleNamespace(devices=lambda:SimpleNamespace(other=[device]))
            await worker.handle('42:2',settings)
            device.async_open_door.assert_awaited_once();store.db.close()
