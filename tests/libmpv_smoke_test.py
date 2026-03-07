"""
alsa_smoke_test.py - manual smoke test for the ALSA-based audio player.

Run on the target device (Pi Zero 2W) to verify aplay/flac are working:
    python -m tests.libmpv_smoke_test

Optionally pass an ALSA device name as the first argument:
    python -m tests.libmpv_smoke_test plughw:CARD=USB,DEV=0
"""

import sys
import time

from linux_voice_assistant.player.libmpv import LibMpvPlayer

device = sys.argv[1] if len(sys.argv) > 1 else None
player = LibMpvPlayer(device=device)

TEST_URL = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"

print(f"Playing {TEST_URL} for 5 seconds ...")
player.play(TEST_URL, done_callback=lambda: print("Playback finished."))
time.sleep(5)

print("Pausing ...")
player.pause()
time.sleep(2)

print("Resuming ...")
player.resume()
time.sleep(3)

print("Stopping ...")
player.stop()
time.sleep(1)
print("Done.")
