"""Error diagnostics must remain useful without exposing provider secrets."""
import contextlib
import io
import json
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import polarmeter_free_cache_worker as worker
import polarmeter_cache_snapshot as cache
import polarmeter_github_pages_prepare as prepare
import polarmeter_data_freshness_audit as freshness
from polarmeter_market_source_diagnostics import summarize_market_sources


def check_market_recovery():
    now = datetime.now(timezone.utc)
    spec = next(item for item in prepare.REQUIRED_SIGNALS if item['key'] == 'us10y')
    quote = {'key': 'us10y', 'value': 4.2, 'changePct': -0.1, 'status': 'ok',
             'valuePolicy': 'show', 'provider': 'public-chart-delayed',
             'dataAsOf': (now - timedelta(hours=2)).isoformat()}

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    with patch.object(cache, 'datetime', Frozen), patch.object(prepare, 'datetime', Frozen):
        recovered = cache.choose_signal(spec, {'public-chart-delayed': [
            {'key': 'us10y', 'status': 'error', 'reason': 'HTTP Error 429 private body'}]}, {'us10y': quote})
        assert recovered['status'] == 'stale' and recovered['valuePolicy'] == 'show'
        assert recovered['value'] == 4.2 and recovered['dataAsOf'] == quote['dataAsOf']
        assert recovered['reliability']['confidencePolicy'].startswith('low_')
        assert recovered['history']['points'] == []
        signals = {key: {'status': 'ok', 'value': 1, 'changePct': 0, 'dataAsOf': now.isoformat()}
                   for key in freshness.CRITICAL_SIGNAL_REQUIRED_FIELDS}
        signals['us10y'] = recovered
        assert freshness.audit({'generatedAt': now.isoformat(), 'signals': signals}, {}) == []
        for changes in ({'value': 'bad'}, {'value': True}, {'value': float('nan')}, {'value': float('inf')},
                        {'value': 9}, {'changePct': None}, {'changePct': False}, {'changePct': float('nan')},
                        {'dataAsOf': None}, {'dataAsOf': (now + timedelta(hours=1)).isoformat()},
                        {'dataAsOf': (now - timedelta(hours=96, seconds=1)).isoformat()},
                        {'status': 'invalid'}, {'status': []}, {'valuePolicy': 'hide'}, {'key': 'gold'}):
            assert cache.stale_from_last_good(spec, {'us10y': {**quote, **changes}}, 'test') is None, changes
        boundary = {**quote, 'dataAsOf': (now - timedelta(hours=96)).isoformat()}
        assert cache.stale_from_last_good(spec, {'us10y': boundary}, 'test') is not None
        bad = cache.choose_signal(spec, {'public-chart-delayed': [{'key': 'us10y', 'status': 'error'}]}, {})
        signals['us10y'] = bad
        assert freshness.audit({'generatedAt': now.isoformat(), 'signals': signals}, {})
        for changes in ({'price': float('nan')}, {'price': True}, {'price': 10 ** 400}, {'changePct': float('inf')},
                        {'asOf': None}, {'asOf': (now + timedelta(hours=1)).isoformat()}):
            item = {'key': 'us10y', 'status': 'ok', 'price': 4.2, 'changePct': -0.1, 'asOf': now.isoformat(), **changes}
            result = cache.choose_signal(spec, {'public-chart-delayed': [item]}, {'us10y': quote})
            assert result['status'] == 'stale' and result['dataAsOf'] == quote['dataAsOf']

        with TemporaryDirectory(prefix='polarmeter-market-recovery-') as folder:
            site, target = Path(folder) / 'site', Path(folder) / 'last-good.json'
            site.mkdir()
            path = site / 'market-snapshot-latest.json'
            payload = {'generatedAt': now.isoformat(), 'status': 'partial', 'paidProviderEnabled': False,
                       'clientDirectProviderCalls': False, 'signals': {'us10y': quote}}
            polluted = deepcopy(payload)
            polluted['signals']['us10y'].update(qualityReason='SENSITIVE_TEST_CREDENTIAL',
                                                history={'private': 'SENSITIVE_TEST_CREDENTIAL'},
                                                sourceId='https://private.invalid/?apikey=SENSITIVE_TEST_CREDENTIAL')
            path.write_text(json.dumps(polluted), encoding='utf-8')
            assert prepare.seed_last_known_good_from_site(site, target)
            assert 'SENSITIVE_TEST_CREDENTIAL' not in target.read_text()
            seeded = json.loads(target.read_text())['signals']['us10y']
            assert seeded['dataAsOf'] == quote['dataAsOf'] and seeded['value'] == 4.2
            newer = deepcopy(payload)
            newer['signals']['us10y']['dataAsOf'] = (now - timedelta(minutes=30)).isoformat()
            path.write_text(json.dumps(newer), encoding='utf-8')
            assert prepare.seed_last_known_good_from_site(site, target)
            path.write_text(json.dumps(payload), encoding='utf-8')
            assert prepare.seed_last_known_good_from_site(site, target)
            assert json.loads(target.read_text())['signals']['us10y']['dataAsOf'] == newer['signals']['us10y']['dataAsOf']
            for changes in ({'valuePolicy': 'hide'}, {'provider': []},
                            {'provider': 'bok-ecos-free'}, {'provider': 'data-go-kr-free'},
                            {'provider': 'data-go-kr-index-free'}, {'status': []}, {'value': 'bad'},
                            {'dataAsOf': (now + timedelta(hours=1)).isoformat()},
                            {'dataAsOf': (now - timedelta(hours=97)).isoformat()}, {'key': 'gold'}):
                target.unlink()
                invalid = deepcopy(payload)
                invalid['signals']['us10y'].update(changes)
                path.write_text(json.dumps(invalid), encoding='utf-8')
                assert not prepare.seed_last_known_good_from_site(site, target)
                assert json.loads(target.read_text())['signals'] == {}
            for missing, alias in (('value', 'price'), ('dataAsOf', 'asOf')):
                incomplete = deepcopy(payload)
                signal = incomplete['signals']['us10y']
                signal[alias] = signal.pop(missing)
                target.unlink()
                path.write_text(json.dumps(incomplete), encoding='utf-8')
                assert not prepare.seed_last_known_good_from_site(site, target)
                assert json.loads(target.read_text())['signals'] == {}
            for provider in ('public-chart-delayed', 'twelvedata-free', 'fmp-free'):
                compatible = deepcopy(payload)
                compatible['signals']['us10y']['provider'] = provider
                path.write_text(json.dumps(compatible), encoding='utf-8')
                target.unlink()
                assert prepare.seed_last_known_good_from_site(site, target)
                assert json.loads(target.read_text())['signals']['us10y']['provider'] == provider
            for changes in ({'status': []}, {'status': 'invalid'}, {'paidProviderEnabled': True},
                            {'generatedAt': (now + timedelta(hours=1)).isoformat()}):
                path.write_text(json.dumps({**payload, **changes}), encoding='utf-8')
                assert not prepare.seed_last_known_good_from_site(site, target)
            path.write_text('[]', encoding='utf-8')
            assert not prepare.seed_last_known_good_from_site(site, target)

    secret = 'SENSITIVE_TEST_CREDENTIAL'
    hostile = {'signals': {'us10y': {'status': 'invalid', 'provider': secret, 'qualityReason': 'private body ' + secret,
                                    'fallbackChain': [{'provider': 'public-chart-delayed', 'quality': 'invalid',
                                                       'reason': 'price_above_range>8.0 ' + secret}]}, secret: quote}}
    probe = {'providers': [{'provider': secret, 'items': [{'key': 'us10y', 'status': 'error',
                                                         'reason': 'HTTP Error 429 https://private.invalid/?apikey=' + secret}]}]}
    logged = json.dumps(summarize_market_sources(hostile, probe, now=now))
    assert 'us10y' in logged and 'price_above_range' in logged and 'http_429' in logged
    for private in (secret, 'private body', 'https://', 'apikey', '8.0'):
        assert private not in logged
    assert summarize_market_sources([], probe)['reportStatus'] == 'snapshot_unreadable'
    failure = subprocess.CalledProcessError(1, ['private'], output=secret, stderr=f'{secret}Error: https://private.invalid/?apikey={secret}')
    output = io.StringIO()
    with patch.object(worker.subprocess, 'run', side_effect=failure), contextlib.redirect_stderr(output):
        try:
            worker.run_freshness_audit(Path('snapshot.json'), Path('probe.json'))
        except RuntimeError as error:
            assert secret not in str(error) and 'marketSourceDiagnostics' in str(error)
            assert error.__suppress_context__
        else:
            raise AssertionError('audit must still stop publication')
    diagnostic = json.loads(output.getvalue())
    assert diagnostic['workerFailure'] == 'market_freshness_audit_failed'
    assert diagnostic['failureStage'] == 'market_freshness' and diagnostic['errorType'] == 'unknown'
    assert secret not in output.getvalue()

def main():
    check_market_recovery()
    secret = 'SENSITIVE_TEST_CREDENTIAL'
    failure = subprocess.CalledProcessError(1, ['python', 'polarmeter_cache_snapshot.py'], stderr=f'https://provider.invalid/?apikey={secret}\nValueError: official FOMC statement target range was not found\n')
    output = io.StringIO()
    with patch.object(worker.subprocess, 'run', side_effect=failure), contextlib.redirect_stderr(output):
        try:
            worker.run(['python', 'polarmeter_cache_snapshot.py'])
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError('failure must propagate')
    logged = output.getvalue()
    assert 'official_fomc_target_range_unrecognized' in logged
    assert 'ValueError' in logged
    assert secret not in logged and 'apikey' not in logged and 'https://' not in logged
    cases = [
        ('BLS API request failed: private body', '_bls_series_points', 'official_bls_request_failed', 'official_bls_response'),
        ('BLS series CUUR0000SA0 is missing period 2026-09', '_required_bls_value', 'official_bls_period_missing', 'official_bls_period'),
        ('macro release label has no reference month: private label', '_reference_period', 'official_macro_reference_month_missing', 'official_macro_reference_period'),
        ('macro release label has invalid reference month: private label', '_reference_period', 'official_macro_reference_month_invalid', 'official_macro_reference_period'),
        ('Invalid value NaN', 'normalized_as_of', 'child_process_failed', 'provider_timestamp'),
        ('private body', 'untrusted_function', 'child_process_failed', 'unknown'),
    ]
    for message, frame, reason, stage in cases:
        private_path = '/private/SENSITIVE_TEST_CREDENTIAL/provider.py'
        stderr = f'  File "{private_path}", line 123, in {frame}\nValueError: {message} https://provider.invalid/?apikey={secret}\n'
        failure = subprocess.CalledProcessError(1, ['python', 'polarmeter_cache_snapshot.py'], stderr=stderr)
        output = io.StringIO()
        with patch.object(worker.subprocess, 'run', side_effect=failure), contextlib.redirect_stderr(output):
            try:
                worker.run(['python', 'polarmeter_cache_snapshot.py'])
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError('diagnostics must not turn worker failure into success')
        logged = output.getvalue()
        diagnostic = json.loads(logged)
        assert diagnostic['workerFailure'] == reason
        assert diagnostic['failureStage'] == stage
        assert diagnostic['exitCode'] == 1 and diagnostic['errorType'] == 'ValueError'
        for private in (secret, private_path, 'apikey', 'https://', 'private body', 'private label', 'CUUR0000SA0', 'untrusted_function'):
            assert private not in logged
    print('PASS worker failure diagnostics and secret exclusion')

if __name__ == '__main__':
    main()
