"""Offline tests using synthetic HTML, not captured/live PoorStock responses."""
from __future__ import annotations
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poorstock_monitor as m

DECK = 'https://mopsov.twse.com.tw/nas/STR/123420260914M001.pdf'
OTHER = 'https://mopsov.twse.com.tw/nas/STR/123420260101M002.pptx'
LABEL = '\u516c\u958b\u8cc7\u8a0a\u89c0\u6e2c\u7ad9' + m.PRESENTATION + '\u9023\u7d50'
NAME = '\u6e2c\u8a66\u516c\u53f8'

def block(date='2026/09/14', url=DECK):
    link = f'<a href="{url}">{LABEL}</a>' if url else '\u516c\u53f8\u672a\u63d0\u4f9b'
    return (f'<dl><dt>{m.DATE_LABEL}</dt><dd>{date} 14:00</dd>'
            f'<dt>PDF{m.PRESENTATION}</dt><dd>{link}</dd></dl>')

def company(current=DECK, history=None, code='1234'):
    s = f'<html><h1>{NAME}\uff08{code}\uff09{m.CALL}</h1><h2>{m.MOPS_SECTION}</h2>'
    s += block(url=current)
    s += f'<h2>{m.AI_SECTION}</h2><p>{m.DATE_LABEL}</p><p>2099/01/01</p>'
    s += f'<a href="https://example.com/fake.pdf">{m.PRESENTATION}</a>'
    if history is not None:
        s += f'<h2>{m.HISTORY_SECTION}</h2>' + block('2025/12/01', history)
    s += f'<h2>{m.CATEGORY_SECTION}</h2><a href="/tag/test">Category</a></html>'
    return s

CAL = (f'<html><h1>{m.CALL}</h1><a href="/earningcall/1234">{NAME}\uff081234\uff09</a>'
       '<a href="/tag/test">Category</a></html>')
CAT = '<a href="/stock/1234">1234 Test company</a><a href="/stock/5678">5678 Another</a>'

class ParseTests(unittest.TestCase):
    def test_scope(self):
        self.assertTrue(m.allowed_url(m.CALENDAR))
        for url in [DECK,'https://poorstock.com.evil.test/x','https://poorstock.com@evil.test',
                    'http://poorstock.com/x','https://poorstock.com:444/x']:
            self.assertFalse(m.allowed_url(url))
    def test_canonical(self):
        self.assertEqual(m.canonical_url('/x.pdf?z=2&amp;id=7&utm_source=x#page=5'),
                         m.BASE+'/x.pdf?id=7&z=2')
    def test_reject_javascript(self):
        self.assertIsNone(m.canonical_url('javascript:alert(1)'))
    def test_dates(self):
        self.assertEqual(m.extract_date('115/9/14'), '2026-09-14')
        self.assertIsNone(m.extract_date('2026/02/31'))
    def test_file_query(self):
        self.assertEqual(m.file_kind('https://example.com/d?file=slides.PPTX'), 'pptx')
    def test_calendar(self):
        codes, tags = m.parse_calendar(CAL)
        self.assertEqual(codes, {'1234':NAME})
        self.assertEqual(tags, [m.BASE+'/tag/test'])
    def test_empty_calendar_fails(self):
        with self.assertRaises(m.LayoutError): m.parse_calendar('<html>verify you are human</html>')
    def test_categories(self):
        self.assertEqual(set(m.parse_category(CAT)), {'1234','5678'})
    def test_empty_category_fails(self):
        with self.assertRaises(m.LayoutError): m.parse_category('<html>empty</html>')
    def test_history_and_ai_exclusion(self):
        name, decks = m.parse_company(company(history=OTHER), '1234')
        self.assertEqual(name, NAME)
        self.assertEqual(len(decks), 2)
        self.assertEqual(decks[0]['meeting_dates'], ['2026-09-14'])
        self.assertEqual(decks[1]['meeting_dates'], ['2025-12-01'])
        self.assertFalse(any('fake' in d['url'] for d in decks))
    def test_reused_url(self):
        _, decks = m.parse_company(company(history=DECK), '1234')
        self.assertEqual(len(decks),1)
        self.assertEqual(decks[0]['meeting_dates'], ['2026-09-14','2025-12-01'])
    def test_unverified_resource(self):
        _, decks = m.parse_company(company(current='https://mops.twse.com.tw/mops/web/t100sb02'), '1234')
        self.assertEqual(decks[0]['file_type'], 'unverified_resource_link')
    def test_no_deck_is_valid(self):
        self.assertEqual(m.parse_company(company(current=None), '1234')[1], [])
    def test_wrong_company(self):
        with self.assertRaises(m.LayoutError): m.parse_company(company(), '4321')
    def test_table_style(self):
        body = (f'<h1>Test (1234) {m.CALL}</h1><h2>{m.MOPS_SECTION}</h2>'
                f'<table><tr><th>{m.DATE_LABEL}</th><td>2026/09/12</td></tr>'
                f'<tr><th>PDF{m.PRESENTATION}</th><td><a href="{OTHER}">Download</a></td></tr></table>')
        _, decks = m.parse_company(body, '1234')
        self.assertEqual(decks[0]['file_type'],'pptx')
        self.assertEqual(decks[0]['meeting_dates'],['2026-09-12'])

class StateTests(unittest.TestCase):
    def setUp(self):
        self.db=m.open_db(':memory:')
        m.add_companies(self.db,{'1234':NAME})
    def tearDown(self): self.db.close()
    def observe(self, body):
        name, decks = m.parse_company(body,'1234')
        m.observe(self.db,'1234',name,decks)
    def rows(self): return m.decode_rows(self.db.execute('SELECT * FROM decks'))
    def test_baseline_then_new_link_even_old_date(self):
        self.observe(company())
        self.assertEqual(self.rows()[0]['observation'],'initial_inventory')
        self.observe(company(history=OTHER))
        rows=self.rows()
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['observation'],'new_link')
        self.assertEqual(rows[1]['meeting_dates'],['2025-12-01'])
    def test_repeated_run_no_duplicate(self):
        self.observe(company());self.observe(company())
        self.assertEqual(len(self.rows()),1)
    def test_reused_date_does_not_alert(self):
        self.observe(company());self.observe(company(history=DECK))
        self.assertEqual(len(self.rows()),1)
        self.assertEqual(len(self.rows()[0]['meeting_dates']),2)
    def test_late_initial_link(self):
        self.observe(company(current=None));self.observe(company())
        self.assertEqual(self.rows()[0]['observation'],'new_link')
    def test_export_once(self):
        self.observe(company())
        with tempfile.TemporaryDirectory() as d:
            report=m.export_reports(self.db,Path(d),{'status':'completed'})
            self.assertEqual(len(report['initial_inventory']),1)
            self.assertEqual(m.export_reports(self.db,Path(d),{})['initial_inventory'],[])
            self.assertTrue((Path(d)/'catalog.html').exists())
    def test_failed_export_preserves_pending(self):
        self.observe(company())
        with tempfile.TemporaryDirectory() as d, patch.object(m,'atomic_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):m.export_reports(self.db,Path(d),{})
        self.assertEqual(self.rows()[0]['reported'],0)
    def test_html_escaping(self):
        self.observe(company())
        rows=self.rows(); rows[0]['company']='<script>alert(1)</script>'
        output=m.render_html({'new_links':rows,'initial_inventory':[]})
        self.assertIn('&lt;script&gt;',output)
        self.assertNotIn('<script>alert(1)</script>',output)
    def test_fixed_edt(self):
        cfg=dict(m.DEFAULTS)
        self.assertFalse(m.is_due(self.db,cfg,datetime(2026,12,1,14,59,tzinfo=timezone.utc)))
        self.assertTrue(m.is_due(self.db,cfg,datetime(2026,12,1,15,0,tzinfo=timezone.utc)))
    def test_new_york_winter(self):
        cfg=dict(m.DEFAULTS,timezone='America/New_York')
        self.assertFalse(m.is_due(self.db,cfg,datetime(2026,12,1,15,0,tzinfo=timezone.utc)))
        self.assertTrue(m.is_due(self.db,cfg,datetime(2026,12,1,16,0,tzinfo=timezone.utc)))
    def test_new_york_summer(self):
        cfg=dict(m.DEFAULTS,timezone='America/New_York')
        self.assertTrue(m.is_due(self.db,cfg,datetime(2026,9,14,15,0,tzinfo=timezone.utc)))
    def test_schedule_once_a_day(self):
        m.put_meta(self.db,'last_scheduled_attempt','2026-09-14')
        self.assertFalse(m.is_due(self.db,m.DEFAULTS,datetime(2026,9,14,19,0,tzinfo=timezone.utc)))
        self.assertTrue(m.is_due(self.db,m.DEFAULTS,datetime(2026,9,15,19,0,tzinfo=timezone.utc)))

class FakeFetcher:
    def __init__(self, pages):self.pages=pages;self.requests=0
    def get(self,url):
        assert m.allowed_url(url), url
        self.requests+=1
        value=self.pages[url]
        if isinstance(value,Exception):raise value
        return value
    def close(self):pass

class ScanTests(unittest.TestCase):
    def test_broad_integration_baseline_then_new(self):
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory);db=m.open_db(data/'state.sqlite3')
            cfg=dict(m.DEFAULTS)
            pages={m.CALENDAR:CAL,m.BASE+'/tag/test':CAT,
                   m.BASE+'/earningcall/1234':company(),m.BASE+'/earningcall/5678':company(code='5678',current=None)}
            a=m.scan(db,cfg,data,FakeFetcher(pages))
            self.assertEqual(a['status'],'completed')
            self.assertEqual(a['planned'],2)
            self.assertEqual(len(a['initial_inventory']),1)
            self.assertEqual(a['new_links'],[])
            pages[m.BASE+'/earningcall/1234']=company(history=OTHER)
            b=m.scan(db,cfg,data,FakeFetcher(pages))
            self.assertEqual(len(b['new_links']),1)
            self.assertEqual(b['new_links'][0]['url'],OTHER)
            c=m.scan(db,cfg,data,FakeFetcher(pages))
            self.assertEqual(c['new_links'],[])
            self.assertEqual(len(json.loads((data/'catalog.json').read_text())['initial_inventory']),1)
            db.close()
    def test_site_block_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            db=m.open_db(':memory:')
            r=m.scan(db,dict(m.DEFAULTS),Path(directory),FakeFetcher({m.CALENDAR:m.StopCrawl('HTTP 403')}))
            self.assertEqual(r['status'],'failed')
            self.assertIn('HTTP 403',r['errors'])
            self.assertEqual(r['new_links'],[])
            db.close()
    def test_disappearing_links_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            db=m.open_db(':memory:');cfg=dict(m.DEFAULTS,companies=['1234'])
            key=m.BASE+'/earningcall/1234'
            m.scan(db,cfg,Path(directory),FakeFetcher({key:company()}))
            r=m.scan(db,cfg,Path(directory),FakeFetcher({key:company(current=None)}))
            self.assertEqual(r['status'],'failed')
            self.assertTrue(any('disappeared' in e for e in r['errors']))
            db.close()

class NetworkSafetyTests(unittest.TestCase):
    def setUp(self):
        self.db=m.open_db(':memory:'); self.f=m.Fetcher(self.db,dict(m.DEFAULTS))
        self.f.delay=0
    def tearDown(self):self.f.close();self.db.close()
    def response(self,status,headers=None,body=b''):
        r=Mock();r.status_code=status;r.headers=headers or {};r.iter_content.return_value=[body]
        r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
        return r
    def test_offsite_not_fetched(self):
        with patch.object(self.f.session,'get') as call:
            with self.assertRaises(m.StopCrawl):self.f.get(DECK)
            call.assert_not_called()
    def test_offsite_redirect_blocked(self):
        r=self.response(302,{'Location':DECK})
        with patch.object(self.f,'check_robots'),patch.object(self.f.session,'get',return_value=r) as call:
            with self.assertRaises(m.StopCrawl):self.f._request(m.CALENDAR)
            self.assertEqual(call.call_count,1)
    def test_429_stops(self):
        with patch.object(self.f,'check_robots'),patch.object(self.f.session,'get',return_value=self.response(429,{'Retry-After':'3600'})):
            with self.assertRaisesRegex(m.StopCrawl,'Retry-After: 3600'):self.f._request(m.CALENDAR)
    def test_request_budget(self):
        self.f.requests=self.f.cfg['max_requests_per_run']
        with patch.object(self.f,'check_robots'),patch.object(self.f.session,'get') as call:
            with self.assertRaises(m.StopCrawl):self.f._request(m.CALENDAR)
            call.assert_not_called()
    def test_cache_304(self):
        self.db.execute('INSERT INTO http_cache VALUES (?,?,?,?)',(m.CALENDAR,b'cached','etag','today'))
        with patch.object(self.f,'_request',return_value=(304,b'',{})) as call:
            self.assertEqual(self.f.get(m.CALENDAR),b'cached')
            self.assertEqual(call.call_args.args[1]['If-None-Match'],'etag')

if __name__=='__main__':unittest.main()
