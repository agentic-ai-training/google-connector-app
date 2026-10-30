"""Exporter lifecycle simulations; they do not prove actual Docker archive contents."""
import io

import pytest

from scripts import probe_toolchain_archive as probe


@pytest.mark.parametrize('failure', ['inspection', 'export', None])
def test_exporter_cleans_owned_process_and_watchdog_for_all_terminal_paths(monkeypatch, failure):
    image = 'sha256:' + 'a' * 64
    calls, alarms, killed = [], [], []

    class Child:
        pid = 12345
        stdout = io.BytesIO(b'fixed-public')
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            self.returncode = 1 if failure == 'export' else 0
            return self.returncode

    child = Child()
    monkeypatch.setattr(probe, 'inspect', lambda role: image)
    monkeypatch.setattr(probe.signal, 'getsignal', lambda _: 'previous')
    monkeypatch.setattr(probe.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(probe.signal, 'alarm', alarms.append)
    monkeypatch.setattr(probe.os, 'killpg', lambda pid, sig: killed.append(pid))

    def spawn(args, **kwargs):
        calls.append((args, kwargs))
        return child

    def inspect_archive(*args, **kwargs):
        if failure == 'inspection':
            raise ValueError('fixed synthetic invalid archive')
        return {'image_loaded': False, 'image_approved': False}

    monkeypatch.setattr(probe.subprocess, 'Popen', spawn)
    monkeypatch.setattr(probe, 'inspect_archive', inspect_archive)
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            probe.scan('react')
    else:
        assert probe.scan('react')['archive_written_locally'] is False
    assert calls[0][0][-3:] == ['image', 'save', image]
    assert calls[0][1]['env'] == probe.ENV and calls[0][1]['start_new_session'] is True
    assert alarms == [150, 0]
    assert bool(killed) is (failure == 'inspection')
    assert child.stdout.closed
