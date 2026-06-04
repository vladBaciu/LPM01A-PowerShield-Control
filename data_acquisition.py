#!/bin/env python3

from src.LPM01A import LPM01A

# Configuration
CAPTURE_MODE = "static"  # "static" for multiple samples, "continuous" for streaming
NUM_SAMPLES = 100
DELAY_BETWEEN_SAMPLES = 0  # Set to 0 for fastest sampling, increase if needed (in seconds)

try:
    lpm = LPM01A(port="COM6", baud_rate=3864000, print_info_every_ms=10_000)
    lpm.init_device(mode="ascii", voltage=3300, freq=500, duration=0)
    
    if CAPTURE_MODE == "static":
        lpm.start_capture(num_samples=NUM_SAMPLES, delay_between_samples=DELAY_BETWEEN_SAMPLES)
    else:
        lpm.start_capture()
        lpm.read_and_parse_data()
    
except KeyboardInterrupt:
    print("KeyboardInterrupt detected. Exiting...")
    lpm.stop_capture()
    lpm.deinit_capture()
    exit(0)
except Exception as e:
    print(f"Error: {e}")
    lpm.deinit_capture()
    exit(1)
