from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient,TestServer
from server import make_app,SERVICE_KEY
from google_login import SESSION_COOKIE,NONCE_COOKIE

class BrowserAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.app=make_app(Path(self.temp.name),'a'*40)
        self.service=self.app[SERVICE_KEY]
        self.client=TestClient(TestServer(self.app));await self.client.start_server()
        settings=self.service.store.settings();settings.update(google_client_id='123-test.apps.googleusercontent.com',google_email='owner@example.com');self.service.store.set_settings(settings)

    async def asyncTearDown(self):await self.client.close();self.temp.cleanup()

    async def google(self,email='owner@example.com'):
        response=await self.client.get('/auth/config');config=await response.json()
        headers={'Cookie':f'{NONCE_COOKIE}={response.cookies[NONCE_COOKIE].value}'}
        claims={'sub':'owner-sub','email':email,'email_verified':True,'given_name':'Damon','nonce':config['nonce']}
        with patch('google_login.verify_google',return_value=claims) as verify:
            response=await self.client.post('/auth/google',headers=headers,json={'credential':'test-jwt'})
        verify.assert_called_once_with('test-jwt','123-test.apps.googleusercontent.com')
        return response

    async def test_only_selected_google_email_can_login(self):
        response=await self.google('someone-else@example.com')
        self.assertEqual(response.status,403)
        self.assertNotIn(SESSION_COOKIE,response.cookies)

    async def test_google_session_is_secure_and_old_key_is_retired(self):
        response=await self.google();self.assertEqual(response.status,200)
        cookie=response.cookies[SESSION_COOKIE];self.assertTrue(cookie['secure']);self.assertTrue(cookie['httponly']);self.assertEqual(cookie['path'],'/')
        response=await self.client.post('/auth/key',json={'token':'a'*40})
        self.assertEqual(response.status,403)
        response=await self.client.get('/api/status',headers={'Authorization':'Bearer '+'a'*40})
        self.assertEqual(response.status,401)

    async def test_cookie_write_requires_csrf(self):
        response=await self.google()
        headers={'Cookie':f'{SESSION_COOKIE}={response.cookies[SESSION_COOKIE].value}'}
        response=await self.client.get('/auth/me',headers=headers);me=await response.json()
        self.assertTrue(me['signed_in'])
        response=await self.client.post('/api/disarm',headers=headers,json={});self.assertEqual(response.status,403)
        response=await self.client.post('/api/disarm',headers={**headers,'X-CSRF-Token':me['csrf']},json={});self.assertEqual(response.status,200)

    async def test_nonce_is_required(self):
        with patch('google_login.verify_google') as verify:
            response=await self.client.post('/auth/google',json={'credential':'test-jwt'})
        self.assertEqual(response.status,401);verify.assert_not_called()

    async def test_session_survives_auth_object_recreation(self):
        from google_login import BrowserAuth
        response=await self.google();token=response.cookies[SESSION_COOKIE].value
        auth=BrowserAuth(self.service.store,'a'*40)
        class Request: cookies={SESSION_COOKIE:token}
        self.assertEqual(auth.session(Request())['sub'],'owner-sub')
