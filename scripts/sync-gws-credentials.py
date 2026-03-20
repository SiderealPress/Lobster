#!/usr/bin/env python3
"""
Sync gws credentials.enc -> credentials.json.
gws auth login writes new tokens to credentials.enc (AES-GCM encrypted)
but doesn't update credentials.json. This script keeps them in sync.
"""
import json, base64, os, sys
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

GWS_DIR = os.path.expanduser('~/.config/gws')
KEY_FILE = os.path.join(GWS_DIR, '.encryption_key')
ENC_FILE = os.path.join(GWS_DIR, 'credentials.enc')
PLAIN_FILE = os.path.join(GWS_DIR, 'credentials.json')

def decrypt_credentials():
    with open(KEY_FILE, 'rb') as f:
        key_b64 = f.read().strip()
    with open(ENC_FILE, 'rb') as f:
        enc_data = f.read()
    key = base64.b64decode(key_b64.rstrip(b'=') + b'==')
    nonce = enc_data[:12]
    ciphertext = enc_data[12:]
    aesgcm = AESGCM(key)
    return json.loads(aesgcm.decrypt(nonce, ciphertext, None))

def main():
    if not os.path.exists(ENC_FILE):
        print("No credentials.enc found, skipping")
        return

    enc_creds = decrypt_credentials()

    if not os.path.exists(PLAIN_FILE):
        print("No credentials.json found, skipping")
        return

    with open(PLAIN_FILE) as f:
        plain_creds = json.load(f)

    # Only update if refresh_token differs
    if enc_creds.get('refresh_token') == plain_creds.get('refresh_token'):
        print("credentials.json already up to date")
        return

    plain_creds['refresh_token'] = enc_creds['refresh_token']
    # Remove stale access_token so gws fetches a fresh one
    plain_creds.pop('access_token', None)

    with open(PLAIN_FILE, 'w') as f:
        json.dump(plain_creds, f, indent=2)
    print("Synced new refresh_token from credentials.enc -> credentials.json")

if __name__ == '__main__':
    main()
