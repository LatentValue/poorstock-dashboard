"""SMTP state tests with mocked SMTP, no real email is sent."""
from __future__ import annotations
import os
from pathlib import Path
import smtplib
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poorstock_monitor as m

class EmailTests(unittest.TestCase):
    def setUp(self):
        self.db=m.open_db(':memory:')
        m.add_companies(self.db,{'1234':'Synthetic'})
        m.observe(self.db,'1234','Synthetic',[])
        m.observe(self.db,'1234','Synthetic',[{'url':'https://example.com/test.pdf',
            'file_type':'pdf','meeting_dates':['2026-09-14'],
            'poorstock_url':m.BASE+'/earningcall/1234'}])
        self.report={'status':'completed','finished_at':'2026-09-14T15:00:00Z',
                     'checked':1,'planned':1,'initial_inventory':[],'errors':[]}
        self.cfg=dict(m.DEFAULTS,email_enabled=True)
        self.env={'SMTP_HOST':'smtp.example.com','SMTP_FROM':'from@example.com','SMTP_TO':'to@example.com'}
    def tearDown(self):self.db.close()
    def delivered(self):return self.db.execute('SELECT email_sent FROM decks').fetchone()[0]
    def test_missing_settings_preserves_queue(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaises(m.MonitorError):m.send_email(self.db,self.cfg,self.report)
        self.assertEqual(self.delivered(),0)
    def test_delivery_failure_preserves_queue(self):
        context=Mock();context.__enter__=Mock(return_value=context);context.__exit__=Mock(return_value=False)
        context.send_message.side_effect=smtplib.SMTPException('mocked failure')
        with patch.dict(os.environ,self.env,clear=True),patch.object(m.smtplib,'SMTP',return_value=context):
            with self.assertRaises(smtplib.SMTPException):m.send_email(self.db,self.cfg,self.report)
        self.assertEqual(self.delivered(),0)
    def test_success_marks_queue_after_send(self):
        context=Mock();context.__enter__=Mock(return_value=context);context.__exit__=Mock(return_value=False)
        with patch.dict(os.environ,self.env,clear=True),patch.object(m.smtplib,'SMTP',return_value=context):
            m.send_email(self.db,self.cfg,self.report)
        context.starttls.assert_called_once()
        context.send_message.assert_called_once()
        self.assertEqual(self.delivered(),1)
    def test_disabled_does_not_send(self):
        with patch.object(m.smtplib,'SMTP') as constructor:
            m.send_email(self.db,dict(m.DEFAULTS),self.report)
            constructor.assert_not_called()
        self.assertEqual(self.delivered(),0)

if __name__=='__main__':unittest.main()
