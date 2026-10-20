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
import polarmeter_last_close_policy as close_policy
from polarmeter_market_source_diagnostics import summarize_market_sources, summarize_market_freshness_failure


def check_same_session_current_quote_priority():
    now = datetime(2026, 10, 20, 12, 15, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    specs = {item['key']: item for item in prepare.REQUIRED_SIGNALS}
    prices = {'sp500': 766.48, 'nasdaq100': 718.36, 'iwm': 271.28,
              'soxx': 300, 'smh': 250, 'eem': 60, 'vix': 22,
              'kospi': 7000, 'kosdaq': 1000}

    def choose(key, current_at, retained_at, *, pct=0.4, retained_pct=0.4):
        price = prices.get(key, 100)
        item = {'key': key, 'symbol': key, 'status': 'ok', 'price': price,
                'changePct': pct, 'asOf': current_at.isoformat(),
                'history': {'version': 'market-series-v1', 'status': 'ok', 'interval': '1d',
                            'sourceId': f'public-chart-delayed:{key}',
                            'dataAsOf': current_at.isoformat(),
                            'points': [{'timestamp': (current_at - timedelta(days=1)).isoformat(), 'value': price - 1},
                                       {'timestamp': current_at.isoformat(), 'value': price}]}}
        previous = {'key': key, 'value': price + 0.12, 'changePct': retained_pct,
                    'dataAsOf': retained_at.isoformat(), 'status': 'ok', 'valuePolicy': 'show',
                    'provider': 'fmp-free', 'sourceId': f'fmp-free:{key}'}
        before_item, before_previous = deepcopy(item), deepcopy(previous)
        selected = cache.choose_signal(specs[key], {'public-chart-delayed': [item]}, {key: previous})
        assert item == before_item and previous == before_previous
        return selected, item, previous

    with patch.object(cache, 'datetime', Frozen), patch.object(close_policy, 'datetime', Frozen):
        # All seven US session signals and both KR indices: timestamp rounding
        # must not turn a usable current quote into a value-only retained quote.
        for key in sorted(close_policy.US_SESSION_KEYS | close_policy.KR_SESSION_KEYS):
            observed = (datetime(2026, 10, 20, 6, 30, tzinfo=timezone.utc)
                        if key in close_policy.KR_SESSION_KEYS
                        else datetime(2026, 10, 19, 20, tzinfo=timezone.utc))
            for seconds in (-60, -1, 0, 1, 59, 60):
                selected, item, _ = choose(key, observed, observed + timedelta(seconds=seconds))
                assert selected['provider'] == 'public-chart-delayed', (key, seconds, selected['status'])
                assert selected['value'] == item['price'] and selected['status'] == 'ok'
                assert selected['dataAsOf'] == cache.normalized_as_of(item['asOf'])
                assert selected['history']['status'] == 'ok' and len(selected['history']['points']) == 2
                assert selected['freshnessRank'] == cache.candidate_freshness_rank(item)
                public = worker.sanitize_public_signal(selected, as_of=now)
                assert public['value'] == item['price'] and public['history']['status'] == 'ok'
                assert worker.snapshot_score_point(public, key, as_of=now)['close'] == item['price']
                assert public['sessionCloseReference']['completedSessionDate'] == observed.date().isoformat()
            # Larger timing differences still use the original recency order.
            selected, _, previous = choose(key, observed, observed + timedelta(seconds=61))
            assert selected['provider'] == 'fmp-free' and selected['status'] == 'stale'
            assert selected['value'] == previous['value'] and selected['history']['points'] == []
            # A live suspect quote is not silently promoted to normal quality.
            suspect_pct = cache.SANITY_RANGES[key]['suspectAbsChangePct'] + 0.1
            selected, item, _ = choose(key, observed, observed + timedelta(seconds=1), pct=suspect_pct)
            assert selected['status'] == 'suspect' and selected['value'] == item['price']
            assert selected['reliability'] == cache.signal_reliability(
                key, 'public-chart-delayed', 'suspect', selected['qualityReason'], item['asOf'])
            # Invalid current input must not remove a valid retained close.
            selected, _, previous = choose(key, observed, observed + timedelta(seconds=1), pct=None)
            if cache.SANITY_RANGES[key].get('requiresChangePct'):
                assert selected['status'] == 'stale' and selected['value'] == previous['value']

        observed = datetime(2026, 10, 19, 20, tzinfo=timezone.utc)
        # Missing previous session, reopening and freshness-rank boundaries.
        old_session = datetime(2026, 10, 16, 20, tzinfo=timezone.utc)
        selected, _, previous = choose('sp500', old_session, observed)
        assert selected['status'] == 'stale' and selected['value'] == previous['value']
        now = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
        selected, _, previous = choose('sp500', observed, observed + timedelta(seconds=1))
        assert selected['status'] == 'stale' and selected['value'] == previous['value']
        now = old_session + timedelta(hours=18, microseconds=1)
        selected, item, _ = choose('sp500', old_session, old_session + timedelta(seconds=1))
        assert selected['status'] == 'ok' and selected['value'] == item['price']
        assert selected['freshnessRank'] == 2
        # An existing stale verdict must never qualify for the close tie-break.
        now = datetime(2026, 10, 20, 12, 15, tzinfo=timezone.utc)
        for guard in ('active_market_stale_reason', 'hard_stale_reason'):
            with patch.object(cache, guard, return_value='offline_guard_marks_stale'):
                selected, _, previous = choose('sp500', observed, observed + timedelta(seconds=1))
                assert selected['status'] == 'stale' and selected['value'] == previous['value']
        # FX, rates and commodities do not inherit an equity-session exception.
        now = datetime(2026, 10, 20, 12, 15, tzinfo=timezone.utc)
        prices.update(usd_krw=1400, us10y=4.2, dxy=100, wti=75, gold=2500, kr_samsung=70000)
        for key in ('usd_krw', 'us10y', 'dxy', 'wti', 'gold'):
            selected, item, _ = choose(key, now - timedelta(hours=1), now - timedelta(minutes=30))
            assert selected['status'] == 'ok' and selected['value'] == item['price']
            assert 'sessionCloseReference' not in worker.sanitize_public_signal(selected, as_of=now)
        # Samsung's separate last-close policy must not be changed implicitly.
        selected, _, previous = choose('kr_samsung', now - timedelta(hours=1), now - timedelta(minutes=30))
        assert selected['status'] == 'stale' and selected['value'] == previous['value']


def check_seeded_last_good_public_metadata():
    now = datetime(2026, 10, 19, 1, tzinfo=timezone.utc)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    prices = {'us10y': 4.2, 'dxy': 100, 'soxx': 300, 'smh': 250,
              'kospi': 7000, 'kosdaq': 1000, 'kr_samsung': 70000}
    quotes = {}
    for spec in prepare.REQUIRED_SIGNALS:
        key = spec['key']
        observed = (datetime(2026, 10, 16, 20, tzinfo=timezone.utc) if key == 'sp500'
                    else datetime(2026, 10, 16, 6, 30, tzinfo=timezone.utc) if key == 'kr_samsung'
                    else now - timedelta(hours=2))
        quotes[key] = {'key': key, 'value': prices.get(key, 100), 'changePct': 0,
                       'dataAsOf': observed.isoformat(), 'status': 'ok', 'valuePolicy': 'show',
                       'provider': 'data-go-kr-free' if key == 'kr_samsung' else 'public-chart-delayed',
                       'freshnessRank': 999, 'dataAgeHours': -999}
    with patch.object(cache, 'datetime', Frozen), patch.object(prepare, 'datetime', Frozen), patch.object(close_policy, 'datetime', Frozen):
        with TemporaryDirectory(prefix='polarmeter-lkg-public-contract-') as folder:
            site, target = Path(folder) / 'site', Path(folder) / 'last-good.json'
            prepare.copy_site(prepare.DEFAULT_SITE, site)
            snapshot = {'status': 'partial', 'generatedAt': now.isoformat(),
                        'paidProviderEnabled': False, 'clientDirectProviderCalls': False,
                        'signals': quotes}
            snapshot_path = site / 'market-snapshot-latest.json'
            snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
            assert prepare.seed_last_known_good_from_site(site, target)
            seed = json.loads(target.read_text())['signals']
            assert set(seed) == set(quotes)
            assert all('freshnessRank' not in quote and 'dataAgeHours' not in quote for quote in seed.values())
            recovered = {spec['key']: cache.choose_signal(spec, {}, seed) for spec in prepare.REQUIRED_SIGNALS}
            public = {key: worker.sanitize_public_signal(quote, as_of=now) for key, quote in recovered.items()}
            snapshot.update(signals=public, dataQuality={'coreCoverageRatio': 1.0, 'displayMode': 'normal'},
                            news={'items': [{} for _ in range(10)], 'ttlMinutes': 30, 'nextRefreshAt': now.isoformat()},
                            temperatureHistory={'version': 'temperature-history-v1', 'retentionDays': 7,
                                                'items': [], 'dailyDelta': {'status': 'ready'}},
                            costGuardrails=worker.public_cost_guardrails())
            snapshot_path.write_text(json.dumps(snapshot), encoding='utf-8')
            manifest = worker.public_manifest(snapshot, snapshot_path.name)
            (site / 'market-snapshot-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (site / 'health.json').write_text(json.dumps(worker.public_health(manifest)), encoding='utf-8')
            # Exercise the exact final publication gate that failed in CI.
            prepare.assert_pages_contract(site, {'snapshotStatus': 'partial', 'freshnessAudit': 'passed'})
            assert freshness.audit(snapshot, {}) == []
            for key, quote in recovered.items():
                observed = quotes[key]['dataAsOf']
                assert quote['status'] == 'stale' and quote['valuePolicy'] == 'show'
                assert quote['value'] == quotes[key]['value'] and quote['dataAsOf'] == observed
                assert quote['freshnessRank'] == cache.candidate_freshness_rank({'asOf': observed})
                assert quote['dataAgeHours'] >= 0 and quote['history']['points'] == []
                assert quote['reliability']['confidencePolicy'].startswith('low_')
                assert worker.snapshot_score_point(public[key], key, as_of=now)['close'] == quote['value']
            # The gate itself must remain strict: deleting the field still fails.
            for key in ('sp500', 'kr_samsung'):
                broken = deepcopy(snapshot)
                broken['signals'][key].pop('freshnessRank')
                snapshot_path.write_text(json.dumps(broken), encoding='utf-8')
                try:
                    prepare.assert_pages_contract(site, {'snapshotStatus': 'partial', 'freshnessAudit': 'passed'})
                except AssertionError as error:
                    assert str(error) == f'public snapshot showable signal missing freshnessRank: {key}'
                else:
                    raise AssertionError('missing freshness metadata must still block publication')

        spec = next(item for item in prepare.REQUIRED_SIGNALS if item['key'] == 'kr_samsung')
        for hours, rank in ((2, 3), (18, 3), (18 + 1 / 3600, 2), (84, 2),
                            (84 + 1 / 3600, 1), (168, 1), (168 + 1 / 3600, 0)):
            quote = {**quotes['kr_samsung'], 'dataAsOf': (now - timedelta(hours=hours)).isoformat()}
            before = deepcopy(quote)
            result = cache.stale_from_last_good(spec, {'kr_samsung': quote}, 'offline_failure')
            assert result is not None and result['freshnessRank'] == rank
            assert quote == before and result['dataAsOf'] == quote['dataAsOf']
        for observed in (None, '', 'bad-date', (now + timedelta(hours=1)).isoformat()):
            quote = {**quotes['us10y'], 'dataAsOf': observed, 'fetchedAt': now.isoformat(), 'lastSuccessfulAt': now.isoformat()}
            rate_spec = next(item for item in prepare.REQUIRED_SIGNALS if item['key'] == 'us10y')
            assert cache.stale_from_last_good(rate_spec, {'us10y': quote}, 'offline_failure') is None
        index_spec = next(item for item in prepare.REQUIRED_SIGNALS if item['key'] == 'sp500')
        unobserved = {**quotes['sp500'], 'dataAsOf': None, 'fetchedAt': now.isoformat(),
                      'lastSuccessfulAt': now.isoformat()}
        result = cache.stale_from_last_good(index_spec, {'sp500': unobserved}, 'offline_failure')
        assert result is not None and result['freshnessRank'] == 0 and result['dataAsOf'] is None
        assert worker.snapshot_score_point(result, 'sp500', as_of=now)['close'] is None


def check_freshness_failure_details():
    secret = 'SENSITIVE_TEST_CREDENTIAL'
    base = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
    cases = (
        ('us10y', 'provider_recency_gap', base, 25),
        ('sp500', 'active_market_stale', base.replace(hour=16), 4),
        ('kospi', 'public_candidate_rank', base, 1),
    )
    for key, expected_reason, now, age in cases:
        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        signals = {name: {'key': name, 'status': 'ok', 'valuePolicy': 'show', 'value': 1,
                          'changePct': 0, 'dataAsOf': now.isoformat()}
                   for name in freshness.CRITICAL_SIGNAL_REQUIRED_FIELDS}
        signals[key].update(value=4.2 if key == 'us10y' else 7000, provider='twelvedata-free',
                            freshnessRank=1, dataAsOf=(now - timedelta(hours=age)).isoformat())
        snapshot = {'generatedAt': now.isoformat(), 'signals': signals}
        probe = {'providers': [{'provider': 'public-chart-delayed', 'items': [
            {'key': key, 'status': 'ok', 'price': signals[key]['value'], 'changePct': 0,
             'asOf': (now - timedelta(minutes=30)).isoformat()}]}]} if key != 'sp500' else {}
        # Each selected quote looks clean to the original source summary.
        assert summarize_market_sources(snapshot, probe, now=now)['signals'] == []
        with patch.object(freshness.cache, 'datetime', Frozen):
            errors = freshness.audit(snapshot, probe)
        assert any(error.startswith(key + ': ') for error in errors)
        # Raw provider details stay in captured child output only.
        child_output = json.dumps({'ok': False, 'errors': errors + [
            f'{key}: private body https://private.invalid/?apikey={secret}']})
        failure = subprocess.CalledProcessError(1, ['private'], output=child_output, stderr=secret)
        output = io.StringIO()
        with patch.object(worker.subprocess, 'run', side_effect=failure) as child, contextlib.redirect_stderr(output):
            try:
                worker.run_freshness_audit(Path('snapshot.json'), Path('probe.json'))
            except RuntimeError as error:
                assert error.__suppress_context__
                assert str(error) == 'data freshness audit failed; see marketSourceDiagnostics'
            else:
                raise AssertionError('audit rejection must still stop publication')
        assert child.call_args.args[0][-1] == '--json'
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        diagnostics = records[-1]['marketSourceDiagnostics']
        row = next(item for item in diagnostics['signals'] if item['key'] == key)
        assert expected_reason in row['reasons'], diagnostics
        for private in (secret, 'private body', 'https://', 'apikey', now.isoformat()):
            assert private not in output.getvalue()

    for malformed in (None, b'private', secret, '[]', '{"ok": true, "errors": []}',
                      '{"ok": false, "errors": "private"}', 'x' * 262145):
        assert summarize_market_freshness_failure(malformed) == {
            'reportStatus': 'freshness_audit_unreadable', 'signals': []}
    bounded = summarize_market_freshness_failure(json.dumps({'ok': False, 'errors': [
        secret + ': private', [], 'us10y: ' + secret, 'us10y: ' + 'x' * 4096] + ['us10y: private'] * 200}))
    assert bounded == {'reportStatus': 'freshness_audit_failed',
                       'signals': [{'key': 'us10y', 'reasons': ['freshness_audit_failure']}],
                       'hasUnclassifiedFailure': True}
    assert secret not in json.dumps(bounded)
    output = io.StringIO()
    with patch.object(worker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}', '')), contextlib.redirect_stderr(output):
        worker.run_freshness_audit(Path('snapshot.json'), Path('probe.json'))
    assert output.getvalue() == ''

    # Exercise the real CLI/JSON boundary, not only a mocked child response.
    now = datetime.now(timezone.utc)
    signals = {name: {'status': 'ok', 'value': 1, 'changePct': 0, 'dataAsOf': now.isoformat()}
               for name in freshness.CRITICAL_SIGNAL_REQUIRED_FIELDS}
    signals['us10y'].update(value=4.2, provider=secret,
                            dataAsOf=(now - timedelta(hours=25)).isoformat())
    with TemporaryDirectory(prefix='polarmeter-audit-json-') as folder:
        snapshot_path, probe_path = Path(folder) / 'snapshot.json', Path(folder) / 'probe.json'
        snapshot_path.write_text(json.dumps({'generatedAt': now.isoformat(), 'signals': signals}), encoding='utf-8')
        probe_path.write_text(json.dumps({'providers': [{'provider': 'public-chart-delayed', 'items': [
            {'key': 'us10y', 'status': 'ok', 'price': 4.2, 'changePct': 0,
             'asOf': (now - timedelta(minutes=30)).isoformat()}]}]}), encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            try:
                worker.run_freshness_audit(snapshot_path, probe_path)
            except RuntimeError as error:
                assert error.__suppress_context__
            else:
                raise AssertionError('real CLI must still reject the older selected quote')
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        assert records[-1]['marketSourceDiagnostics']['signals'] == [
            {'key': 'us10y', 'reasons': ['provider_recency_gap']}]
        assert secret not in output.getvalue() and str(snapshot_path) not in output.getvalue()


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
    diagnostic, fallback = [json.loads(line) for line in output.getvalue().splitlines()]
    assert diagnostic['workerFailure'] == 'market_freshness_audit_failed'
    assert diagnostic['failureStage'] == 'market_freshness' and diagnostic['errorType'] == 'unknown'
    assert fallback['marketSourceDiagnostics']['reportStatus'] == 'freshness_audit_unreadable'
    assert secret not in output.getvalue()

def main():
    check_same_session_current_quote_priority()
    check_seeded_last_good_public_metadata()
    check_freshness_failure_details()
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
