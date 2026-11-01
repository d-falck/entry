from datetime import datetime
import unittest
from scheduling import entry_state,validate_window,get_zone


def at(value):return datetime.fromisoformat(value).timestamp()

class ScheduleTests(unittest.TestCase):
    def settings(self,**kwargs):return {'timezone':'Europe/London','windows':[],'armed_until':0,'unlock':True,**kwargs}

    def test_one_off_start_inclusive_end_exclusive(self):
        now=at('2026-11-02T10:00:00+00:00')
        window=validate_window({'kind':'once','start':'2026-11-02T11:00','end':'2026-11-02T12:00'},'Europe/London',now)
        s=self.settings(windows=[window])
        self.assertFalse(entry_state(s,window['start']-1)['active'])
        self.assertTrue(entry_state(s,window['start'])['active'])
        self.assertFalse(entry_state(s,window['end'])['active'])

    def test_weekly_overnight_uses_start_day(self):
        s=self.settings(windows=[{'id':'1','kind':'weekly','days':[4],'start':'22:00','end':'02:00','enabled':True}])
        self.assertTrue(entry_state(s,at('2026-11-06T23:00:00+00:00'))['active'])
        self.assertTrue(entry_state(s,at('2026-11-07T01:59:00+00:00'))['active'])
        self.assertFalse(entry_state(s,at('2026-11-07T02:00:00+00:00'))['active'])
        self.assertFalse(entry_state(s,at('2026-11-07T23:00:00+00:00'))['active'])

    def test_weekly_follows_london_summer_time(self):
        s=self.settings(windows=[{'kind':'weekly','days':[0],'start':'09:00','end':'10:00','enabled':True}])
        self.assertTrue(entry_state(s,at('2027-06-07T08:30:00+00:00'))['active'])
        self.assertFalse(entry_state(s,at('2027-06-07T09:30:00+00:00'))['active'])
        self.assertTrue(entry_state(s,at('2027-01-04T09:30:00+00:00'))['active'])

    def test_pause_overrides_manual_and_schedule(self):
        now=at('2026-11-02T11:30:00+00:00')
        s=self.settings(paused=True,armed_until=now+3600,windows=[{'kind':'once','start':now-100,'end':now+100,'enabled':True}])
        self.assertFalse(entry_state(s,now)['active'])

    def test_overlapping_windows_report_last_close(self):
        now=1000000000
        s=self.settings(armed_until=now+300,windows=[{'kind':'once','start':now+100,'end':now+900,'enabled':True}])
        self.assertEqual(entry_state(s,now)['until'],now+900)

    def test_disabled_window_is_ignored(self):
        now=1000000000
        s=self.settings(windows=[{'kind':'once','start':now-100,'end':now+100,'enabled':False}])
        self.assertFalse(entry_state(s,now)['active'])

    def test_invalid_or_ambiguous_times_are_rejected(self):
        for start in ['2027-03-28T01:30','2027-10-31T01:30']:
            with self.assertRaises(ValueError):validate_window({'kind':'once','start':start,'end':'2027-11-01T02:00'},'Europe/London',0)
        with self.assertRaises(ValueError):validate_window({'kind':'weekly','days':[],'start':'09:00','end':'10:00'},'Europe/London',0)
        with self.assertRaises(ValueError):validate_window({'kind':'weekly','days':[0],'start':'09:00','end':'09:00'},'Europe/London',0)
        with self.assertRaises(ValueError):get_zone('not/a/timezone')
