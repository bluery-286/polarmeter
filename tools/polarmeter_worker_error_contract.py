"""Error diagnostics must remain useful without exposing provider secrets."""
import contextlib
import io
import json
import subprocess
from unittest.mock import patch
import polarmeter_free_cache_worker as worker

def main():
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
