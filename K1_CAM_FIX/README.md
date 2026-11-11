# K1 Nebula camera firmware

This is the firmware package for the Creality K1's Nebula USB camera (UnionImage CCX2F3298/CCX2F3299, Ingenic T31), taken from Creality's `K1_CAM_FIX` Windows flashing kit. The Windows tool and its DLLs are left out; `crealitycamflash.py` replaces them.

The camera reports `UVC-UnionImage-CCX2F3298-240103V020` after flashing.

## Tested

Flashed on a K1 SE with the camera plugged into the printer, using `crealitycamflash.py` on the printer itself. The camera was a CCX2F3299 on `250422V031`, and after flashing it reports as CCX2F3298 on `240103V020`. It came back on `/dev/video4` and streams at 1280x720 in ustreamer.

## Flashing from the K1

SSH into the printer as root:

```sh
cd /usr/data
git clone -b k1-nebula-cam https://github.com/Cartrigger/creality-camera-flasher-tux.git
cd creality-camera-flasher-tux
chmod +x crealitycamflash.py
```

Stop whatever is streaming from the camera. On printers set up with Simple AF:

```sh
/etc/init.d/S50webcam stop
```

Check the camera is seen, do a dry run, then flash:

```sh
./crealitycamflash.py version
./crealitycamflash.py -n flash K1_CAM_FIX
./crealitycamflash.py flash K1_CAM_FIX
```

It takes about two minutes. Don't power off the printer or unplug the camera while it runs. The camera reboots by itself at the end; then check it and start streaming again:

```sh
./crealitycamflash.py version
/etc/init.d/S50webcam start
```

With `-v`, the last step may print `reboot: [Errno 5] SG_IO host 0x7`. That's the camera dropping off USB as it reboots, and it's expected.

## Flashing from a Linux PC

Plug the camera into the PC, then from the repository folder:

```sh
sudo ./crealitycamflash.py version
sudo ./crealitycamflash.py flash K1_CAM_FIX
```

## What's in here

| File | |
|---|---|
| `auto_update.txt` | the update script the tool runs |
| `uImage` | camera kernel, written by `sfwritekernel` |
| `appfs.jffs2` | camera app partition, written by `sfwriteappfs` |
| `version.ini` | the version the Windows tool shows; older than what the images report |
| `iboot.bin`, `ucamera-t31`, `root-uclibc-toolchain4.7.2-1.1.squashfs` | shipped in the kit but not used by `auto_update.txt` |
