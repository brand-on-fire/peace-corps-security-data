"""Lossless storage for retained, non-display weather evidence.

Callers write only inside a candidate transaction. Public country archives keep
their existing JSON contract. Every accepted gzip is complete and bounded.
"""
import gzip
import json
import os
from pathlib import Path
import re
import tempfile
import zlib

MAX_BYTES = 25_000_000
NAME = re.compile(r'[A-Z]{2}(?:-[0-9]{3})?\.json')


def compression_enabled(configuration):
    policy = configuration.get('policy', {})
    if not isinstance(policy, dict) or policy.get('retainedWeatherStorage', 'json-v1') not in ('json-v1', 'gzip-v1'):
        raise ValueError('Unknown retained-weather storage policy')
    return policy.get('retainedWeatherStorage') == 'gzip-v1'


def decode_gzip(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('Compressed archive exceeds 25MB; complete file rejected')
    decoder = zlib.decompressobj(31)
    try:
        decoded = decoder.decompress(raw, MAX_BYTES + 1)
    except zlib.error as error:
        raise ValueError('Invalid compressed archive; complete file rejected') from error
    if len(decoded) > MAX_BYTES or decoder.unconsumed_tail:
        raise ValueError('Decoded archive exceeds 25MB; complete file rejected')
    if not decoder.eof or decoder.unused_data:
        raise ValueError('Incomplete or concatenated compressed archive; complete file rejected')
    json.loads(decoded)
    return decoded


def _paths(path):
    path = Path(path)
    if path.parent.name != 'retained-weather' or not NAME.fullmatch(path.name):
        raise ValueError('Storage path is outside the retained-weather archive')
    packed = path.with_suffix('.json.gz')
    if path.parent.is_symlink() or path.is_symlink() or packed.is_symlink():
        raise ValueError('Archive storage symlinks are not permitted')
    return path, packed


def _read(path):
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Archive file exceeds 25MB; complete file rejected')
    return raw


def read_raw(path):
    plain, packed = _paths(path)
    if plain.exists() and packed.exists():
        raise ValueError('Ambiguous duplicate archive representations')
    if plain.exists():
        raw = _read(plain)
        json.loads(raw)
        return raw
    if packed.exists():
        return decode_gzip(_read(packed))
    return None


def load(path, default=None):
    raw = read_raw(path)
    return default if raw is None else json.loads(raw)


def save(path, raw, *, compressed):
    """Atomically replace one representation; retain exactly the supplied bytes.

    Removing its superseded encoding is safe only inside the caller's staged
    archive. The complete publication still needs its normal preservation gate.
    """
    if type(compressed) is not bool or not isinstance(raw, bytes):
        raise ValueError('Archive encoding requires bytes and an explicit boolean')
    plain, packed = _paths(path)
    if len(raw) > MAX_BYTES:
        raise ValueError('Decoded archive exceeds 25MB; complete file rejected')
    json.loads(raw)
    payload = gzip.compress(raw, compresslevel=6, mtime=0) if compressed else raw
    if compressed and decode_gzip(payload) != raw:
        raise ValueError('Compressed archive round trip changed original bytes')
    target, previous = (packed, plain) if compressed else (plain, packed)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or _read(target) != payload:
        fd, temporary = tempfile.mkstemp(prefix='.' + target.name + '.', suffix='.tmp', dir=target.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
            if _read(temporary) != payload:
                raise ValueError('Archive write did not retain complete bytes')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    previous.unlink(missing_ok=True)


def compress_legacy_files(directory):
    """Keep unreferenced older parts too, changing only their encoding."""
    for path in sorted(Path(directory).glob('*.json')):
        raw = read_raw(path)
        save(path, raw, compressed=True)
