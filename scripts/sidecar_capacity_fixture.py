"""Internal-only synthetic scrape/remote-write sink and bounded HTTP probe.

No application data, credentials, database or public listener mapping. The sink
accepts only its ephemeral fixture Basic auth, validates Snappy/protobuf metadata,
and reports counters/booleans rather than raw samples or request headers.
"""
import argparse
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import os
import socket
import threading
import urllib.request

MAX_WIRE = 1_000_000
FORBIDDEN = {'user_id', 'session_id', 'run_id', 'email', 'file_id', 'message_id'}
METRICS = ('agent_capacity_probe', 'process_capacity_probe', 'python_capacity_probe')


def varint(raw, position):
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(raw):
            raise ValueError('truncated_protobuf')
        byte = raw[position]
        position += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, position
    raise ValueError('bounded_protobuf_integer_required')


def fields(raw):
    """Read only bounded protobuf wire fields; never execute/deserialise objects."""
    position = 0
    while position < len(raw):
        key, position = varint(raw, position)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError('protobuf_field_required')
        if wire == 2:
            length, position = varint(raw, position)
            end = position + length
            if end > len(raw):
                raise ValueError('truncated_protobuf_field')
            yield field, raw[position:end]
            position = end
        elif wire == 0:
            _, position = varint(raw, position)
        elif wire in (1, 5):
            position += 8 if wire == 1 else 4
            if position > len(raw):
                raise ValueError('truncated_fixed_protobuf_field')
        else:
            raise ValueError('unsupported_protobuf_wire_type')


def inspect_write(raw):
    if not 0 < len(raw) <= MAX_WIRE:
        raise ValueError('bounded_write_required')
    jobs, names, count = set(), set(), 0
    for field, series in fields(raw):
        if field != 1:
            continue
        count += 1
        if count > 200:
            raise ValueError('bounded_synthetic_series_required')
        labels = {}
        for number, label in fields(series):
            if number != 1:
                continue
            values = {key: value.decode('utf-8') for key, value in fields(label)}
            if set(values) != {1, 2} or values[1] in labels or len(label) > 2048:
                raise ValueError('valid_unique_bounded_labels_required')
            labels[values[1]] = values[2]
        name = labels.get('__name__', '')
        if (not name.startswith(('agent_', 'process_', 'python_')) or FORBIDDEN & labels.keys()
                or any('SYNTHETIC_PRIVATE_' in value for value in labels.values())):
            raise ValueError('metric_filter_or_privacy_failure')
        if labels.get('application') != 'google-connector-app':
            raise ValueError('expected_external_label_required')
        if name in METRICS:
            names.add(name)
            jobs.add(labels.get('job', ''))
    if not count:
        raise ValueError('nonempty_write_required')
    return names, jobs, count


def check_embeddings(value, expected):
    vectors = value.get('embeddings')
    if (value.get('model') != 'nomic-embed-text' or not isinstance(vectors, list) or len(vectors) != expected
            or any(not isinstance(vector, list) or len(vector) != 768
                or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in vector)
                or sum(x*x for x in vector) < 0.01 for vector in vectors)):
        raise ValueError('finite_nonempty_768_dimensional_embeddings_required')
    return {'count': len(vectors), 'dimensions': 768,
            'prompt_eval_count': value.get('prompt_eval_count')}


class Sink:
    def __init__(self, username, password):
        self.authorization = 'Basic '+base64.b64encode((username+':'+password).encode()).decode()
        self.lock = threading.Lock()
        self.names, self.jobs, self.scrapes = set(), set(), set()
        self.writes, self.series, self.errors = 0, 0, 0

    def snapshot(self):
        with self.lock:
            return {'writes': self.writes, 'series': self.series, 'validation_errors': self.errors,
                    'metrics': sorted(self.names), 'jobs': sorted(self.jobs), 'scrapes': sorted(self.scrapes)}


def handler_for(sink):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Never write headers, body, password or URLs into logs.

        def response(self, status, body, kind='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == '/proof':
                self.response(200, json.dumps(sink.snapshot()).encode())
            elif self.path in {'/metrics', '/monitoring/metrics'}:
                with sink.lock:
                    sink.scrapes.add(self.path)
                private = ','.join(key+'="SYNTHETIC_PRIVATE_'+key+'"' for key in sorted(FORBIDDEN))
                lines = [name+'{'+private+'} 1' for name in METRICS]
                lines.append('forbidden_capacity_probe{'+private+'} 1')
                self.response(200, ('\n'.join(lines)+'\n').encode(), 'text/plain; version=0.0.4')
            else:
                self.response(404, b'{}')

        def do_POST(self):
            if self.path != '/write' or not hmac.compare_digest(self.headers.get('Authorization', ''), sink.authorization):
                self.response(403, b'{}')
                return
            try:
                if (self.headers.get('Content-Encoding') != 'snappy'
                        or self.headers.get('X-Prometheus-Remote-Write-Version') != '0.1.0'):
                    raise ValueError('remote_write_v1_required')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_WIRE:
                    raise ValueError('bounded_wire_body_required')
                compressed = self.rfile.read(length)
                if len(compressed) != length:
                    raise ValueError('complete_wire_body_required')
                import cramjam
                if not 0 < cramjam.snappy.decompress_raw_len(compressed) <= MAX_WIRE:
                    raise ValueError('bounded_decompressed_body_required')
                raw = bytes(cramjam.snappy.decompress_raw(compressed))
                names, jobs, count = inspect_write(raw)
                with sink.lock:
                    sink.names.update(names)
                    sink.jobs.update(jobs)
                    sink.writes += 1
                    sink.series += count
            except (ValueError, TypeError, OverflowError, RuntimeError):
                with sink.lock:
                    sink.errors += 1
                self.response(400, b'{}')
                return
            self.response(204, b'')
    return Handler


def probe(url, payload, timeout):
    # This CLI runs ONLY inside the disposable internal network with no secrets.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_WIRE+1)
    if len(data) > MAX_WIRE:
        raise ValueError('bounded_probe_response_required')
    if payload is not None and url.endswith('/api/embed'):
        return check_embeddings(json.loads(data), len(payload['input']))
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['serve', 'probe'])
    parser.add_argument('--url')
    parser.add_argument('--payload')
    parser.add_argument('--timeout', type=int, default=5)
    args = parser.parse_args()
    if args.mode == 'serve':
        sink = Sink(os.environ['SYNTHETIC_METRICS_USERNAME'], os.environ['SYNTHETIC_METRICS_PASSWORD'])
        address = socket.gethostbyname(socket.gethostname())
        if not ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback:
            raise ValueError('private_disposable_container_address_required')
        server = ThreadingHTTPServer((address, 8080), handler_for(sink))
        server.daemon_threads = True
        server.serve_forever()
    else:
        print(json.dumps(probe(args.url, json.loads(args.payload) if args.payload else None, args.timeout)))


if __name__ == '__main__':
    main()
