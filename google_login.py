"""Google ID-token verification and durable, opaque browser sessions."""
import asyncio
import hashlib
import hmac
import json
import secrets
import time

from aiohttp import web

SESSION_COOKIE='__Host-entry_session'
NONCE_COOKIE='__Host-entry_nonce'


def digest(value):return hashlib.sha256(value.encode()).hexdigest()


def verify_google(credential,client_id):
    # Google's maintained library checks signature, issuer, audience and expiry.
    from google.oauth2 import id_token
    from google.auth.transport.requests import Request
    return id_token.verify_oauth2_token(credential,Request(),client_id)


class BrowserAuth:
    def __init__(self,store,admin_token):
        self.store=store;self.admin_token=admin_token
        db=store.db
        db.execute('CREATE TABLE IF NOT EXISTS browser_sessions (hash TEXT PRIMARY KEY, body TEXT, expires REAL)')
        db.execute('CREATE TABLE IF NOT EXISTS login_nonces (hash TEXT PRIMARY KEY, expires REAL)')
        db.commit()

    def config(self):
        s=self.store.settings()
        return {'client_id':s.get('google_client_id',''),'email':s.get('google_email',''),
                'owner_sub':s.get('google_owner_sub','')}

    def session(self,request):
        token=request.cookies.get(SESSION_COOKIE,'')
        if not token:return None
        row=self.store.db.execute('SELECT body,expires FROM browser_sessions WHERE hash=?',(digest(token),)).fetchone()
        if not row or row[1]<=time.time():return None
        return json.loads(row[0])

    def issue_session(self,response,identity):
        token=secrets.token_urlsafe(32)
        identity={**identity,'csrf':secrets.token_urlsafe(32)}
        expires=time.time()+30*86400
        db=self.store.db
        db.execute('DELETE FROM browser_sessions WHERE expires<?',(time.time(),))
        db.execute('INSERT INTO browser_sessions VALUES (?,?,?)',(digest(token),json.dumps(identity),expires))
        db.commit()
        response.set_cookie(SESSION_COOKIE,token,max_age=30*86400,httponly=True,secure=True,samesite='Lax',path='/')

    async def public_config(self,request):
        cfg=self.config();nonce=secrets.token_urlsafe(32);now=time.time()
        db=self.store.db
        db.execute('DELETE FROM login_nonces WHERE expires<?',(now,))
        db.execute('INSERT INTO login_nonces VALUES (?,?)',(digest(nonce),now+600));db.commit()
        response=web.json_response({'client_id':cfg['client_id'],'google_ready':bool(cfg['client_id'] and cfg['email']),
            'allow_setup_key':not bool(cfg['owner_sub']),'nonce':nonce})
        response.set_cookie(NONCE_COOKIE,nonce,max_age=600,httponly=True,secure=True,samesite='Strict',path='/')
        return response

    async def me(self,request):
        identity=self.session(request)
        if not identity:return web.json_response({'signed_in':False})
        return web.json_response({'signed_in':True,**identity})

    async def key_login(self,request):
        if self.config()['owner_sub']:
            raise web.HTTPForbidden(text='Google sign-in is enabled. Use your Google account.')
        body=await request.json()
        token=body.get('token','')
        if not isinstance(token,str) or not hmac.compare_digest(token.encode(),self.admin_token.encode()):
            raise web.HTTPUnauthorized(text='That setup key is not correct')
        response=web.json_response({'signed_in':True})
        self.issue_session(response,{'name':'Damon','email':'','method':'setup'})
        return response

    async def google_login(self,request):
        cfg=self.config()
        if not(cfg['client_id'] and cfg['email']):raise web.HTTPBadRequest(text='Finish Google setup first')
        body=await request.json();credential=body.get('credential')
        if not isinstance(credential,str) or len(credential)>12000:raise web.HTTPBadRequest(text='Invalid Google sign-in response')
        nonce=request.cookies.get(NONCE_COOKIE,'')
        row=self.store.db.execute('SELECT expires FROM login_nonces WHERE hash=?',(digest(nonce),)).fetchone()
        if not nonce or not row or row[0]<=time.time():raise web.HTTPUnauthorized(text='Sign-in expired. Reload and try again.')
        try:
            claims=await asyncio.to_thread(verify_google,credential,cfg['client_id'])
        except Exception:raise web.HTTPUnauthorized(text='Google sign-in could not be verified. Please try again.') from None
        if not hmac.compare_digest(str(claims.get('nonce','')),nonce):raise web.HTTPUnauthorized(text='Invalid sign-in request')
        if claims.get('email_verified') is not True or claims.get('email','').casefold()!=cfg['email'].casefold():
            raise web.HTTPForbidden(text='This app is private. Sign in with the Google account selected in Settings.')
        sub=claims.get('sub')
        if not isinstance(sub,str) or not sub or (cfg['owner_sub'] and sub!=cfg['owner_sub']):
            raise web.HTTPForbidden(text='This Google account is not allowed')
        used=self.store.db.execute('DELETE FROM login_nonces WHERE hash=? AND expires>?',(digest(nonce),time.time()))
        if used.rowcount!=1:raise web.HTTPUnauthorized(text='This sign-in was already used. Reload and try again.')
        if not cfg['owner_sub']:
            self.store.db.execute('DELETE FROM browser_sessions')
            settings=self.store.settings();settings['google_owner_sub']=sub;self.store.set_settings(settings)
        db=self.store.db
        db.execute('DELETE FROM login_nonces WHERE hash=?',(digest(nonce),));db.commit()
        response=web.json_response({'signed_in':True})
        self.issue_session(response,{'name':claims.get('given_name') or 'Damon','email':claims['email'],'sub':sub,'method':'google'})
        response.del_cookie(NONCE_COOKIE,path='/')
        return response

    async def logout(self,request):
        identity=self.session(request)
        if identity:self.verify_csrf(request,identity)
        token=request.cookies.get(SESSION_COOKIE,'')
        if token:
            self.store.db.execute('DELETE FROM browser_sessions WHERE hash=?',(digest(token),));self.store.db.commit()
        response=web.json_response({'signed_in':False});response.del_cookie(SESSION_COOKIE,path='/')
        return response

    def verify_csrf(self,request,identity):
        if request.method not in ('GET','HEAD','OPTIONS'):
            supplied=request.headers.get('X-CSRF-Token','')
            if not hmac.compare_digest(supplied,identity['csrf']):raise web.HTTPForbidden(text='Reload the page and try again')
