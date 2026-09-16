"""Error diagnostics must remain useful without exposing provider secrets."""
import contextlib
import io
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
    print('PASS worker failure diagnostics and secret exclusion')

if __name__ == '__main__':
    main()
