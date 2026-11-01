from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from aiohttp.test_utils import TestClient, TestServer
from server import make_app, SERVICE_KEY

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = make_app(Path(self.temp.name),'a'*40)
        self.service = self.app[SERVICE_KEY]
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {'Authorization':'Bearer '+'a'*40}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_api_requires_key(self):
        response = await self.client.get('/api/status')
        self.assertEqual(response.status,401)
        response = await self.client.get('/api/status',headers=self.headers)
        self.assertEqual(response.status,200)
        self.assertFalse((await response.json())['armed'])
        self.assertEqual(response.headers['Cache-Control'],'no-store')

    async def test_arm_requires_real_device_and_bounds(self):
        for body in [{'device_id':42,'seconds':60},{'device_id':42,'seconds':0}]:
            response = await self.client.post('/api/arm',headers=self.headers,json=body)
            self.assertEqual(response.status,400)
        self.service.worker.status = 'listening'
        self.service.worker.devices = [{'id':42,'name':'Entrance'}]
        response = await self.client.post('/api/arm',headers=self.headers,json={'device_id':42,'seconds':60,'unlock':False})
        self.assertEqual(response.status,200)
        response = await self.client.post('/api/disarm',headers=self.headers,json={})
        self.assertEqual(response.status,200)
        self.assertEqual(self.service.store.settings()['armed_until'],0)

    async def test_message_is_prepared_before_replacing_settings(self):
        with patch('server.synthesize',AsyncMock(return_value='prepared.wav')):
            response = await self.client.put('/api/message',headers=self.headers,json={'message':'Hello'})
        self.assertEqual(response.status,200)
        self.assertEqual(self.service.store.settings()['audio_file'],'prepared.wav')
        with patch('server.synthesize',AsyncMock(side_effect=RuntimeError('secret'))):
            response = await self.client.put('/api/message',headers=self.headers,json={'message':'Bad'})
        self.assertEqual(response.status,502)
        self.assertNotIn('secret',await response.text())
        self.assertEqual(self.service.store.settings()['message'],'Hello')

    async def test_oversized_message_rejected(self):
        response = await self.client.put('/api/message',headers=self.headers,json={'message':'a'*401})
        self.assertEqual(response.status,400)

    async def test_weak_key_rejected(self):
        with self.assertRaises(ValueError): make_app(Path(self.temp.name),'short')

class MessagePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_key_cannot_enable_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            app = make_app(Path(temp),'a'*40,'m'*40)
            async with TestClient(TestServer(app)) as client:
                headers = {'Authorization':'Bearer '+'m'*40}
                with patch('server.synthesize',AsyncMock(return_value='audio.wav')):
                    response = await client.put('/api/message',headers=headers,json={'message':'Hi'})
                self.assertEqual(response.status,200)
                for path in ('arm','disarm','ring/login'):
                    response = await client.post('/api/'+path,headers=headers,json={})
                    self.assertEqual(response.status,401)

class RingLoginFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=make_app(Path(self.temp.name),'a'*40)
        self.service=self.app[SERVICE_KEY]
        self.client=TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers={'Authorization':'Bearer '+'a'*40}
        self.body={'email':'test@example.com','password':'test-password'}

    async def asyncTearDown(self):
        await self.client.close();self.temp.cleanup()

    async def test_verification_step_is_explicit_in_status(self):
        from ring_doorbell import Requires2FAError
        auth=AsyncMock()
        auth.async_fetch_token.side_effect=Requires2FAError()
        with patch('server.Auth',return_value=auth):
            response=await self.client.post('/api/ring/login',headers=self.headers,json=self.body)
        self.assertTrue((await response.json())['needs_2fa'])
        response=await self.client.get('/api/status',headers=self.headers)
        status=await response.json()
        self.assertTrue(status['verification_required'])
        self.assertIsNone(status['activity'])

    async def test_bad_credentials_have_safe_actionable_error(self):
        from ring_doorbell import AuthenticationError
        auth=AsyncMock()
        auth.async_fetch_token.side_effect=AuthenticationError('private upstream response')
        with patch('server.Auth',return_value=auth):
            response=await self.client.post('/api/ring/login',headers=self.headers,json=self.body)
        self.assertEqual(response.status,401)
        data=await response.json()
        self.assertIn('Ring rejected',data['error']);self.assertNotIn('private',data['error'])

    async def test_saved_session_distinguished_from_listener_failure(self):
        from handler import save_token
        auth=AsyncMock()
        async def signed_in(*args):save_token(self.service.store.root/'ring-token.json',{'test':'not-a-real-token'})
        auth.async_fetch_token.side_effect=signed_in
        with patch('server.Auth',return_value=auth),patch.object(self.service.worker,'connect',AsyncMock(side_effect=RuntimeError('private'))):
            response=await self.client.post('/api/ring/login',headers=self.headers,json=self.body)
        data=await response.json()
        self.assertTrue(data['authenticated']);self.assertFalse(data['connected'])
        response=await self.client.get('/api/status',headers=self.headers)
        self.assertTrue((await response.json())['authenticated'])

    async def test_reconnect_without_session_tells_user_to_sign_in(self):
        response=await self.client.post('/api/ring/reconnect',headers=self.headers,json={})
        self.assertEqual(response.status,400)
        self.assertIn('Sign in',(await response.json())['error'])

class ScheduleApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import time
        self.temp=tempfile.TemporaryDirectory();self.app=make_app(Path(self.temp.name),'a'*40)
        self.service=self.app[SERVICE_KEY];self.service.worker.devices=[{'id':42,'name':'Entrance'}];self.service.worker.status='listening'
        self.client=TestClient(TestServer(self.app));await self.client.start_server();self.headers={'Authorization':'Bearer '+'a'*40}

    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()

    async def test_save_toggle_and_remove_weekly_window(self):
        response=await self.client.post('/api/windows',headers=self.headers,json={'kind':'weekly','days':[0,2,4],'start':'09:00','end':'17:00'})
        self.assertEqual(response.status,200);identifier=(await response.json())['window']['id']
        response=await self.client.patch('/api/windows/'+identifier,headers=self.headers,json={'enabled':False});self.assertEqual(response.status,200)
        self.assertFalse(self.service.store.settings()['windows'][0]['enabled'])
        response=await self.client.delete('/api/windows/'+identifier,headers=self.headers);self.assertEqual(response.status,200)
        self.assertEqual(self.service.store.settings()['windows'],[])

    async def test_pause_preserves_windows_and_resume_restores(self):
        settings=self.service.store.settings();settings['windows']=[{'id':'weekly','kind':'weekly','days':list(range(7)),'start':'00:00','end':'23:59','enabled':True}];self.service.store.set_settings(settings)
        response=await self.client.post('/api/disarm',headers=self.headers,json={});self.assertEqual(response.status,200)
        self.assertTrue(self.service.store.settings()['paused']);self.assertEqual(len(self.service.store.settings()['windows']),1)
        response=await self.client.post('/api/resume',headers=self.headers,json={});self.assertEqual(response.status,200)
        self.assertFalse(self.service.store.settings()['paused'])

    async def test_quick_entry_and_validation(self):
        response=await self.client.post('/api/entry',headers=self.headers,json={'seconds':900});self.assertEqual(response.status,200)
        self.assertTrue((await response.json())['active'])
        response=await self.client.post('/api/windows',headers=self.headers,json={'kind':'weekly','days':[],'start':'09:00','end':'17:00'})
        self.assertEqual(response.status,400);self.assertIn('Choose at least one day',(await response.json())['error'])
