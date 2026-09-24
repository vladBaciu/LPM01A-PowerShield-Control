from time import sleep
from time import time, monotonic
from enum import Enum
import re
from src.SerialCommunication import SerialCommunication
from src.CsvWriter import CsvWriter
from src.UnitConversions import UnitConversions


class StaticCurrentUnstableError(RuntimeError):
    """The device rejected a static measurement because current varied."""


class LPM01A:
    def __init__(
        self, port: str, baud_rate: int, print_info_every_ms: int = 10_000
    ) -> None:
        """
        Initializes the LPM01A device with the given port and baud rate.

        Args:
            port (str): The port where the LPM01A device is connected.
            baud_rate (int): The baud rate for the serial communication.
            print_info_every_ms (int): The interval in ms to print the info.
        """
        self.serial_comm = SerialCommunication(port, baud_rate)
        self.serial_comm.open_serial()
        self.csv_writer = CsvWriter()
        self.csv_writer.write("Current (uA),rx timestamp (us),board timestamps (ms)\n")

        self.uc = UnitConversions()

        self.print_info_every_ms = print_info_every_ms

        self.mode = None

        self.board_timestamp_ms = 0
        self.capture_start_us = 0
        self.num_of_captured_values = 0
        self.last_print_timestamp_ms = 0
        self.board_buffer_usage_percentage = 0

        self.sum_current_values_ua = 0
        self.number_of_current_values = 0

    def _parse_current_sample(self, response: str) -> float:
        """
        Parses a single current sample from the response string.

        Args:
            response (str): The response string containing the current sample.

        Returns:
            float: The current value in uA, or None if parsing fails.
        """
        try:
            if "-" in response:
                exponent_sign = "-"
            elif "+" in response:
                exponent_sign = "+"
            else:
                return None

            split_response = response.split("-")
            try:
                current = int(split_response[0])  # Extract the raw current value
            except ValueError:
                # When the TimeStamp is received,
                # the next current values has \x00 in the beginning so I need to strip it
                current = int(split_response[0][1:])

            exponent = int(split_response[1])  # Extract the exponent value

            # Apply the exponent with the correct sign
            if exponent_sign == "+":
                current = current * pow(10, exponent)
            else:
                current = current * pow(10, ((-1) * exponent))

            current = round(self.uc.A_to_uA(current), 4)
            return current
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _is_device_error(response: str) -> bool:
        return response.lstrip("\x00 \t").lower().startswith(
            ("error:", "powershield > error")
        )

    def _read_and_parse_ascii_single_sample(self, timeout_s: float = 30) -> bool:
        """
        Reads and parses a single sample from the LPM01A device in ASCII mode (for static mode).
        
        Returns:
            bool: True if a sample was successfully read and written to CSV, False otherwise.
        """
        timeout_start = monotonic()
        pending_sample = None
        
        while monotonic() - timeout_start < timeout_s:
            response = self.serial_comm.receive_data()
            if not response:
                continue
            if self._is_device_error(response):
                if "static acquisition: current not constant" in response.lower():
                    raise StaticCurrentUnstableError(
                        f"Measurement rejected by device; no sample saved: {response}"
                    )
                raise RuntimeError(f"Measurement rejected by device; no sample saved: {response}")

            if response == "PowerShield > Acquisition completed":
                if pending_sample is None:
                    raise RuntimeError("Static acquisition completed without a current sample")
                current, local_timestamp_us = pending_sample
                self.csv_writer.write(
                    f"{current},{local_timestamp_us},{self.board_timestamp_ms}\n"
                )
                self.num_of_captured_values += 1
                print(
                    f"Sample captured: {current} uA at {self.uc.us_to_ms(local_timestamp_us)} ms\n"
                    f"Total samples: {self.num_of_captured_values}\n"
                )
                return True

            if "TimeStamp:" in response:
                try:
                    match = re.search(
                        "TimeStamp: (\d+)s (\d+)ms, buff (\d+)%", response
                    )
                    if match:
                        self.board_timestamp_ms = (
                            int(match.group(2)) + int(match.group(1)) * 1000
                        )
                        self.board_buffer_usage_percentage = int(match.group(3))
                except ValueError as e:
                    print(f"Error parsing timestamp: {response} - {e}")
                continue

            # Try to parse the current sample
            current = self._parse_current_sample(response)
            if current is not None:
                local_timestamp_us = (
                    int(self.uc.s_to_us(time())) - self.capture_start_us
                )
                pending_sample = (current, local_timestamp_us)
        
        print(f"Timeout: No completed static measurement within {timeout_s} seconds")
        return False

    def _read_and_parse_ascii(self, duration_s: float = None) -> None:
        """
        Reads and parses the data from the LPM01A device in ASCII mode (continuous mode).
        """
        deadline = None if duration_s is None else monotonic() + duration_s
        while deadline is None or monotonic() < deadline:
            response = self.serial_comm.receive_data()
            if not response:
                continue
            if self._is_device_error(response):
                raise RuntimeError(f"Acquisition stopped by device: {response}")

            if "TimeStamp:" in response:
                try:
                    match = re.search(
                        "TimeStamp: (\d+)s (\d+)ms, buff (\d+)%", response
                    )
                    if match:
                        self.board_timestamp_ms = (
                            int(match.group(2)) + int(match.group(1)) * 1000
                        )
                        self.board_buffer_usage_percentage = int(match.group(3))

                except ValueError as e:
                    print(f"Error parsing timestamp: {response} - {e}")
                continue

            # Try to parse the current sample
            current = self._parse_current_sample(response)
            if current is not None:
                local_timestamp_us = (
                    int(self.uc.s_to_us(time())) - self.capture_start_us
                )
                self.csv_writer.write(
                    f"{current},{local_timestamp_us},{self.board_timestamp_ms}\n"
                )
                self.num_of_captured_values += 1

                self.sum_current_values_ua += current
                self.number_of_current_values += 1

                if (
                    self.uc.us_to_ms(local_timestamp_us) - self.last_print_timestamp_ms
                    > self.print_info_every_ms
                ):
                    average_current = (
                        self.sum_current_values_ua / self.number_of_current_values
                    )
                    average_current = round(average_current, 4)
                    self.sum_current_values_ua = 0
                    self.number_of_current_values = 0
                    print(
                        f"Average current for previous {self.print_info_every_ms} ms: {average_current} uA\n"
                        f"Local timestamp: {self.uc.us_to_ms(local_timestamp_us)} ms\n"
                        f"Num of received values: {self.num_of_captured_values}\n"
                        f"LPM01A buffer usage: {self.board_buffer_usage_percentage}%\n"
                    )
                    self.last_print_timestamp_ms = self.uc.us_to_ms(local_timestamp_us)

    def send_command_wait_for_response(
        self, command: str, expected_response: str = None, timeout_s: int = 5
    ) -> bool:
        """
        Sends a command to the LPM01A device and waits for a response.

        This device emits both command acknowledgements and measurement summary lines.
        We therefore accept either the command-specific ack or the summary-finalization
        line for stop/measurement completion instead of assuming a single exact reply.
        """
        if self.serial_comm.ser and self.serial_comm.ser.is_open:
            self.serial_comm.ser.reset_input_buffer()

        tick_start = time()
        self.serial_comm.send_data(command)
        while time() - tick_start < timeout_s:
            response = self.serial_comm.receive_data()
            if not response:
                continue
            # Status is a query and clears the firmware error state. Its reply
            # can include the previous error rather than a bare command ack.
            if command == "status" and (
                response.startswith("PowerShield > ack status")
                or response in {"ok", "PowerShield > ok"}
                or self._is_device_error(response)
            ):
                return True
            if self._is_device_error(response):
                print(f"Command failed: {response}")
                return False

            if expected_response is not None:
                if response == expected_response:
                    return True
                if response.startswith("PowerShield > ack ") and response.endswith(command):
                    return True
                if "Acquisition completed" in response:
                    return True
                continue

            if response.startswith("PowerShield > ack ") and response.endswith(command):
                return True

            if response in {"end", "PowerShield > Acquisition completed"}:
                continue

        return False

    def init_device(
        self,
        mode: str = "ascii",
        voltage: int = 3300,
        freq: int = 5000,
        duration: int = 0,
    ) -> None:
        """
        Initializes the LPM01A device with the given mode, voltage, frequency, and duration.

        Args:
            mode (str): The mode for the LPM01A device. Currently only supports "ascii".
            voltage (int): The voltage for the LPM01A device.
            freq (int): The frequency for the LPM01A device.
            duration (int): The duration for the LPM01A device.
        """

        self.mode = mode
        self.send_command_wait_for_response("htc")

        if self.mode == "ascii":
            self.send_command_wait_for_response(f"format ascii_dec")
        else:
            raise NotImplementedError
            self.send_command_wait_for_response(f"format bin_hexa")
            
        # Static mode is required for high-current measurements. Dynamic mode is only
        # suitable for low-current waveform tracking and saturates/limits current above ~50 mA.
        self.send_command_wait_for_response("acqmode stat")
        if not self.send_command_wait_for_response("pwrend on"):
            raise RuntimeError("Device did not acknowledge pwrend on")
        sleep(2)

        # The board rejects time-suffixed commands such as "1s". For static acquisition,
        # set a numeric acquisition time value; 0 means the board uses the static measurement
        # mode semantics and waits for a completed capture cycle.
        self.send_command_wait_for_response(f"acqtime {int(duration)}")
        self.send_command_wait_for_response(f"volt {voltage}m")
        self.send_command_wait_for_response(f"freq {freq}")

    def start_capture(self, num_samples: int = None, delay_between_samples: float = 0.1) -> None:
        """
        Starts the capture of the LPM01A device.
        
        For static mode: Captures multiple samples in a loop.
        For continuous mode: Starts continuous capturing (user must call read_and_parse_data()).
        
        Args:
            num_samples (int): Number of samples to capture (only for static mode). 
                             If None, starts continuous capture.
            delay_between_samples (float): Delay in seconds between samples (only for static mode). 
                                         Default is 0.1 seconds.
        """
        print(f"Starting capture, printing info every {self.print_info_every_ms} ms")

        # Power-on is managed by the caller so the board is not powered on twice.
        # The measurement process assumes the board is already in the powered state.
        self.capture_start_us = int(self.uc.s_to_us(time()))
        
        # Static mode: capture specific number of samples
        if num_samples is not None:
            print(f"Capturing {num_samples} samples in static mode...")
            for i in range(num_samples):
                success = self.capture_single_sample()
                if not success:
                    print(f"Failed to capture sample {i+1}/{num_samples}")
                    break
                
                # Add delay between measurements (except after the last one)
                if i < num_samples - 1:
                    sleep(delay_between_samples)
            
            print(f"Static capture complete. Total samples: {self.num_of_captured_values}/{num_samples}")
        
        # Continuous mode: start capture (user calls read_and_parse_data())
        else:
            self.send_command_wait_for_response("start")

    def capture_static_for(self, duration_s: float) -> None:
        """Repeat static acquisitions for a host-timed interval.

        Static mode produces one sample per start; frequency does not control
        its cadence. Serial timeouts and final cleanup can extend the interval.
        """
        if duration_s <= 0:
            raise ValueError("duration_s must be greater than zero")
        if self.mode != "ascii":
            raise NotImplementedError
        self.capture_start_us = int(self.uc.s_to_us(time()))
        deadline = monotonic() + duration_s
        print(f"Capturing repeated static measurements for {duration_s} seconds...")
        while monotonic() < deadline:
            if not self.capture_single_sample(deadline=deadline):
                break

    def capture_single_sample(
        self, deadline: float = None, max_retries: int = 3, retry_delay_s: float = 0.5
    ) -> bool:
        """Capture a valid static sample, retrying only current-stability failures.

        Failed attempts stop acquisition and clear status without releasing
        host control or cycling target power. Up to three
        retries follow the initial attempt by default. The optional monotonic
        deadline includes recovery and settling delays.
        """
        if not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if retry_delay_s < 0:
            raise ValueError("retry_delay_s must be nonnegative")
        for attempt in range(max_retries + 1):
            try:
                return self._capture_single_sample_attempt(deadline)
            except StaticCurrentUnstableError as error:
                print(error)
                if not self.send_command_wait_for_response("status"):
                    raise RuntimeError("Device did not respond to status; recovery stopped") from error
                if attempt == max_retries:
                    raise StaticCurrentUnstableError(
                        f"Static current not constant after {attempt + 1} attempts; "
                        "stabilize the target workload before capturing again."
                    ) from error
                delay = retry_delay_s
                if deadline is not None:
                    remaining = deadline - monotonic()
                    if remaining <= delay:
                        print("Capture interval ended before another static retry could start.")
                        return False
                print(f"Retrying static measurement {attempt + 1}/{max_retries} in {delay}s...")
                sleep(delay)

    def _capture_single_sample_attempt(self, deadline: float = None) -> bool:
        """
        Captures a single sample in static mode.
        Starts a measurement, reads one sample, writes it to CSV, then finalizes the
        board state before another `start` is issued.

        Returns:
            bool: True if sample was successfully captured, False otherwise.
        """
        timeout_s = 5 if deadline is None else min(5, deadline - monotonic())
        if timeout_s <= 0:
            return False
        completed = False
        try:
            if not self.send_command_wait_for_response("start", timeout_s=timeout_s):
                raise RuntimeError("Device did not acknowledge start")
            remaining_s = 30 if deadline is None else max(0, deadline - monotonic())
            completed = self._read_and_parse_ascii_single_sample(timeout_s=remaining_s)
            return completed
        finally:
            if not completed:
                self.stop_capture()

    def stop_capture(self) -> None:
        """
        Stops the capture of the LPM01A device.

        Retains host control and the configured target power state. Releasing
        control here would hand power/configuration back to standalone mode.
        """
        if not self.send_command_wait_for_response("stop"):
            raise RuntimeError("Capture cleanup failed: stop was not acknowledged")

    def deinit_capture(self) -> None:
        """
        Deinitializes the capture of the LPM01A device.
        """
        self.csv_writer.close()
        self.serial_comm.close_serial()

    def read_and_parse_data(self, duration_s: float = None) -> None:
        """
        Reads and saves samples, indefinitely or for the given duration in seconds.

        A pending serial read can extend the duration by up to the serial timeout.
        """
        if duration_s is not None and duration_s <= 0:
            raise ValueError("duration_s must be greater than zero")
        self.capture_start_us = int(self.uc.s_to_us(time()))
        if self.mode == "ascii":
            self._read_and_parse_ascii(duration_s=duration_s)
        else:
            raise NotImplementedError
