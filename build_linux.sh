#!/bin/sh
# Builds dist/p8tape on Linux (Debian/Ubuntu).
# The result runs on this distribution and newer ones.
set -e
sudo apt-get install -y python3-venv python3-tk libportaudio2
python3 -m venv build-env
./build-env/bin/pip install pyinstaller numpy pillow sounddevice
cp -L "$(ldconfig -p | awk '/libportaudio\.so\.2 / {print $NF; exit}')" ./libportaudio.so.2
./build-env/bin/pyinstaller --onefile --name p8tape \
    --add-data "loader.p8:." \
    --add-binary "libportaudio.so.2:." \
    --hidden-import sounddevice --noconfirm p8tape.py
echo "Done: dist/p8tape"
