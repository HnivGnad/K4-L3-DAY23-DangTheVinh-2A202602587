import itertools
import json
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from dr import failover as fo, health_checker as hc, runbook as rb


def test_recovery_resets_failure_streak_and_logs_only_transitions(tmp_path, monkeypatch):
    seq = iter([True, False, False, True, False, False, False, False, True, True])
    monkeypatch.setattr(hc, 'URL', {'a': hc.URL['a']})
    monkeypatch.setattr(hc, 'probe', lambda *args: (next(seq, True), 'mock'))
    out = tmp_path / 'health.jsonl'
    hc.run(.005, .01, 3, .07, out)
    events = [json.loads(x) for x in out.read_text().splitlines()]
    assert [x['to'] for x in events] == ['UNHEALTHY', 'HEALTHY']
    assert events[0]['consecutive_fails'] == 3


def test_confirmation_defaults_to_no(monkeypatch):
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert not rb.confirm(False, 'confirm')
    monkeypatch.setattr('builtins.input', lambda _: 'y')
    assert rb.confirm(False, 'confirm')
    assert rb.confirm(True, 'confirm')


def test_runbook_calls_failover_once_and_checks_ten_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, 'LOG', tmp_path / 'runbook.jsonl')
    monkeypatch.setattr(rb.hc, 'probe', lambda *args: (False, 'timeout'))
    monkeypatch.setattr(rb.time, 'sleep', lambda _: None)
    def response(url, **kwargs):
        return httpx.Response(200, json={'region': 'b'}, request=httpx.Request('GET', url))
    monkeypatch.setattr(rb.httpx, 'get', response)
    result = {'ok': True, 'state': {'count': 200, 'weights': True},
              'replica': {'embed_model_version': 'test'}}
    failover = Mock(return_value=result)
    monkeypatch.setattr(rb.fo, 'failover', failover)
    requests = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            requests.append(url)
            return response(url)
    monkeypatch.setattr(rb.httpx, 'Client', Client)
    assert rb.run('a', 'b', 'fs', True)['ok']
    failover.assert_called_once_with('b', 'fs', wait=60)
    assert len(requests) == 10
    events = [json.loads(x) for x in rb.LOG.read_text().splitlines()]
    assert [x['step'] for x in events] == list(range(1, 8))


def test_missing_snapshot_preserves_routing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path('edge').mkdir()
    Path('edge/active_region').write_text('a')
    monkeypatch.setattr(fo, 'LOG', tmp_path / 'failover.jsonl')
    monkeypatch.setattr(fo, 'state_of', lambda _: {'pool_state': 'warm'})
    def missing(*args):
        raise SystemExit('missing snapshot')
    monkeypatch.setattr(fo.snapshot, 'get', missing)
    assert not fo.failover('b', 'fs', .01)['ok']
    assert Path('edge/active_region').read_text() == 'a'
    assert '5_dns_cutover' not in fo.LOG.read_text()
