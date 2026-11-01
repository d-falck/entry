"""Ring push listener, durable event reservation, and call orchestration."""
from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import time
from pathlib import Path

from ring_doorbell import Auth
from ring_access import IntercomRing, ActiveBuzzListener, RING_USER_AGENT
from ring_doorbell.listen import RingEventListener

from handler import CallSession, run_sequence, save_token
from scheduling import entry_state


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / 'state.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, at REAL, status TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT)')
        self.db.commit()

    def settings(self):
        row = self.db.execute('SELECT body FROM settings WHERE id=1').fetchone()
        defaults={'device_id':None,'message':'','audio_file':None,'armed_until':0,'unlock':False,'timezone':'Europe/London','windows':[],'paused':False}
        if row:defaults.update(json.loads(row[0]))
        return defaults

    def set_settings(self, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (1, ?)', (json.dumps(value),))
        self.db.commit()

    def reserve(self, key):
        try:
            self.db.execute('INSERT INTO events VALUES (?, ?, ?)', (key, time.time(), 'reserved'))
            self.db.execute('DELETE FROM events WHERE at < ?', (time.time() - 7 * 86400,))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def finish(self, key, status):
        self.db.execute('UPDATE events SET status=? WHERE id=?', (status, key))
        self.db.commit()

    def recent(self):
        return [{'at': at, 'status': status} for at, status in self.db.execute('SELECT at, status FROM events ORDER BY at DESC LIMIT 20')]


class Worker:
    def __init__(self, store):
        self.store = store
        self.ring = None
        self.auth = None
        self.listener = None
        self.call_task = None
        self.last_call = 0.0
        self.hardware_id = None
        self.status = 'not_connected'
        self.connection_phase = None
        self.last_error_type = None
        self.devices = []

    async def connect(self):
        await self.disconnect()
        token_path = self.store.root / 'ring-token.json'
        if not token_path.exists():
            return
        self.status = 'connecting'
        self.connection_phase = 'authentication'
        self.last_error_type = None
        try:
            self.auth = Auth(RING_USER_AGENT, json.loads(token_path.read_text()), lambda token: save_token(token_path, token), hardware_id=self.hardware_id)
            self.ring = IntercomRing(self.auth)
            self.connection_phase = 'device_discovery'
            await self.ring.async_update_data()
            self.devices = [{'id': d.device_api_id, 'name': d.name} for d in self.ring.devices().other if d.kind == 'intercom_handset_video']
            self.connection_phase = 'call_notifications'
            self.listener = ActiveBuzzListener(self.ring)
            self.listener.add_notification_callback(self.on_event)
            if not await self.listener.start(timeout=20):
                raise RuntimeError('Push listener did not start')
            self.status = 'listening'
            self.connection_phase = None
        except Exception as exc:
            self.last_error_type = type(exc).__name__
            await self.disconnect()
            self.status = 'connection_failed'
            raise

    def on_event(self, event):
        """Reserve synchronously before scheduling; never queue calls behind an active one."""
        settings = self.store.settings()
        now = time.time()
        if event.kind not in ('ding', 'com.ring.push.HANDLE_NEW_DING'):
            return
        if str(event.doorbot_id) != str(settings['device_id']):
            return
        if not -5 <= now - event.now <= 30:
            return
        if not entry_state(settings,now)['active'] or self.status != 'listening':
            return
        if (self.call_task and not self.call_task.done()) or now - self.last_call < 15:
            return
        key = f'{event.doorbot_id}:{event.id}'
        if not self.store.reserve(key):
            return
        self.last_call = now
        self.call_task = asyncio.create_task(self.handle(key, settings))

    async def handle(self, key, settings):
        status = 'failed'
        try:
            # Recheck after scheduling: disarming takes effect before connecting.
            if not entry_state(self.store.settings(),time.time())['active']:
                status = 'disarmed'
                return
            device = next(d for d in self.ring.devices().other if str(d.device_api_id) == str(settings['device_id']))

            async def unlock():
                current = self.store.settings()
                if (not entry_state(current,time.time())['active']
                        or str(current['device_id']) != str(settings['device_id'])):
                    raise RuntimeError('Entry disabled during call')
                # Do not retry even if the response is lost after physical unlocking.
                return await device.async_open_door()

            mode=settings.get('mode','answer')
            if mode=='unlock_only':
                async with asyncio.timeout(15):
                    if not await unlock():raise RuntimeError('Unlock was not acknowledged')
                status='unlock_acknowledged'
                return
            if mode=='observe':
                status='buzz_received'
                return
            message = self.store.root / settings['audio_file'] if settings['audio_file'] else None
            session = CallSession(self.ring, device.device_api_id, message)
            await run_sequence(session, unlock, should_unlock=settings['unlock'])
            status = 'unlock_acknowledged' if settings['unlock'] else 'message_sent'
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        except Exception:
            # Upstream errors may include credentials; retain only a bounded status.
            status = 'failed'
        finally:
            self.store.finish(key, status)

    async def disconnect(self):
        if self.call_task and not self.call_task.done():
            self.call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.call_task
        self.call_task = None
        if self.listener:
            with contextlib.suppress(Exception):
                await self.listener.stop()
        self.listener = None
        if self.auth:
            await self.auth.async_close()
        self.auth = self.ring = None
        self.devices = []
        self.status = 'not_connected'
