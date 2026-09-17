"""Local development signing of the fixed sample/test fixtures; no portal calls.

Call only after the user authorizes access to local provisioning profiles and
the signing Keychain. Certificate/account metadata never appears in diagnostics.
"""
from __future__ import annotations
from datetime import datetime, timezone
import fnmatch
import hashlib
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
from .core import require

ALLOWED_APPS = frozenset({
    'io.reproloop.sample.ios', 'io.reproloop.sample.ios.replay.xctrunner',
    'io.reproloop.live.host', 'io.reproloop.live.tests.xctrunner',
})


def eligible_profile(profile, udid, bundles, identities, now=None):
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    entitlements = profile.get('Entitlements', {})
    expiry = profile.get('ExpirationDate')
    application = entitlements.get('application-identifier', '')
    prefix, separator, pattern = application.partition('.')
    teams = profile.get('TeamIdentifier', [])
    if (not separator or not pattern or not bundles or not set(bundles) <= ALLOWED_APPS
            or entitlements.get('get-task-allow') is not True
            or not isinstance(expiry, datetime) or expiry <= now
            or udid not in profile.get('ProvisionedDevices', [])
            or len(teams) != 1 or prefix not in profile.get('ApplicationIdentifierPrefix', [])
            or entitlements.get('com.apple.developer.team-identifier') != teams[0]
            or not all(fnmatch.fnmatchcase(bundle, pattern) for bundle in bundles)):
        return None
    for certificate in profile.get('DeveloperCertificates', []):
        if not isinstance(certificate, bytes):
            continue
        identity = hashlib.sha1(certificate).hexdigest().upper()
        if identity in identities:
            return identity
    return None


def sign_products(products, device):
    """Sign a newly built/copied iphoneos products directory in place."""
    products = Path(products).resolve()
    apps = sorted(products.glob('Debug-iphoneos/*.app'))
    require(bool(apps), 'No physical iPhone fixture apps to sign')
    bundles = []
    for app in apps:
        with (app / 'Info.plist').open('rb') as stream:
            bundles.append(plistlib.load(stream).get('CFBundleIdentifier'))
    require(set(bundles) <= ALLOWED_APPS, 'Signing is limited to the fixed sample/test apps')
    result = subprocess.run(['security', 'find-identity', '-v', '-p', 'codesigning'], capture_output=True, timeout=20)
    require(result.returncode == 0, 'Local signing identities unavailable')
    identities = set(re.findall(r'\b[A-F0-9]{40}\b', result.stdout.decode('utf-8', 'replace')))
    selected = None
    for directory in [Path.home() / 'Library/MobileDevice/Provisioning Profiles',
                      Path.home() / 'Library/Developer/Xcode/UserData/Provisioning Profiles']:
        for path in sorted(directory.glob('*.mobileprovision')):
            result = subprocess.run(['security', 'cms', '-D', '-i', str(path)], capture_output=True, timeout=20)
            if result.returncode:
                continue
            try:
                profile = plistlib.loads(result.stdout)
            except (ValueError, plistlib.InvalidFileException):
                continue
            identity = eligible_profile(profile, device.udid, bundles, identities)
            if identity:
                selected = path, profile, identity
                break
        if selected:
            break
    require(selected is not None, 'No valid local development profile covers this device and all fixture apps')
    profile_path, profile, identity = selected

    def sign(target, entitlements=None):
        command = ['/usr/bin/codesign', '--force', '--sign', identity, '--timestamp=none']
        if entitlements is not None:
            command += ['--entitlements', str(entitlements), '--generate-entitlement-der']
        result = subprocess.run(command + [str(target)], capture_output=True, timeout=30)
        require(result.returncode == 0, 'Local fixture code signing failed')

    with tempfile.TemporaryDirectory(prefix='repro-sign-entitlements-') as directory:
        for app, bundle in zip(apps, bundles):
            nested = [p for p in app.rglob('*') if p.suffix in {'.framework', '.dylib', '.xctest'}]
            for item in sorted(nested, key=lambda p: len(p.parts), reverse=True):
                sign(item)
            shutil.copyfile(profile_path, app / 'embedded.mobileprovision')
            prefix = profile['ApplicationIdentifierPrefix'][0]
            entitlements = {'application-identifier': prefix + '.' + bundle,
                'com.apple.developer.team-identifier': profile['TeamIdentifier'][0],
                'get-task-allow': True, 'keychain-access-groups': [prefix + '.' + bundle]}
            path = Path(directory) / 'entitlements.plist'
            path.write_bytes(plistlib.dumps(entitlements))
            path.chmod(0o600)
            sign(app, path)
            result = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
                                    capture_output=True, timeout=20)
            require(result.returncode == 0, 'Signed fixture validation failed')
    return {'signedApps': len(apps), 'method': 'existing-local-development-profile'}
