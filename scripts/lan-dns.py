#!/usr/bin/env python3
"""Maintain PhoneMic's DNS-only LAN address and ACME TXT challenges."""
import base64
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = 'lan.mic.example.com'

def api(method, suffix, data=None):
    pem = (Path.home() / '.cloudflared/cert.pem').read_text()
    credentials = json.loads(base64.b64decode(''.join(pem.splitlines()[1:-1])))
    request = Request('https://api.cloudflare.com/client/v4/zones/' + credentials['zoneID'] + '/dns_records' + suffix,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={'Authorization': 'Bearer ' + credentials['apiToken'], 'Content-Type': 'application/json'}, method=method)
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get('success'):
        raise RuntimeError('Cloudflare DNS operation failed')
    return result['result']

def main():
    action = sys.argv[1]
    if action in ('auth', 'cleanup'):
        domain = os.environ.get('CERTBOT_IDENTIFIER', os.environ.get('CERTBOT_DOMAIN'))
        if domain != HOST:
            raise RuntimeError('Unexpected certificate hostname')
        name = '_acme-challenge.' + HOST
        value = os.environ['CERTBOT_VALIDATION']
        if action == 'auth':
            api('POST', '', {'type': 'TXT', 'name': name, 'content': value, 'ttl': 60})
            time.sleep(45)
        else:
            for record in api('GET', '?' + urlencode({'type': 'TXT', 'name': name})):
                if record['content'] == value:
                    api('DELETE', '/' + record['id'])
    elif action == 'sync':
        # Only the Wi-Fi interface, never a VPN or Docker bridge.
        interfaces = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', 'wlo1']))
        addresses = [a['local'] for i in interfaces for a in i['addr_info'] if a['scope'] == 'global']
        if not addresses:
            return
        address = addresses[0]
        if not ipaddress.ip_address(address).is_private:
            raise RuntimeError('LAN DNS requires a private address')
        records = api('GET', '?' + urlencode({'type': 'A', 'name': HOST}))
        payload = {'type': 'A', 'name': HOST, 'content': address, 'proxied': False, 'ttl': 60}
        if not records:
            api('POST', '', payload)
        elif len(records) == 1:
            record = records[0]
            if record['content'] != address or record['proxied']:
                api('PUT', '/' + record['id'], payload)
        else:
            raise RuntimeError('Multiple LAN DNS records; refusing to overwrite')
        env_path = Path.home() / '.config/phonemic/web.env'
        settings = env_path.read_text()
        values = dict(line.split('=', 1) for line in settings.splitlines() if '=' in line)
        if values.get('PM_LOCAL_URL') == 'https://' + HOST + ':8445':
            changed = values.get('PM_LOCAL_BIND') != address
            if changed:
                settings = '\n'.join('PM_LOCAL_BIND=' + address if line.startswith('PM_LOCAL_BIND=') else line
                                     for line in settings.splitlines()) + '\n'
                env_path.write_text(settings)
            with socket.socket() as probe:
                probe.settimeout(1)
                listening = probe.connect_ex((address, 8445)) == 0
            if changed or not listening:
                subprocess.run(['systemctl', '--user', 'restart', 'phonemic-web'], check=True)
    else:
        raise RuntimeError('Unknown action')

if __name__ == '__main__':
    main()
