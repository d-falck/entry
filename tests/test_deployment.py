import importlib.util
import json
import os
from pathlib import Path
import unittest
import base64
import io
import zipfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('finish',Path(__file__).resolve().parents[1]/'deploy/finish.py')
finish = importlib.util.module_from_spec(spec)
spec.loader.exec_module(finish)


class MigrationGuards(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'ENTRY_GOOGLE_EMAIL':'owner@example.com',
            'GOOGLE_CLIENT_ID':'123.apps.googleusercontent.com'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.inspection = patch.object(finish,'prepare_inspection')
        self.inspection.start()
        self.addCleanup(self.inspection.stop)

    def test_bundle_matches_installed_bootstrap_directory(self):
        # The installed bootstrap extracts at /app and starts /app/ring-handler/server.py.
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(finish.bundle()))) as archive:
            names = archive.namelist()
            self.assertIn('ring-handler/server.py',names)
            self.assertIn('ring-handler/requirements-lock.txt',names)
            self.assertIn('ring-handler/web/index.html',names)
            self.assertTrue(all(name.startswith('ring-handler/') for name in names))
            self.assertNotIn('ring-handler/data/ring-token.json',names)

    def test_active_new_worker_stops_migration_before_any_restart(self):
        with patch.object(finish,'remote',side_effect=['{}','False']), patch.object(finish,'deploy_source') as deploy:
            with self.assertRaisesRegex(RuntimeError,'must be paused'):
                finish.finish()
            deploy.assert_not_called()

    def test_failed_ring_connection_preserves_original_worker(self):
        results = ['{}','True',json.dumps({'connection':'disconnected','has_devices':False})]
        def remote(*args):
            if results:return results.pop(0)
            return json.dumps({'connection':'disconnected','has_devices':False})
        with patch.object(finish,'remote',side_effect=remote), patch.object(finish,'deploy_source'), patch.object(finish,'api') as api, patch.object(finish.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'Original worker remains unchanged'):
                finish.finish()
            api.assert_not_called()

    def test_original_redirect_confirmed_before_new_entry_activation(self):
        events = []
        results = iter(['{}','True',json.dumps({'connection':'listening','has_devices':True}),
                        json.dumps({'paused':False,'armed_until':1234,'windows':[{'id':'existing'}]}),
                        '',f'302 https://{finish.APP}.fly.dev',''])
        def remote(app,machine,code):
            if "SELECT body" in code and app == finish.OLD_APP: events.append('read latest settings')
            if "c.request('GET','/')" in code: events.append('redirect confirmed')
            if 'INSERT OR REPLACE' in code: events.append('activate new entry')
            return next(results)
        def api(path,body=None,method=None):
            if body: events.append('replace old worker')
            return {'config':{}}
        with patch.object(finish,'remote',side_effect=remote), patch.object(finish,'api',side_effect=api), patch.object(finish,'deploy_source'):
            finish.finish()
        self.assertEqual(events,['read latest settings','replace old worker','redirect confirmed','activate new entry'])

    def test_completed_cutover_only_updates_new_app(self):
        with patch.object(finish,'remote',return_value='{"complete":true}'), patch.object(finish,'deploy_source') as deploy, patch.object(finish,'api') as api:
            finish.finish()
            deploy.assert_called_once()
            api.assert_not_called()
