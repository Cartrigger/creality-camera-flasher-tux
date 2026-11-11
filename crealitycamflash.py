#!/usr/bin/env python3
"""Flash Creality (Ingenic T31) UVC cameras from Linux. See README.md."""
import argparse
import ctypes
import errno
import fcntl
import glob
import hashlib
import os
import platform
import struct
import sys
import time

VENDOR_ID = 0xA108
XU_GUID = bytes.fromhex('a29e7641de0447e38b2bf4341aff003b')
XU_SEL_VERSION = 15
XU_SEL_UPDATE = 13

UVC_SET_CUR, UVC_GET_CUR, UVC_GET_LEN = 0x01, 0x81, 0x85

SCSI_OP = 0xE8
MODE_DATA, MODE_STATUS, MODE_DATA_LAST, MODE_LOADINFO, MODE_CMD = 1, 2, 4, 5, 6
CHUNK = 0x8000
BOOT_CHUNK = 0x400

STATE_NAMES = {0: 'empty', 1: 'UPD (boot ROM)', 2: 'UBO (ready)', 3: 'unknown'}

verbose = False
dry_run = False


def log(msg):
    print(msg, flush=True)


def debug(msg):
    if verbose:
        print('  ' + msg, file=sys.stderr, flush=True)


def ioc(direction, typ, nr, size):
    if platform.machine().startswith(('mips', 'ppc', 'powerpc', 'sparc', 'alpha')):
        dirs, dirshift, sizebits = {'w': 4, 'r': 2, 'rw': 6}, 29, 13
    else:
        dirs, dirshift, sizebits = {'w': 1, 'r': 2, 'rw': 3}, 30, 14
    assert size < (1 << sizebits)
    return (dirs[direction] << dirshift) | (size << 16) | (ord(typ) << 8) | nr


class XuQuery(ctypes.Structure):
    _fields_ = [('unit', ctypes.c_uint8), ('selector', ctypes.c_uint8),
                ('query', ctypes.c_uint8), ('size', ctypes.c_uint16),
                ('data', ctypes.c_void_p)]


UVCIOC_CTRL_QUERY = ioc('rw', 'u', 0x21, ctypes.sizeof(XuQuery))


class SgIoHdr(ctypes.Structure):
    _fields_ = [('interface_id', ctypes.c_int), ('dxfer_direction', ctypes.c_int),
                ('cmd_len', ctypes.c_ubyte), ('mx_sb_len', ctypes.c_ubyte),
                ('iovec_count', ctypes.c_ushort), ('dxfer_len', ctypes.c_uint),
                ('dxferp', ctypes.c_void_p), ('cmdp', ctypes.c_void_p),
                ('sbp', ctypes.c_void_p), ('timeout', ctypes.c_uint),
                ('flags', ctypes.c_uint), ('pack_id', ctypes.c_int),
                ('usr_ptr', ctypes.c_void_p), ('status', ctypes.c_ubyte),
                ('masked_status', ctypes.c_ubyte), ('msg_status', ctypes.c_ubyte),
                ('sb_len_wr', ctypes.c_ubyte), ('host_status', ctypes.c_ushort),
                ('driver_status', ctypes.c_ushort), ('resid', ctypes.c_int),
                ('duration', ctypes.c_uint), ('info', ctypes.c_uint)]


SG_IO = 0x2285
SG_DXFER_TO_DEV, SG_DXFER_FROM_DEV = -2, -3


class CamError(Exception):
    pass


# ---------------------------------------------------------------- UVC side

def usb_device_dir(path):
    path = os.path.realpath(path)
    while path != '/' and not os.path.exists(os.path.join(path, 'idVendor')):
        path = os.path.dirname(path)
    return path if path != '/' else None


def read_sys(path, default=''):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def find_xu_unit(usbdir):
    try:
        with open(os.path.join(usbdir, 'descriptors'), 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    i = 0
    while i + 2 <= len(raw):
        length, dtype = raw[i], raw[i + 1]
        if length < 2:
            break
        d = raw[i:i + length]
        # CS_INTERFACE, VC_EXTENSION_UNIT: bUnitID then guidExtensionCode
        if dtype == 0x24 and length >= 20 and d[2] == 0x06 and d[4:20] == XU_GUID:
            return d[3]
        i += length
    return None


def find_cameras():
    cams = []
    for node in sorted(glob.glob('/sys/class/video4linux/video*'),
                       key=lambda p: int(p.rsplit('video', 1)[1])):
        usbdir = usb_device_dir(os.path.join(node, 'device'))
        if not usbdir:
            continue
        if int(read_sys(os.path.join(usbdir, 'idVendor'), '0'), 16) != VENDOR_ID:
            continue
        if any(c['usb'] == usbdir for c in cams):
            next(c for c in cams if c['usb'] == usbdir)['nodes'].append('/dev/' + os.path.basename(node))
            continue
        cams.append({'usb': usbdir, 'nodes': ['/dev/' + os.path.basename(node)],
                     'unit': find_xu_unit(usbdir),
                     'product': read_sys(os.path.join(usbdir, 'product'))})
    return cams


class UvcCamera:
    def __init__(self, device=None, unit=None):
        cams = find_cameras()
        if device:
            cams = [c for c in cams if device in c['nodes']] or \
                   [{'usb': None, 'nodes': [device], 'unit': None, 'product': ''}]
        if not cams:
            raise CamError(f'no camera with USB vendor {VENDOR_ID:04x} found')
        cam = cams[0]
        self.unit = unit if unit is not None else cam['unit']
        if self.unit is None:
            raise CamError('camera found but its update extension unit was not; pass --unit')
        self.fd = None
        last = None
        for node in cam['nodes']:
            try:
                fd = os.open(node, os.O_RDWR)
            except OSError as e:
                last = e
                continue
            self.fd, self.node = fd, node
            try:
                self.ctrl_len(XU_SEL_VERSION)
                break
            except OSError as e:
                os.close(fd)
                self.fd, last = None, e
        if self.fd is None:
            raise CamError(f'cannot talk to {", ".join(cam["nodes"])}: {last}')
        debug(f'{self.node}: {cam["product"]} extension unit {self.unit}')

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def query(self, selector, query, size, data=None):
        buf = ctypes.create_string_buffer(bytes(data or b'').ljust(size, b'\0'), size)
        q = XuQuery(self.unit, selector, query, size, ctypes.addressof(buf))
        fcntl.ioctl(self.fd, UVCIOC_CTRL_QUERY, q)
        return buf.raw

    def ctrl_len(self, selector):
        return struct.unpack('<H', self.query(selector, UVC_GET_LEN, 2))[0]

    def get(self, selector):
        return self.query(selector, UVC_GET_CUR, self.ctrl_len(selector))

    def set(self, selector, data=b''):
        size = self.ctrl_len(selector)
        self.query(selector, UVC_SET_CUR, size, data)

    def version(self):
        return self.get(XU_SEL_VERSION).split(b'\0', 1)[0].decode(errors='replace')

    def enter_update(self):
        if dry_run:
            log(f'[dry run] XU SET_CUR unit {self.unit} selector {XU_SEL_UPDATE}')
            return
        try:
            self.set(XU_SEL_UPDATE)
        except OSError as e:
            # the camera drops off the bus to come back as a disk, so a
            # failed status on this request is expected
            debug(f'update request: {e}')


# --------------------------------------------------------------- SCSI side

def find_disks():
    disks = []
    for dev in glob.glob('/sys/class/scsi_device/*/device'):
        if not read_sys(os.path.join(dev, 'vendor')).startswith('GC'):
            continue
        nodes = ['/dev/' + os.path.basename(p)
                 for p in glob.glob(os.path.join(dev, 'scsi_generic', 'sg*')) +
                 glob.glob(os.path.join(dev, 'block', 'sd*'))]
        if nodes:
            disks.append({'nodes': nodes, 'model': read_sys(os.path.join(dev, 'model'))})
    return disks


class UpdateDisk:
    def __init__(self, node):
        self.node = node
        self.fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        os.close(self.fd)

    def sg(self, cdb, direction, data, timeout_ms):
        cdb_buf = ctypes.create_string_buffer(bytes(cdb), len(cdb))
        sense = ctypes.create_string_buffer(32)
        h = SgIoHdr()
        h.interface_id = ord('S')
        h.dxfer_direction = direction
        h.cmd_len = len(cdb)
        h.mx_sb_len = len(sense)
        h.dxfer_len = len(data)
        h.dxferp = ctypes.addressof(data) if len(data) else None
        h.cmdp = ctypes.addressof(cdb_buf)
        h.sbp = ctypes.addressof(sense)
        h.timeout = timeout_ms
        fcntl.ioctl(self.fd, SG_IO, h)
        if h.host_status or (h.driver_status & 0x0F) not in (0, 8):
            raise OSError(errno.EIO, f'SG_IO host {h.host_status:#x} driver {h.driver_status:#x}')
        if h.status:
            # the Windows tool never looks at SCSI status, so neither do we
            debug(f'scsi status {h.status:#x} sense {sense.raw[:h.sb_len_wr].hex()}')

    def inquiry(self):
        buf = ctypes.create_string_buffer(36)
        self.sg([0x12, 0, 0, 0, 36, 0], SG_DXFER_FROM_DEV, buf, 2000)
        return buf.raw

    def state(self):
        d = self.inquiry()
        if d[8:10] != b'GC':
            return None
        p = d[16:19]
        return 0 if d[16] == 0 else {b'UPD': 1, b'UBO': 2}.get(p, 3)

    def vendor_cmd(self, mode, payload, read=False):
        cdb = [SCSI_OP, mode, 0, 0, 0, 0] + list(struct.pack('>I', len(payload)))
        buf = ctypes.create_string_buffer(bytes(payload), len(payload))
        if dry_run:
            (debug if mode in (MODE_DATA, MODE_DATA_LAST) else log)(
                f'[dry run] scsi {bytes(cdb).hex()} {len(payload)} bytes')
            return b'\x0d\x00\x00\x00' if read else None
        self.sg(cdb, SG_DXFER_FROM_DEV if read else SG_DXFER_TO_DEV, buf,
                10000 if read else 120000)
        return buf.raw

    def run_cmd(self, cmd):
        log(f'runcmd: {cmd}')
        try:
            self.vendor_cmd(MODE_CMD, cmd.encode())
        except OSError as e:
            if cmd.split()[0] != 'reboot':
                raise CamError(f'command "{cmd}" failed: {e}')
            debug(f'reboot: {e}')

    def write(self, data, chunk):
        total, done, last_pct = len(data), 0, -1
        while done < total:
            size = min(chunk, total - done)
            mode = MODE_DATA_LAST if done + size == total else MODE_DATA
            for attempt in range(501):
                try:
                    self.vendor_cmd(mode, data[done:done + size])
                    break
                except OSError as e:
                    if attempt == 500:
                        raise CamError(f'write timed out at {done}/{total}: {e}')
                    time.sleep(0.1)
            done += size
            pct = 100 * done // total
            if pct != last_pct:
                print(f'\r  {pct:3d}%', end='', flush=True)
                last_pct = pct
        print(flush=True)

    def status(self, timeout=60):
        end = time.monotonic() + timeout
        while True:
            try:
                return self.vendor_cmd(MODE_STATUS, b'\xff' * 4, read=True)
            except OSError:
                if time.monotonic() > end:
                    raise CamError('no status from camera')
                time.sleep(0.5)

    def load(self, addr, path):
        with open(path, 'rb') as f:
            data = f.read()
        log(f'load {os.path.basename(path)} ({len(data)} bytes) to {addr:#x}')
        info = struct.pack('<II', addr, len(data)) + hashlib.md5(data).digest()
        for attempt in range(5):
            try:
                self.vendor_cmd(MODE_LOADINFO, info)
                break
            except OSError as e:
                if attempt == 4:
                    raise CamError(f'sending load info failed: {e}')
                time.sleep(0.5)
        self.write(data, CHUNK)
        st = self.status()
        if st[0] != 0x0D or st[1] != 0x00:
            raise CamError(f'camera rejected {path}: status {st[0]:02x} {st[1]:02x}')

    def boot(self, path):
        with open(path, 'rb') as f:
            data = f.read()
        log(f'boot {os.path.basename(path)} ({len(data)} bytes)')
        self.write(data, BOOT_CHUNK)


def open_disk(want_state=None, timeout=0):
    end = time.monotonic() + timeout
    shown = False
    while True:
        for d in find_disks():
            for node in sorted(d['nodes']):
                try:
                    disk = UpdateDisk(node)
                except OSError as e:
                    debug(f'{node}: {e}')
                    continue
                try:
                    st = disk.state()
                except OSError as e:
                    debug(f'{node}: inquiry failed: {e}')
                    disk.close()
                    continue
                if st is not None and (want_state is None or st in want_state):
                    disk.st = st
                    debug(f'{node}: {d["model"]} state {STATE_NAMES[st]}')
                    return disk
                disk.close()
        if time.monotonic() >= end:
            return None
        if not shown:
            log('waiting for the camera to come back as an update disk...')
            shown = True
        time.sleep(0.5)


# ----------------------------------------------------------------- scripts

def run_script(disk, path, depth=0):
    if depth > 8:
        raise CamError('scripts nested too deep')
    base = os.path.dirname(os.path.abspath(path))
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
    for line in lines:
        line = line.strip()
        if not line or line[0] in '#%':
            continue
        if len(line) > 0x3FF:
            raise CamError(f'command too long: {line[:40]}...')
        words = line.split()
        if words[0] == 'estar' and len(words) > 1:
            run_script(disk, os.path.join(base, words[1]), depth + 1)
        elif words[0] == 'tftp' and len(words) > 2:
            disk.load(int(words[1], 16), os.path.join(base, words[2]))
        else:
            disk.run_cmd(line)


def check_script(path, depth=0):
    base = os.path.dirname(os.path.abspath(path))
    missing = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f.read().splitlines():
            words = line.split()
            if not words or words[0][0] in '#%':
                continue
            if words[0] == 'estar' and len(words) > 1:
                missing += check_script(os.path.join(base, words[1]), depth + 1)
            elif words[0] == 'tftp' and len(words) > 2:
                int(words[1], 16)
                if not os.path.isfile(os.path.join(base, words[2])):
                    missing.append(os.path.join(base, words[2]))
    return missing


# ---------------------------------------------------------------- commands

def cmd_list(args):
    cams = find_cameras()
    for c in cams:
        unit = c['unit'] if c['unit'] is not None else 'not found'
        log(f'camera: {" ".join(c["nodes"])}  {c["product"]}  extension unit {unit}')
    disks = find_disks()
    for d in disks:
        log(f'update disk: {" ".join(sorted(d["nodes"]))}  {d["model"]}')
    if not cams and not disks:
        log('nothing found')
        return 1
    return 0


def cmd_version(args):
    cam = UvcCamera(args.device, args.unit)
    try:
        log(cam.version())
    finally:
        cam.close()
    return 0


def enter_update(args):
    disk = open_disk({1, 2})
    if disk:
        log(f'camera is already an update disk ({STATE_NAMES[disk.st]})')
        return disk
    cam = UvcCamera(args.device, args.unit)
    try:
        try:
            log(f'current firmware: {cam.version()}')
        except OSError as e:
            debug(f'version read failed: {e}')
        log('switching the camera to update mode')
        cam.enter_update()
    finally:
        cam.close()
    if dry_run:
        return None
    disk = open_disk({1, 2}, args.timeout)
    if not disk:
        raise CamError('the camera did not come back as an update disk; '
                       'is usb-storage loaded? (see README.md)')
    return disk


def cmd_update_mode(args):
    disk = enter_update(args)
    if disk:
        log(f'{disk.node}: {STATE_NAMES[disk.st]}')
        disk.close()
    return 0


def resolve_script(path):
    if os.path.isdir(path):
        path = os.path.join(path, 'auto_update.txt')
    if not os.path.isfile(path):
        raise CamError(f'{path} not found')
    return path


def cmd_flash(args):
    script = resolve_script(args.firmware)
    missing = check_script(script)
    if missing:
        raise CamError('missing files: ' + ', '.join(missing))
    if dry_run:
        disk = open_disk({1, 2}) or DryDisk()
    else:
        disk = enter_update(args)
    try:
        if disk.st == 1:
            if not args.boot:
                raise CamError('camera is in boot ROM mode (UPD); pass --boot with a loader such as u-boot.bin')
            disk.boot(args.boot)
            disk.close()
            disk = open_disk({2}, args.timeout)
            if not disk:
                raise CamError('camera did not reach UBO after --boot')
        run_script(disk, script)
    finally:
        disk.close()
    log('done; the camera reboots into the new firmware')
    return 0


class DryDisk(UpdateDisk):
    def __init__(self):
        self.node, self.st = '(dry run)', 2

    def close(self):
        pass


def cmd_raw(args):
    if dry_run:
        disk = DryDisk()
    else:
        disk = open_disk({1, 2}, args.timeout)
        if not disk:
            raise CamError('no update disk; run update-mode first')
    try:
        if args.what == 'cmd':
            disk.run_cmd(' '.join(args.rest))
        elif args.what == 'load':
            if len(args.rest) != 2:
                raise CamError('usage: raw load <addr hex> <file>')
            disk.load(int(args.rest[0], 16), args.rest[1])
        elif args.what == 'boot':
            disk.boot(args.rest[0])
    finally:
        disk.close()
    return 0


def main():
    global verbose, dry_run
    p = argparse.ArgumentParser(description='Flash Creality (Ingenic T31) USB cameras from Linux')
    p.add_argument('-v', '--verbose', action='store_true')
    p.add_argument('-n', '--dry-run', action='store_true', help='print what would be sent instead of sending it')
    p.add_argument('-d', '--device', help='camera video node, such as /dev/video0')
    p.add_argument('--unit', type=int, help='extension unit id, if it cannot be found by itself')
    p.add_argument('-t', '--timeout', type=float, default=30, help='seconds to wait for the update disk (30)')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='show cameras and update disks').set_defaults(fn=cmd_list)
    sub.add_parser('version', help='print the camera firmware version').set_defaults(fn=cmd_version)
    sub.add_parser('update-mode', help='switch the camera to its update disk and stop').set_defaults(fn=cmd_update_mode)
    f = sub.add_parser('flash', help='flash a firmware folder or update script')
    f.add_argument('firmware', help='folder holding auto_update.txt, or the script itself')
    f.add_argument('--boot', help='loader to send first if the camera is in boot ROM mode')
    f.set_defaults(fn=cmd_flash)
    r = sub.add_parser('raw', help='send one step by hand: cmd <text> | load <addr> <file> | boot <file>')
    r.add_argument('what', choices=['cmd', 'load', 'boot'])
    r.add_argument('rest', nargs='+')
    r.set_defaults(fn=cmd_raw)
    args = p.parse_args()
    verbose, dry_run = args.verbose, args.dry_run
    try:
        return args.fn(args)
    except CamError as e:
        log(f'error: {e}')
        return 1
    except PermissionError as e:
        log(f'error: {e}; run as root (see README.md)')
        return 1


if __name__ == '__main__':
    sys.exit(main())
