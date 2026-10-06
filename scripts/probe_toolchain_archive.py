"""Stream fixed public reviewed images without local archive files or VM mutation."""
import argparse
import json
import os
import signal
import subprocess  # nosec B404

from app.coding.toolchain_archive import inspect_archive
from scripts.probe_local_offline_toolchains import DOCKER, ENV, inspect


def scan(role):
    image = inspect(role)
    previous = signal.getsignal(signal.SIGALRM)

    def timeout(*_):
        raise TimeoutError('fixed public image export deadline exceeded')

    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(150)
    process = None
    try:
        process = subprocess.Popen(  # nosec B603
            [DOCKER, 'image', 'save', image], env=ENV, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        evidence = inspect_archive(process.stdout, role=role, image_id=image, timeout_seconds=120)
        if process.wait(timeout=10):
            raise RuntimeError('fixed public image export did not complete')
        return {**evidence, 'archive_written_locally': False, 'cloud_resource_created': False,
                'private_source_used': False, 'model_calls': 0}
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            process.stdout.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=('react', 'spring'), required=True)
    print(json.dumps(scan(parser.parse_args().role), sort_keys=True))


if __name__ == '__main__':
    main()
