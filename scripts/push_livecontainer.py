#!/usr/bin/env python3
# Standardized push tool for LiveContainer: builds DmIOS (optional), streams it to LiveContainer
# on the connected iOS device, and resets LCPatchRevision in LCAppInfo.plist so LiveContainer
# automatically re-patches (MH_EXECUTE -> MH_DYLIB, __PAGEZERO, TweakLoader injection) on launch.
#
# Usage:
#   python3 scripts/push_livecontainer.py [--build] [--title-id 01008CF01BAAC000]
#   or run directly from repo root: ./push_livecontainer.py
#   or press F5 in Spyder (configured via the Settings block below).

import argparse
import datetime
import os
import plistlib
import shutil
import subprocess
import sys
import time

# Automatically re-exec with Python 3.13 if pymobiledevice3 is missing in the current python
try:
    import pymobiledevice3
except ImportError:
    for candidate in ['/opt/homebrew/bin/python3.13', '/opt/homebrew/opt/python@3.13/bin/python3.13', 'python3.13']:
        p = shutil.which(candidate)
        if p and p != sys.executable:
            try:
                res = subprocess.run([p, '-c', 'import pymobiledevice3'], capture_output=True)
                if res.returncode == 0:
                    os.execv(p, [p] + sys.argv)
            except Exception:
                pass
    sys.exit("Error: 'pymobiledevice3' is required. Install with: pip3 install pymobiledevice3")

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.exceptions import NoDeviceConnectedError

# ---- Settings (defaults for Spyder F5 / standalone runs) ----
TITLE_ID = ''                           # Empty = auto-detect from Games/*_build
REBUILD = False                         # Recompile DmIOS before pushing
LIVE_CONTAINER_ID = ''                  # Empty = auto-detect LiveContainer container holding the app
DEVICE_ID = ''                          # Device UDID (empty = first connected USB/Network device)
CHUNK_SIZE = 4 * 1024 * 1024            # 4 MB chunk size for high-speed transfer
RESET_PATCH_FLAG = True                 # Reset LCPatchRevision in LCAppInfo.plist


def log(msg):
    t = time.strftime('%H:%M:%S')
    print(f'[{t}] {msg}', flush=True)


def find_repo_root():
    d = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(d) == 'scripts':
        return os.path.dirname(d)
    return d


def detect_title_id(repo_root):
    games_dir = os.path.join(repo_root, 'Games')
    if not os.path.exists(games_dir):
        return None
    candidates = []
    for name in os.listdir(games_dir):
        if name.endswith('_build'):
            tid = name[:-6]
            if len(tid) == 16:
                candidates.append(tid)
    if len(candidates) == 1:
        return candidates[0]
    return candidates


def run_xcodebuild(repo_root, dd_path):
    ios_proj = os.path.join(repo_root, 'dm', 'ios')
    log(f"Building DmIOS for iOS (Release, unsigned) in {ios_proj}...")
    cmd = [
        'xcodebuild',
        '-project', 'DmIOS.xcodeproj',
        '-scheme', 'DmIOS',
        '-configuration', 'Release',
        '-destination', 'generic/platform=iOS',
        '-derivedDataPath', dd_path,
        'CODE_SIGNING_ALLOWED=NO',
        'CODE_SIGNING_REQUIRED=NO',
        'CODE_SIGN_IDENTITY=',
        'build'
    ]
    t0 = time.time()
    res = subprocess.run(cmd, cwd=ios_proj, capture_output=True, text=True)
    if res.returncode != 0:
        log("xcodebuild failed! Showing error lines:")
        for line in res.stdout.splitlines():
            if 'error:' in line.lower():
                print(f"  {line}")
        sys.exit(1)
    log(f"Build succeeded in {time.time() - t0:.1f}s.")


def push_stream(ha, local_path, remote_path):
    total_bytes = os.path.getsize(local_path)
    total_mb = total_bytes / (1024 * 1024)
    log(f"Pushing {os.path.basename(local_path)} ({total_mb:.2f} MB)...")
    
    t0 = time.time()
    sent_bytes = 0
    h = ha.fopen(remote_path, 'w')
    with open(local_path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            ha.fwrite(h, chunk)
            sent_bytes += len(chunk)
            pct = (sent_bytes / total_bytes) * 100
            elapsed = time.time() - t0
            speed = (sent_bytes / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            print(f"\r  [{pct:5.1f}%] {sent_bytes / (1024 * 1024):.1f}/{total_mb:.1f} MB ({speed:.1f} MB/s)", end='', flush=True)
    ha.fclose(h)
    print()

    elapsed = time.time() - t0
    avg_speed = total_mb / elapsed if elapsed > 0 else 0
    stat_info = ha.stat(remote_path)
    rem_size = stat_info.get('st_size', 0)
    assert rem_size == total_bytes, f"Size verification failed! Local={total_bytes}, Remote={rem_size}"
    log(f"Transfer complete in {elapsed:.2f}s ({avg_speed:.1f} MB/s). Remote stat verified: {rem_size} bytes.")


def reset_patch_revision(ha, remote_app_dir):
    plist_path = f"{remote_app_dir}/LCAppInfo.plist"
    try:
        h = ha.fopen(plist_path, 'r')
        raw = ha.fread(h, 65536)
        ha.fclose(h)
        data = plistlib.loads(raw)
    except Exception as e:
        log(f"LCAppInfo.plist not found or empty ({e}); LiveContainer will generate fresh config.")
        return

    prev_val = data.get('LCPatchRevision')
    data['LCPatchRevision'] = 0
    
    log(f"Setting LCPatchRevision to 0 (was {prev_val}) in {plist_path}...")
    new_data = plistlib.dumps(data, fmt=plistlib.FMT_BINARY)
    h = ha.fopen(plist_path, 'w')
    ha.fwrite(h, new_data)
    ha.fclose(h)
    log("LCPatchRevision reset to 0 (binary plist).")


def main():
    parser = argparse.ArgumentParser(description="Build and push nxnative binary to LiveContainer on iOS.")
    parser.add_argument('-t', '--title-id', default=TITLE_ID, help="Base title ID (e.g. 01008CF01BAAC000)")
    parser.add_argument('-b', '--build', action='store_true', default=REBUILD, help="Rebuild DmIOS before pushing")
    parser.add_argument('--app-bin', default='', help="Direct path to DmIOS binary to push")
    parser.add_argument('--container-id', default=LIVE_CONTAINER_ID, help="LiveContainer bundle ID")
    parser.add_argument('--device-id', default=DEVICE_ID, help="Target device UDID")
    parser.add_argument('--no-reset-patch', action='store_true', help="Do not reset LCPatchRevision flag")
    args = parser.parse_args()

    repo_root = find_repo_root()
    title_id = args.title_id
    if not title_id:
        detected = detect_title_id(repo_root)
        if isinstance(detected, list):
            sys.exit(f"Multiple game builds found: {detected}. Specify one with -t / --title-id.")
        elif detected:
            title_id = detected
        else:
            sys.exit("No game build directory found under Games/. Specify --title-id.")

    title_id = title_id.upper()
    bundle_name = f"org.nxnative.{title_id.lower()}.app"
    log(f"Target Title ID: {title_id} ({bundle_name})")

    dd_path = os.path.join(repo_root, 'Games', f"{title_id}_build", 'ipa', 'build', 'ios', 'pkg', 'dd')
    local_bin = args.app_bin
    if not local_bin:
        local_bin = os.path.join(dd_path, 'Build', 'Products', 'Release-iphoneos', 'DmIOS.app', 'DmIOS')

    if args.build or not os.path.exists(local_bin):
        run_xcodebuild(repo_root, dd_path)

    if not os.path.exists(local_bin):
        sys.exit(f"Binary not found: {local_bin}")

    log(f"Local binary: {local_bin} ({os.path.getsize(local_bin) / (1024*1024):.2f} MB)")

    # Connect to device
    try:
        log("Connecting to iOS device via usbmux...")
        ld = create_using_usbmux(serial=args.device_id or None)
        dev_name = ld.all_values.get('DeviceName', 'iOS Device')
        dev_id = ld.identifier
        log(f"Connected: {dev_name} ({dev_id})")
    except NoDeviceConnectedError:
        sys.exit("Error: No iOS device connected. Plug in your device via USB or ensure Wi-Fi sync is trusted.")
    except Exception as e:
        sys.exit(f"Device connection failed: {e}")

    # Determine LiveContainer bundle ID
    lc_candidates = [args.container_id] if args.container_id else [
        'com.kdt.livecontainer.DUF42MYZ68',
        'com.kdt.LiveContainer2.DUF42MYZ68',
        'com.kdt.livecontainer'
    ]

    selected_ha = None
    selected_bid = None
    for bid in lc_candidates:
        if not bid:
            continue
        try:
            ha = HouseArrestService(lockdown=ld, bundle_id=bid)
            apps = ha.listdir('Documents/Applications')
            if bundle_name in apps:
                selected_ha = ha
                selected_bid = bid
                log(f"Found {bundle_name} inside LiveContainer container: {bid}")
                break
        except Exception:
            continue

    if not selected_ha:
        # Fallback to first working container
        for bid in lc_candidates:
            if not bid:
                continue
            try:
                ha = HouseArrestService(lockdown=ld, bundle_id=bid)
                selected_ha = ha
                selected_bid = bid
                break
            except Exception:
                continue

    if not selected_ha:
        sys.exit(f"Error: Could not access LiveContainer container. Verified bundle IDs: {lc_candidates}")

    remote_app_dir = f"Documents/Applications/{bundle_name}"
    remote_bin = f"{remote_app_dir}/DmIOS"

    # Push binary
    push_stream(selected_ha, local_bin, remote_bin)

    # Reset LCPatchRevision
    if RESET_PATCH_FLAG and not args.no_reset_patch:
        reset_patch_revision(selected_ha, remote_app_dir)

    log(f"All done! NOTE: If LiveContainer is running, force-close it (swipe up in iOS App Switcher) before tapping {bundle_name} so it reloads config and re-patches.")


if __name__ == '__main__':
    main()
