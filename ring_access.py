"""Direct bearer-authenticated Ring calls, without the unused session-create API.

Ring accepts device discovery and RPC requests with the saved OAuth token.
The generic library's eager /clients_api/session POST currently returns 406
for this deployment, so this adapter avoids requiring that unrelated request.
"""
import asyncio
import time

from ring_doorbell import Ring
from ring_doorbell.const import (
    API_URI, API_VERSION, DEVICES_ENDPOINT, SUBSCRIPTION_ENDPOINT,
)
from ring_doorbell.listen import RingEventListener

RING_USER_AGENT = 'android:com.ringapp:3.98.0(70492092)'


class IntercomRing(Ring):
    async def async_query(self, url, method='GET', extra_params=None, data=None,
                          json=None, timeout=None, base_uri=API_URI):
        return await self.auth.async_query(base_uri+url, method=method,
            extra_params=extra_params, data=data, json=json, timeout=timeout)

    async def async_update_devices(self):
        response = await self.auth.async_query(API_URI+DEVICES_ENDPOINT)
        self.devices_data = {
            kind: {device['id']:device for device in devices}
            for kind, devices in response.json().items()
            if isinstance(devices, list)
        }

    async def async_update_data(self):
        # Auto-entry needs device discovery; groups and active-camera dings
        # are unrelated. Incoming rings are delivered through the push listener.
        await self.async_update_devices()


class IntercomEventListener(RingEventListener):
    async def add_subscription_to_ring(self, token):
        payload={'device':{'metadata':{'api_version':API_VERSION,
            'device_model':self._device_model,'pn_dict_version':'2.0.0','pn_service':'fcm'},
            'os':'android','push_notification_token':token}}
        response=await self._ring.auth.async_query(API_URI+SUBSCRIPTION_ENDPOINT,
            method='PATCH',json=payload,raise_for_status=False)
        self.subscribed=response.status_code==204
        if not self.subscribed:
            raise RuntimeError(f'Ring notification subscription HTTP {response.status_code}')
        self._ring.session_refresh_time=time.monotonic()
        if not self._ring.devices_data:
            await self._ring.async_update_devices()

    async def _periodic_session_refresh(self):
        while self.started:
            await asyncio.sleep(6*60*60)
            await self._ring.async_update_devices()
            await self.add_subscription_to_ring(self.fcm_token)

class ActiveBuzzListener:
    """Poll active Ring dings when push registration is unavailable."""
    def __init__(self, ring):
        self.ring=ring
        self.callbacks=[]
        self.task=None
        self.started=False
        self.seen=set()

    def add_notification_callback(self, callback):
        self.callbacks.append(callback)

    async def _fetch(self):
        from ring_doorbell.const import DINGS_ENDPOINT
        response=await self.ring.auth.async_query(API_URI+DINGS_ENDPOINT)
        data=response.json()
        if not isinstance(data,list):
            raise RuntimeError('Unexpected active-buzz response')
        return data

    async def start(self, **kwargs):
        initial=await self._fetch()
        self.seen={str(d.get('id')) for d in initial}
        self.started=True
        self.task=asyncio.create_task(self._poll())
        return True

    async def _poll(self):
        from types import SimpleNamespace
        from ring_doorbell.util import parse_datetime
        while self.started:
            await asyncio.sleep(2)
            try:
                dings=await self._fetch()
                active=set()
                for ding in dings:
                    key=str(ding.get('id'));active.add(key)
                    if key in self.seen:continue
                    self.seen.add(key)
                    device=ding.get('doorbot') or {}
                    device_id=ding.get('doorbot_id') or device.get('id')
                    created=ding.get('created_at')
                    now=parse_datetime(created).timestamp() if created else time.time()
                    event=SimpleNamespace(id=ding.get('id'),doorbot_id=device_id,
                        kind=ding.get('kind','ding'),now=now)
                    for callback in self.callbacks:callback(event)
                # Worker also durably deduplicates event IDs across restarts.
                if len(self.seen)>1000:self.seen=active
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(5)

    async def stop(self):
        self.started=False
        if self.task:
            self.task.cancel()
            try:await self.task
            except asyncio.CancelledError:pass
