"""Carry only the sandbox-provisioned loopback model proxy through adapters."""
import re

PROXY_MARKER = 'INFERMATRIX_MODEL_PROXY'


def proxy_environment(environ):
    if environ.get(PROXY_MARKER) != '1':
        return {}
    proxy = environ.get('HTTPS_PROXY', '')
    match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})', proxy)
    if (not match or int(match[1]) > 65535 or environ.get('HTTP_PROXY') != proxy):
        raise ValueError('invalid sandbox model proxy environment')
    return {PROXY_MARKER: '1', 'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy}


def bind_proxy(environ, host, port):
    selected = {PROXY_MARKER: '1', 'HTTP_PROXY': f'http://{host}:{port}', 'HTTPS_PROXY': f'http://{host}:{port}'}
    environ.update(proxy_environment(selected))
