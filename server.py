"""Phone control page and authenticated API for a single Ring worker."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import time
import uuid
import re

from aiohttp import web
from ring_doorbell import Auth, AuthenticationError, Requires2FAError

from handler import save_token
from speech import synthesize
from worker import Store, Worker
from scheduling import entry_state, validate_window, get_zone, local_instant
from google_login import BrowserAuth

SERVICE_KEY = web.AppKey('service', object)


class Service:
    def __init__(self, root, admin_token):
        if len(admin_token) < 32:
            raise ValueError('ADMIN_TOKEN must contain at least 32 characters')
        self.store = Store(root)
        self.worker = Worker(self.store)
        self.browser_auth = BrowserAuth(self.store,admin_token)
        self.admin_token = admin_token
        self.lock = asyncio.Lock()
        self.last_login = 0
        self.login_state = "idle"
        self.activity = None
        self.connection_error = None
        self.hardware_path = root / 'hardware-id'
        if not self.hardware_path.exists():
            self.hardware_path.write_text(str(uuid.uuid4()))
        self.worker.hardware_id = self.hardware_path.read_text().strip()

    async def status(self, request):
        settings = self.store.settings()
        return web.json_response({'connection': self.worker.status, 'devices': self.worker.devices,
                                  'authenticated': (self.store.root / 'ring-token.json').exists(),
                                  'verification_required': self.login_state == 'verification_required',
                                  'activity': self.activity, 'connection_error': self.connection_error,
                                  'connection_diagnostics': {'stage': self.worker.connection_phase, 'error_type': self.worker.last_error_type},
                                  'settings': {k: v for k, v in settings.items() if k != 'audio_file'},
                                  'armed': entry_state(settings,time.time())['active'],
                                  'entry': entry_state(settings,time.time()),
                                  'google': {'configured':bool(settings.get('google_client_id') and settings.get('google_email')),
                                             'email':settings.get('google_email',''),'client_id':settings.get('google_client_id',''),
                                             'linked':bool(settings.get('google_owner_sub'))},
                                  'events': self.store.recent()})

    async def login(self, request):
        body = await request.json()
        async with self.lock:
            if time.monotonic() - self.last_login < 3:
                raise web.HTTPTooManyRequests(text='Wait a few seconds before retrying')
            self.last_login = time.monotonic()
            if not all(isinstance(body.get(k), str) and 0 < len(body[k]) <= 1024 for k in ('email', 'password')):
                raise web.HTTPBadRequest(text='Email and password required')
            otp = body.get('code') or None
            if otp is not None and (not isinstance(otp, str) or len(otp) > 32):
                raise web.HTTPBadRequest(text='Invalid verification code')
            # Disconnect first to avoid refreshing/overwriting credentials during login.
            await self.worker.disconnect()
            settings = self.store.settings()
            settings['armed_until'] = 0
            self.store.set_settings(settings)
            self.activity = 'signing_in'
            self.connection_error = None
            auth = Auth('ring-handler/0.2', None, lambda token: save_token(self.store.root / 'ring-token.json', token), hardware_id=self.worker.hardware_id)
            try:
                async with asyncio.timeout(30):
                    await auth.async_fetch_token(body['email'], body['password'], otp)
            except Requires2FAError:
                self.login_state = 'verification_required'
                return web.json_response({'needs_2fa': True, 'message': 'Ring needs a verification code. Check your Ring email or text messages, then enter the code below.'})
            except AuthenticationError:
                self.login_state = 'failed'
                self.connection_error = 'Ring rejected the sign-in. Check your email, password and verification code.'
                raise web.HTTPUnauthorized(text=self.connection_error)
            except TimeoutError:
                self.connection_error = 'Ring took too long to respond. Please try again.'
                raise web.HTTPGatewayTimeout(text=self.connection_error)
            finally:
                self.activity = None
                await auth.async_close()
            self.login_state = 'signed_in'
            self.activity = 'connecting'
            try:
                async with asyncio.timeout(35):
                    await self.worker.connect()
            except Exception:
                self.connection_error = 'Ring sign-in succeeded, but the call listener could not connect. Press Reconnect Ring; you do not need to enter your password again.'
                return web.json_response({'authenticated': True, 'connected': False, 'message': self.connection_error})
            finally:
                self.activity = None
            return web.json_response({'authenticated': True, 'connected': True, 'devices': self.worker.devices})

    async def reconnect(self, request):
        async with self.lock:
            if not (self.store.root / 'ring-token.json').exists():
                raise web.HTTPBadRequest(text='Sign in to Ring first. No saved Ring session was found.')
            self.activity = 'connecting'
            self.connection_error = None
            try:
                async with asyncio.timeout(35):
                    await self.worker.connect()
            except Exception:
                self.connection_error = 'The Ring listener could not connect. Try again in a moment, or sign in again if your Ring session has expired.'
                return web.json_response({'connected': False, 'message': self.connection_error})
            finally:
                self.activity = None
        return web.json_response({'connected': True, 'connection': self.worker.status})

    async def message_audio(self, request):
        filename = self.store.settings().get('audio_file')
        if not filename:
            raise web.HTTPNotFound(text='No spoken message is saved. Enter a message and save it first.')
        return web.FileResponse(self.store.root / filename, headers={'Content-Type': 'audio/wav'})

    async def update_message(self, request):
        body = await request.json()
        text = body.get('message')
        if not isinstance(text, str) or len(text) > 400:
            raise web.HTTPBadRequest(text='Message must be text, up to 400 characters')
        async with self.lock:
            audio = await synthesize(self.store.root, text)
            settings = self.store.settings()
            settings.update(message=text, audio_file=audio)
            self.store.set_settings(settings)
            # Old audio files remain for in-flight calls and are removed on restart.
        return web.json_response({'saved': True})

    async def arm(self, request):
        body = await request.json()
        device = body.get('device_id')
        ttl = body.get('seconds', 3600)
        unlock = body.get('unlock', False)
        if type(ttl) is not int or not 60 <= ttl <= 86400 or type(unlock) is not bool:
            raise web.HTTPBadRequest(text='Choose 60–86400 seconds and a boolean unlock flag')
        async with self.lock:
            if self.worker.status != 'listening' or str(device) not in {str(d['id']) for d in self.worker.devices}:
                raise web.HTTPBadRequest(text='Connect Ring and choose a discovered Intercom Video')
            settings = self.store.settings()
            settings.update(device_id=device, armed_until=time.time() + ttl, unlock=unlock, mode='unlock_only' if unlock else 'observe',paused=False)
            self.store.set_settings(settings)
        return web.json_response({'armed_until': settings['armed_until']})

    async def disarm(self, request):
        async with self.lock:
            settings = self.store.settings()
            settings.update(armed_until=0, unlock=False,paused=True)
            self.store.set_settings(settings)
            if self.worker.call_task and not self.worker.call_task.done():
                self.worker.call_task.cancel()
                try:
                    await self.worker.call_task
                except asyncio.CancelledError:
                    pass
        return web.json_response({'armed': False})

    async def quick_entry(self, request):
        body=await request.json()
        settings=self.store.settings()
        if body.get('until'):
            until=local_instant(body['until'],get_zone(settings['timezone']))
            seconds=int(until-time.time())
        else:
            seconds=body.get('seconds',3600)
        if type(seconds)is not int or not 60<=seconds<=86400:
            raise web.HTTPBadRequest(text='Choose an end time within the next 24 hours')
        async with self.lock:
            if self.worker.status!='listening' or not self.worker.devices:
                raise web.HTTPBadRequest(text='Ring must be connected before enabling entry')
            device=body.get('device_id') or settings.get('device_id') or self.worker.devices[0]['id']
            if str(device) not in {str(d['id']) for d in self.worker.devices}:
                raise web.HTTPBadRequest(text='Choose a connected Intercom')
            settings.update(device_id=device,armed_until=time.time()+seconds,unlock=True,mode='unlock_only',paused=False)
            self.store.set_settings(settings)
        return web.json_response(entry_state(settings,time.time()))

    async def add_window(self, request):
        body=await request.json()
        async with self.lock:
            settings=self.store.settings()
            if len(settings['windows'])>=100:raise web.HTTPBadRequest(text='You can save up to 100 windows')
            window=validate_window(body,settings['timezone'],time.time())
            device=settings.get('device_id') or (self.worker.devices[0]['id'] if self.worker.devices else None)
            if not device:raise web.HTTPBadRequest(text='Connect Ring and choose your Intercom first')
            settings.update(device_id=device,unlock=True,mode='unlock_only')
            settings['windows'].append(window)
            self.store.set_settings(settings)
        return web.json_response({'window':window,'entry':entry_state(settings,time.time())})

    async def change_window(self, request):
        identifier=request.match_info['window_id']
        async with self.lock:
            settings=self.store.settings()
            found=next((w for w in settings['windows'] if w['id']==identifier),None)
            if not found:raise web.HTTPNotFound(text='This window no longer exists')
            if request.method=='DELETE':settings['windows']=[w for w in settings['windows'] if w['id']!=identifier]
            else:
                body=await request.json()
                if type(body.get('enabled')) is not bool:raise web.HTTPBadRequest(text='Choose enabled or disabled')
                found['enabled']=body['enabled']
            self.store.set_settings(settings)
        return web.json_response({'saved':True,'entry':entry_state(settings,time.time())})

    async def resume(self, request):
        async with self.lock:
            settings=self.store.settings();settings.update(paused=False,unlock=True,mode='unlock_only');self.store.set_settings(settings)
        return web.json_response(entry_state(settings,time.time()))

    async def preferences(self, request):
        body=await request.json()
        async with self.lock:
            settings=self.store.settings()
            if 'timezone' in body:
                get_zone(body['timezone']);settings['timezone']=body['timezone']
            if 'google_client_id' in body or 'google_email' in body:
                client=body.get('google_client_id','').strip();email=body.get('google_email','').strip().lower()
                if not re.fullmatch(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com',client):
                    raise web.HTTPBadRequest(text='Enter the Web client ID from Google Cloud')
                if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):raise web.HTTPBadRequest(text='Enter the Google email allowed to access this app')
                if settings.get('google_owner_sub') and email!=settings.get('google_email'):
                    raise web.HTTPBadRequest(text='Sign in with the linked Google account before changing ownership')
                settings.update(google_client_id=client,google_email=email)
            self.store.set_settings(settings)
        return web.json_response({'saved':True})

    async def lifecycle(self, app):
        # Persist the user's explicit windows and manual expiry through deployments.
        settings = self.store.settings()
        migration_marker=self.store.root/'migration-initialized'
        if os.getenv('ENTRY_MIGRATION_PAUSED')=='1' and not migration_marker.exists():
            settings['paused']=True;self.store.set_settings(settings);migration_marker.touch()
        for path in self.store.root.glob('message-*.wav'):
            if path.name != settings['audio_file']:
                path.unlink(missing_ok=True)
        if (self.store.root / 'ring-token.json').exists():
            try:
                await self.worker.connect()
            except Exception:
                self.worker.status = 'connection_failed'
        yield
        await self.worker.disconnect()
        self.store.db.close()


def make_app(root, admin_token, message_token=""):
    if message_token and message_token == admin_token:
        raise ValueError("Message and admin keys must be different")
    if message_token and len(message_token) < 32:
        raise ValueError("MESSAGE_TOKEN must contain at least 32 characters")
    service = Service(root, admin_token)

    @web.middleware
    async def protect(request, handler):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'Referrer-Policy': 'strict-origin-when-cross-origin', 'Cross-Origin-Opener-Policy':'same-origin-allow-popups', 'X-Frame-Options': 'DENY',
                   'Content-Security-Policy': "default-src 'self'; script-src 'self' https://accounts.google.com/gsi/client; style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; connect-src 'self' https://accounts.google.com/gsi/; img-src 'self' data:; frame-src https://accounts.google.com/gsi/; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'"}
        if request.path.startswith('/api/'):
            supplied = request.headers.get('Authorization', '')
            is_admin = (not service.browser_auth.config()['owner_sub'] and hmac.compare_digest(supplied.encode(), ('Bearer ' + admin_token).encode()))
            is_message_writer = (bool(message_token) and request.path == '/api/message' and request.method == 'PUT'
                                 and hmac.compare_digest(supplied.encode(), ('Bearer ' + message_token).encode()))
            identity=service.browser_auth.session(request)
            if identity and not (is_admin or is_message_writer):
                try:service.browser_auth.verify_csrf(request,identity)
                except web.HTTPException as exc:
                    response=web.json_response({'error':exc.text},status=exc.status);response.headers.update(headers);return response
            if not (is_admin or is_message_writer or identity):
                response = web.json_response({'error': 'Unauthorized'}, status=401)
                response.headers.update(headers)
                return response
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.json_response({'error': exc.text if exc.status < 500 or exc.status == 504 else 'Request failed'}, status=exc.status)
        except json.JSONDecodeError:
            response = web.json_response({'error':'Send a valid request'},status=400)
        except ValueError as exc:
            response = web.json_response({'error':str(exc)},status=400)
        except Exception:
            # Never return/log credentials, Ring responses or signaling URLs.
            response = web.json_response({'error': 'Operation failed; check connection status and credentials'}, status=502)
        response.headers.update(headers)
        return response

    app = web.Application(middlewares=[protect], client_max_size=8192)
    app[SERVICE_KEY] = service
    app.cleanup_ctx.append(service.lifecycle)
    base = Path(__file__).parent / 'web'
    async def index(request):
        return web.FileResponse(base / 'index.html')

    async def javascript(request):
        return web.FileResponse(base / 'app.js')

    async def health(request):
        return web.json_response({'ok': True})

    app.add_routes([
        web.get('/', index),
        web.get('/app.js', javascript),
        web.get('/healthz', health),
        web.get('/auth/config',service.browser_auth.public_config),
        web.get('/auth/me',service.browser_auth.me),
        web.post('/auth/key',service.browser_auth.key_login),
        web.post('/auth/google',service.browser_auth.google_login),
        web.post('/auth/logout',service.browser_auth.logout),
        web.post('/api/entry',service.quick_entry),
        web.post('/api/resume',service.resume),
        web.post('/api/windows',service.add_window),
        web.patch('/api/windows/{window_id}',service.change_window),
        web.delete('/api/windows/{window_id}',service.change_window),
        web.put('/api/preferences',service.preferences),
        web.get('/api/status', service.status), web.get('/api/message/audio', service.message_audio), web.post('/api/ring/login', service.login),
        web.post('/api/ring/reconnect', service.reconnect), web.put('/api/message', service.update_message),
        web.post('/api/arm', service.arm), web.post('/api/disarm', service.disarm),
    ])
    return app


if __name__ == '__main__':
    os.umask(0o077)
    # Upstream exception logs can include auth payloads. Application exposes
    # only bounded statuses and durable event outcomes instead.
    logging.disable(logging.CRITICAL)
    web.run_app(make_app(Path(os.getenv('DATA_DIR', 'data')), os.getenv('ADMIN_TOKEN', ''), os.getenv('MESSAGE_TOKEN', '')),
                host='0.0.0.0', port=int(os.getenv('PORT', '8080')), access_log=None, print=None)
