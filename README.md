# P8 Tape

Load PICO-8 carts from cassette tape, ZX Spectrum style: a "Program:" header,
raster bars, a loading picture that draws in black and white and then sweeps
into colour, and the real tape sound playing in PICO-8.

## Using it

Download `p8tape-linux` or `p8tape.exe` (Windows) and run it. Nothing else to
install. On Linux, make it runnable first: `chmod +x p8tape-linux`.

The Linux program runs on distributions from 2019 onwards: Ubuntu, Pop!_OS and
Mint 20.04/20+ and newer, Debian 10+, RHEL/AlmaLinux/Rocky 8+, Fedora 29+, and
current Arch and openSUSE.

**Make a tape tab** - choose a cart (`.p8` or `.p8.png`), optionally a loading
picture (or use the cart's own label), and save the tape as a WAV. Play the WAV
into a tape deck to record a real cassette.

**Load a tape tab** - choose where the tape comes from (a sound input with a
level meter, or a WAV file), then press **Boot PICO-8 loader**. Press X on the
loader's title screen, then press play on the tape.

You need your own copy of PICO-8; point the program at it the first time.

Loading pictures are 128x128. The raster bars cover the outer 4 pixels, so
keep anything important inside the middle 120x120.

Settings, the loader cart and loaded carts live in `~/.config/p8tape`
(Linux) or `%APPDATA%\p8tape` (Windows).

## Command line

    p8tape encode mygame.p8.png --screen title.png
    p8tape play --wav mygame.wav
    p8tape decode recording.wav

(The Windows program is built as a windowed app, so it shows no text output.)

## Building the programs

- **GitHub (recommended):** push this folder to a GitHub repository, open
  the Actions tab and run "Build p8tape". It builds the Windows program, and
  builds the Linux one on an old base (AlmaLinux 8) so it runs on older
  distributions too, then tests it on AlmaLinux 8, Debian 10 and 11, Ubuntu
  20.04 and 24.04, Fedora and Arch. The programs appear as downloads on the
  finished run. Pushing a tag such as `v1.0` also attaches them to a release.
- **Windows PC:** install Python 3.12 from python.org, then run
  `build_windows.bat`.
- **Linux:** run `sh build_linux.sh`. The result runs on the system you built
  it on and newer ones only, so use GitHub for a program to share.

Running from source instead: `pip install numpy pillow sounddevice`, then
`python3 p8tape.py` (with `loader.p8` in the same folder).
