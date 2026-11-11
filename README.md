# creality-camera-flasher-tux

A Linux command-line replacement for Sigmastar's Windows `USBDownloadTool.exe`, which flashes firmware onto Creality's Ingenic T31 USB cameras (such as the K1 camera). It speaks the same protocol, rebuilt from the Windows tool, so the same firmware folders work unchanged.

Only Python 3 is needed; nothing has to be installed.

## Usage

Run it as root: the update talks to the camera with raw SCSI commands, which Linux allows only for root.

```sh
sudo ./crealitycamflash.py list              # cameras and update disks it can see
sudo ./crealitycamflash.py version           # firmware version on the camera
sudo ./crealitycamflash.py flash K1_CAM_FIX  # flash a firmware folder
```

`flash` takes the folder that holds `auto_update.txt` (with `uImage`, `appfs.jffs2` and so on next to it), or the path of the script itself. It reads the version, switches the camera into update mode, waits for it to come back as an update disk, then runs the script. The camera reboots when the script ends with `reboot`; it takes about two minutes in all. Don't unplug it while it runs.

Add `-n` to any command for a dry run, which prints every command it would send without touching the camera, and `-v` for more detail.

| Option | |
|---|---|
| `-d /dev/videoN` | pick the camera when there is more than one |
| `-t SECONDS` | how long to wait for the update disk (default 30) |
| `--unit N` | the camera's extension unit, if it isn't found by itself |
| `flash --boot FILE` | loader to send first if the camera is stuck in boot ROM mode (see below) |

### By hand

`update-mode` only switches the camera to its update disk. `raw` then sends one step at a time:

```sh
sudo ./crealitycamflash.py update-mode
sudo ./crealitycamflash.py raw load 0 uImage
sudo ./crealitycamflash.py raw cmd sfwritekernel
sudo ./crealitycamflash.py raw cmd reboot
```

## Requirements

- The `uvcvideo` and `usb-storage` kernel modules, which nearly every desktop distribution already has. `sg` (`/dev/sg*`) is used if it's loaded; otherwise the tool falls back to `/dev/sd*`.
- On WSL, pass the camera through with [usbipd-win](https://github.com/dorssel/usbipd-win), and attach it again after it switches to update mode: it comes back as a different USB device.

## How it works

1. The camera is found by USB vendor `a108` (Ingenic). Its UVC extension unit `{41769EA2-04DE-E347-8B2B-F4341AFF003B}` is found from the USB descriptors.
2. Selector 15 returns the firmware version. Writing selector 13 makes the camera drop off the bus and come back as a USB disk with vendor `GC`. Its product name says the state: `UBO` is ready for an update; `UPD` is the boot ROM, which needs a loader first.
3. Every update step is a vendor SCSI command, `E8 <mode> 00 00 00 00 <length, 32-bit big-endian>`:

   | Mode | Direction | Payload |
   |---|---|---|
   | 6 | out | a text command, such as `sfwritekernel` or `reboot` |
   | 5 | out | load header: address, size (both 32-bit little-endian), then the MD5 of the file |
   | 1 | out | a 32 KB chunk of the file |
   | 4 | out | the last chunk |
   | 2 | in | 4-byte status; `0D 00` means the file arrived intact |

4. `auto_update.txt` is run line by line. Lines starting with `#` or `%` are comments. `tftp <address in hex> <file>` uploads a file, `estar <script>` runs another script, and any other line is sent as a command.

## Untested paths

It has been checked against the Windows tool's code and with dry runs, but not yet on a camera. `--boot` (boot ROM recovery) copies a path the Windows tool has but doesn't use in its interface, so treat it as experimental.

## License

MIT; see [LICENSE](LICENSE). Firmware files aren't included; they belong to their vendors.
