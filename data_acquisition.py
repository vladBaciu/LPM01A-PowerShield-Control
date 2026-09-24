#!/bin/env python3

from time import sleep

from src.LPM01A import LPM01A

# Configuration
# Increase MEASURE_FOR_SECONDS to allow time to trigger and execute the model.
CAPTURE_MODE = "timed"
MEASURE_FOR_SECONDS = 20

try:
    lpm = LPM01A(port="COM6", baud_rate=3864000, print_info_every_ms=10_000)
    lpm.init_device(mode="ascii", voltage=3300, freq=500, duration=0)

    if CAPTURE_MODE == "static":
        print("Press Enter to start a static acquisition...")
        input()
        lpm.start_capture(num_samples=1, delay_between_samples=0)
    elif CAPTURE_MODE == "timed":
        print("Powering on the board once before the timed capture...")
        lpm.send_command_wait_for_response("pwr on")
        sleep(2)

        print(f"Press Enter to start a {MEASURE_FOR_SECONDS}-second timed acquisition...")
        input()
        lpm.capture_static_for(duration_s=MEASURE_FOR_SECONDS)
        print(f"Timed capture complete. Samples saved: {lpm.num_of_captured_values}")
        if lpm.num_of_captured_values == 0:
            print("No current samples received during the capture interval.")
    else:
        lpm.start_capture()
        lpm.read_and_parse_data()

except KeyboardInterrupt:
    print("KeyboardInterrupt detected. Exiting...")
    if "lpm" in locals():
        lpm.stop_capture()
    exit(0)
except Exception as e:
    print(f"Error: {e}")
    exit(1)
finally:
    if "lpm" in locals():
        lpm.deinit_capture()
