"""Finish the prepared Fly migration, or update it after a successful cutover.

Run only with a Fly organization token in FLY_API_TOKEN. Credentials and Ring
tokens never enter this repository, GitHub artifacts, or deployment logs.
"""
import base64
import io
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OLD_APP, OLD_MACHINE = 'ring-entrance-damon-7c3a', '8911105b43d728'
APP, MACHINE = 'damon-entry', 'd8d9932eb00538'
CHECKPOINT = '/data/entry-cutover.json'


def api(path, body=None, method=None):
    token = os.environ['FLY_API_TOKEN']
    authorization = token if token.startswith('FlyV1 ') else ('FlyV1 ' if token.startswith(('fm1r_', 'fm2_')) else 'Bearer ')+token
    req = urllib.request.Request('https://api.machines.dev'+path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method, headers={'Authorization':authorization,
                                'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        # Never log response bodies: an upstream error might echo credentials.
        raise RuntimeError(f'Fly API returned HTTP {error.code} for {method or ("POST" if body is not None else "GET")} {path}') from None


def remote(app, machine, code):
    result = api(f'/v1/apps/{app}/machines/{machine}/exec',
                 {'command':['/data/runtime/bin/python','-c',code], 'timeout':30})
    if result.get('exit_code') != 0:
        raise RuntimeError('Remote check failed on '+app+'; private output withheld')
    return result.get('stdout','').strip()


def ready(app, machine):
    for _ in range(60):
        try:
            result = remote(app,machine,"import http.client;c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3);c.request('GET','/healthz');print(c.getresponse().status)")
            if result == '200': return
        except Exception: pass
        time.sleep(2)
    raise RuntimeError('Health check timed out on '+app)


def bundle():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in ['handler.py','worker.py','speech.py','server.py','ring_access.py',
                     'scheduling.py','google_login.py','requirements-lock.txt','requirements.txt']:
            archive.write(ROOT/name,name)
        for path in sorted((ROOT/'web').rglob('*')):
            if path.is_file(): archive.write(path,str(path.relative_to(ROOT)))
    return base64.b64encode(buffer.getvalue()).decode()


def deploy_source():
    config = api(f'/v1/apps/{APP}/machines/{MACHINE}')['config']
    # The earlier copy completed. Remove its temporary credential file before restart.
    config['files'] = [f for f in config['files'] if f['guest_path'] != '/opt/entry-transfer.zip']
    source = next(f for f in config['files'] if f['guest_path']=='/opt/ring-source.zip')
    source['raw_value'] = bundle()
    api(f'/v1/apps/{APP}/machines/{MACHINE}',{'config':config})
    ready(APP,MACHINE)


def redirect_config(config):
    code = f'''from http.server import HTTPServer,BaseHTTPRequestHandler
class Redirect(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(302);self.send_header('Location','https://{APP}.fly.dev');self.end_headers()
 def do_POST(self):self.send_error(410,'Use the new Entry address')
 def log_message(self,*args):pass
HTTPServer(('0.0.0.0',8080),Redirect).serve_forever()
'''
    config['init'] = {'exec':['python','-c',code]}
    return config


def finish():
    email = os.environ.get('ENTRY_GOOGLE_EMAIL','').strip().lower()
    client = os.environ.get('GOOGLE_CLIENT_ID','').strip()
    if '@' not in email or not client.endswith('.apps.googleusercontent.com'):
        raise RuntimeError('Supply the allowed Google email and Google Client ID')
    # Hide inputs if a dependency unexpectedly prints them later.
    print('::add-mask::'+email,flush=True)
    checkpoint = json.loads(remote(APP,MACHINE,
        f"import json;from pathlib import Path;p=Path({CHECKPOINT!r});print(p.read_text() if p.exists() else '{{}}')"))
    if checkpoint.get('complete'):
        deploy_source()
        print('App updated. Existing Ring session and entry windows retained.',flush=True)
        return
    # A first-time migration must never start a second active entry worker.
    paused = remote(APP,MACHINE,"import sqlite3,json;d=sqlite3.connect('/data/state.sqlite');s=json.loads(d.execute('SELECT body FROM settings WHERE id=1').fetchone()[0]);print(s.get('paused') is True)")
    if paused != 'True':
        raise RuntimeError('New worker must be paused before the initial migration')
    deploy_source()
    connection = json.loads(remote(APP,MACHINE,"""import os,json,urllib.request
r=urllib.request.Request('http://127.0.0.1:8080/api/status',headers={'Authorization':'Bearer '+os.environ['ADMIN_TOKEN']})
d=json.load(urllib.request.urlopen(r,timeout=5));print(json.dumps({'connection':d['connection'],'has_devices':bool(d['devices'])}))
"""))
    if connection != {'connection':'listening','has_devices':True}:
        raise RuntimeError('New worker cannot reach Ring. Original worker remains unchanged.')
    if not checkpoint:
        latest = json.loads(remote(OLD_APP,OLD_MACHINE,"import sqlite3;print(sqlite3.connect('/data/state.sqlite').execute('SELECT body FROM settings WHERE id=1').fetchone()[0])"))
        if latest.get('google_owner_sub') and latest.get('google_email') != email:
            raise RuntimeError('Existing Google owner differs; migration stopped')
        latest.update(google_client_id=client,google_email=email)
        checkpoint = {'settings':latest,'complete':False}
        remote(APP,MACHINE,f"import os;from pathlib import Path;os.umask(0o077);Path({CHECKPOINT!r}).write_text({json.dumps(checkpoint)!r})")
    old = api(f'/v1/apps/{OLD_APP}/machines/{OLD_MACHINE}')
    api(f'/v1/apps/{OLD_APP}/machines/{OLD_MACHINE}',{'config':redirect_config(old['config'])})
    # Confirm the old entry process has been replaced before activating the new one.
    for _ in range(60):
        try:
            result = remote(OLD_APP,OLD_MACHINE,"import http.client;c=http.client.HTTPConnection('127.0.0.1',8080,timeout=3);c.request('GET','/');r=c.getresponse();print(str(r.status)+' '+str(r.getheader('Location')))")
            if result == f'302 https://{APP}.fly.dev': break
        except Exception: pass
        time.sleep(2)
    else:
        raise RuntimeError('Old-worker shutdown not confirmed; new entry remains paused')
    remote(APP,MACHINE,f"""import json,sqlite3
from pathlib import Path
p=Path({CHECKPOINT!r});checkpoint=json.loads(p.read_text());db=sqlite3.connect('/data/state.sqlite')
db.execute('INSERT OR REPLACE INTO settings VALUES(1,?)',(json.dumps(checkpoint['settings']),));db.commit()
checkpoint['complete']=True;p.write_text(json.dumps(checkpoint))
""")
    print('Migration complete. Google configured; existing entry settings restored.',flush=True)


def domain():
    result = api(f'/v1/apps/{APP}/certificates/acme',{'hostname':'entry.damonfalck.me'})
    requirements = result.get('dns_requirements',{})
    output = 'Entry: https://damon-entry.fly.dev\n\nDNS records returned by Fly:\n```json\n'+json.dumps(requirements,indent=2)+'\n```\n'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a') as file: file.write(output)
    print(output,flush=True)


if __name__ == '__main__':
    finish()
    domain()
