"""Prepare runtime/config only. Does not sign in, send messages or install a plugin registry entry."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

def main():
    parser=argparse.ArgumentParser(description='Prepare CustomSiteLogin runtime inside MoviePilot Linux container')
    parser.add_argument('--apply',action='store_true',help='Actually create missing config and copy the model')
    parser.add_argument('--install-deps',action='store_true',help='Also install pinned Python dependencies; requires --apply')
    args=parser.parse_args()
    source=Path(__file__).resolve().parent
    root=Path('/config/custom-site-login')
    if not sys.platform.startswith('linux'):
        raise SystemExit('Run this inside the MoviePilot Linux container.')
    print('Target:',root)
    print('This script does not change existing credentials, enable tasks, or submit sign-ins.')
    if not args.apply:
        print('Preview only. Use --apply; optionally add --install-deps.')
        return
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    private=root/'sources.json'
    if not private.exists():
        private.write_text((source/'config/sources.example.json').read_text(encoding='utf-8'),encoding='utf-8')
        private.chmod(0o600)
        print('Created blank sources.json. Replace UID 0 with your own account IDs.')
    else:
        print('Kept existing sources.json unchanged.')
    data=(source/'runtime/models/ppocrv5-mobile.onnx').read_bytes()
    expected='5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5'
    if hashlib.sha256(data).hexdigest()!=expected:
        raise SystemExit('Model checksum mismatch; stopped.')
    model=root/'models/ppocrv5-mobile.onnx'
    model.parent.mkdir(exist_ok=True)
    if model.exists() and hashlib.sha256(model.read_bytes()).hexdigest()!=expected:
        raise SystemExit('An existing different model was found; no overwrite performed.')
    if not model.exists():
        model.write_bytes(data)
    if args.install_deps:
        subprocess.run([sys.executable,'-m','pip','install','--target',str(root/'checkin-deps-v1'),
                        '-r',str(source/'requirements-runtime.txt')],check=True)
    print('Prepared. Configure CookieCloud, bind UIDs, reload the plugin, then enable only the sites you need.')

if __name__=='__main__':
    main()
