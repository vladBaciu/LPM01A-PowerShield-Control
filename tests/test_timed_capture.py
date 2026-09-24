import ast
import csv
from collections import deque
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.CsvWriter import CsvWriter
from src.LPM01A import LPM01A, StaticCurrentUnstableError


class TimedCaptureTests(unittest.TestCase):
    def test_static_sample_is_not_saved_until_completion(self):
        for tail in ("error: Static acquisition: current not constant", ""):
            with self.subTest(tail=tail), patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
                device = LPM01A("unused", 3864000)
                device.serial_comm.receive_data.side_effect = ["1022-04", "end", tail]
                with patch.object(device, "send_command_wait_for_response", return_value=True) as command, patch(
                    "src.LPM01A.monotonic", side_effect=[0, 0, 1, 2, 31]
                ):
                    if tail:
                        with self.assertRaises(StaticCurrentUnstableError):
                            device._capture_single_sample_attempt()
                    else:
                        self.assertFalse(device._capture_single_sample_attempt())
                self.assertEqual(device.csv_writer.write.call_count, 1)
                self.assertEqual(device.num_of_captured_values, 0)
                self.assertEqual([call.args[0] for call in command.call_args_list], ["start", "stop"])

    def test_status_accepts_query_reply_with_previous_error(self):
        for response in ("PowerShield > ack status: ok", "PowerShield > ack status: error: current not constant", "error: current not constant"):
            with self.subTest(response=response), patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
                device = LPM01A("unused", 3864000)
                device.serial_comm.receive_data.return_value = response
                self.assertTrue(device.send_command_wait_for_response("status"))

    def test_retry_discards_failed_payload_then_saves_valid_sample(self):
        with patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"), patch("src.LPM01A.sleep") as sleep:
            device = LPM01A("unused", 3864000)
            pending = deque()
            attempts = [0]

            def send(command):
                pending.append(f"PowerShield > ack {command}")
                if command == "start":
                    attempts[0] += 1
                    if attempts[0] == 1:
                        pending.extend([
                            "error: Static acquisition: current not constant (current peak low). Acquisition stopped.",
                            "1200-04", "end", "PowerShield > Acquisition completed",
                        ])
                    else:
                        pending.extend(["7784-05", "end", "summary beg", "Acquisition mode: static", "summary end", "PowerShield > Acquisition completed"])

            device.serial_comm.send_data.side_effect = send
            device.serial_comm.receive_data.side_effect = lambda: pending.popleft() if pending else ""
            device.serial_comm.ser.reset_input_buffer.side_effect = pending.clear
            self.assertTrue(device.capture_single_sample())
            self.assertEqual(device.num_of_captured_values, 1)
            self.assertEqual(device.csv_writer.write.call_count, 2)
            self.assertTrue(device.csv_writer.write.call_args.args[0].startswith("77840.0,"))
            self.assertEqual(
                [call.args[0] for call in device.serial_comm.send_data.call_args_list],
                ["start", "stop", "status", "start"],
            )
            sleep.assert_any_call(0.5)

    def test_retry_limit_and_deadline(self):
        for deadline, expected_attempts in ((None, 4), (0.25, 1)):
            with self.subTest(deadline=deadline), patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"), patch("src.LPM01A.sleep"), patch("src.LPM01A.monotonic", return_value=0):
                device = LPM01A("unused", 3864000)
                device.serial_comm.receive_data.return_value = "error: Static acquisition: current not constant (current peak low). Acquisition stopped."
                with patch.object(device, "send_command_wait_for_response", return_value=True) as command:
                    if deadline is None:
                        with self.assertRaisesRegex(StaticCurrentUnstableError, "after 4 attempts"):
                            device.capture_single_sample()
                    else:
                        self.assertFalse(device.capture_single_sample(deadline=deadline))
                self.assertEqual([call.args[0] for call in command.call_args_list], ["start", "stop", "status"] * expected_attempts)
                self.assertEqual(device.num_of_captured_values, 0)

    def test_unrelated_errors_and_failed_recovery_are_not_retried(self):
        for response, cleanup_ok in (("error: Overcurrent", True), ("error: Static acquisition: current not constant", False)):
            with self.subTest(response=response), patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"), patch("src.LPM01A.sleep"):
                device = LPM01A("unused", 3864000)
                device.serial_comm.receive_data.return_value = response
                with patch.object(device, "send_command_wait_for_response", side_effect=[True, cleanup_ok]) as command:
                    with self.assertRaises(RuntimeError) as error:
                        device.capture_single_sample()
                self.assertNotIsInstance(error.exception, StaticCurrentUnstableError)
                self.assertEqual(command.call_count, 2)
                self.assertEqual(device.num_of_captured_values, 0)

    def test_initialization_requires_keep_power_acknowledgement(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted), patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"), patch("src.LPM01A.sleep"):
                device = LPM01A("unused", 3864000)
                with patch.object(device, "send_command_wait_for_response", side_effect=lambda command: accepted if command == "pwrend on" else True) as command:
                    if accepted:
                        device.init_device()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "pwrend on"):
                            device.init_device()
                self.assertIn("pwrend on", [call.args[0] for call in command.call_args_list])

    def test_static_error_does_not_save_trailing_number_and_still_stops(self):
        for prefix in ("error:", "PowerShield > error:"):
            with self.subTest(prefix=prefix), patch("src.LPM01A.SerialCommunication"), patch(
                "src.LPM01A.CsvWriter"
            ):
                device = LPM01A("unused", 3864000)
                device.mode = "ascii"
                device.serial_comm.receive_data.side_effect = [
                    f"{prefix} Static acquisition: current not constant (current peak low). Acquisition stopped.",
                    "1200-04",
                ]
                with patch.object(device, "send_command_wait_for_response", return_value=True) as command, patch(
                    "src.LPM01A.sleep"
                ):
                    with self.assertRaisesRegex(RuntimeError, "current not constant"):
                        device.capture_single_sample(max_retries=0)
                self.assertEqual(device.num_of_captured_values, 0)
                self.assertEqual(device.csv_writer.write.call_count, 1)  # Header only.
                self.assertEqual([call.args[0] for call in command.call_args_list], ["start", "stop", "status"])

    def test_stream_error_does_not_save_trailing_number(self):
        with patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
            device = LPM01A("unused", 3864000)
            device.mode = "ascii"
            device.serial_comm.receive_data.side_effect = ["error: Acquisition stopped.", "1200-04"]
            with self.assertRaisesRegex(RuntimeError, "Acquisition stopped"):
                device.read_and_parse_data(duration_s=5)
            self.assertEqual(device.num_of_captured_values, 0)
            self.assertEqual(device.csv_writer.write.call_count, 1)

    def test_command_rejects_unprefixed_error_even_with_expected_response(self):
        with patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
            device = LPM01A("unused", 3864000)
            device.serial_comm.receive_data.side_effect = ["error: Acquisition stopped.", "PowerShield > ack start"]
            self.assertFalse(device.send_command_wait_for_response("start", expected_response="PowerShield > ack start"))
            self.assertEqual(device.serial_comm.receive_data.call_count, 1)

    def test_samples_are_saved_before_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(CsvWriter, "CSV_LOGS_FOLDER", directory), patch(
                "src.LPM01A.SerialCommunication"
            ) as communication:
                device = LPM01A("unused", 3864000)
                device.mode = "ascii"
                communication.return_value.receive_data.side_effect = [
                    "", "TimeStamp: 1s 2ms, buff 3%", "125-06", "250-06"
                ]
                try:
                    with patch("src.LPM01A.monotonic", side_effect=[0, 0, 1, 2, 3, 5]):
                        device.read_and_parse_data(duration_s=5)
                finally:
                    device.deinit_capture()

                with (Path(directory) / device.csv_writer.filename).open() as stream:
                    rows = list(csv.reader(stream))
                self.assertEqual(len(rows), 3)
                self.assertEqual([float(row[0]) for row in rows[1:]], [125, 250])
                self.assertEqual([row[2] for row in rows[1:]], ["1002", "1002"])
                self.assertEqual(device.num_of_captured_values, 2)

    def test_silent_device_still_reaches_deadline(self):
        with patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
            device = LPM01A("unused", 3864000)
            device.mode = "ascii"
            device.serial_comm.receive_data.return_value = ""
            with patch("src.LPM01A.monotonic", side_effect=[0, 0, 1, 5]):
                device.read_and_parse_data(duration_s=5)
            self.assertEqual(device.serial_comm.receive_data.call_count, 2)
            self.assertEqual(device.num_of_captured_values, 0)

    def test_timed_script_runs_static_loop_and_closes_on_success_or_failure(self):
        script = Path(__file__).resolve().parents[1] / "data_acquisition.py"
        duration = next(
            ast.literal_eval(node.value)
            for node in ast.parse(script.read_text()).body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "MEASURE_FOR_SECONDS" for target in node.targets)
        )
        for failure in (None, RuntimeError("read failed")):
            with self.subTest(failure=failure):
                device = Mock(num_of_captured_values=2)
                device.capture_static_for.side_effect = failure
                with patch("src.LPM01A.LPM01A", return_value=device), patch(
                    "builtins.input", return_value=""
                ), patch("time.sleep"):
                    if failure:
                        with self.assertRaises(SystemExit) as caught:
                            runpy.run_path(str(script), run_name="__main__")
                        self.assertEqual(caught.exception.code, 1)
                    else:
                        runpy.run_path(str(script), run_name="__main__")
                device.capture_static_for.assert_called_once_with(duration_s=duration)
                calls = [call[0] for call in device.mock_calls]
                self.assertLess(calls.index("capture_static_for"), calls.index("deinit_capture"))
                device.deinit_capture.assert_called_once()

    def test_static_loop_restarts_device_and_saves_multiple_samples(self):
        with patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
            device = LPM01A("unused", 3864000)
            device.mode = "ascii"
            elapsed = [0]
            commands = []

            def command(name, **kwargs):
                commands.append(name)
                return True

            responses = iter(["7784-05", "PowerShield > Acquisition completed"] * 3)

            def receive():
                response = next(responses)
                if response == "PowerShield > Acquisition completed":
                    elapsed[0] += 1
                return response

            device.serial_comm.receive_data.side_effect = receive
            with patch.object(device, "send_command_wait_for_response", side_effect=command), patch(
                "src.LPM01A.monotonic", side_effect=lambda: elapsed[0]
            ), patch("src.LPM01A.sleep"):
                device.capture_static_for(duration_s=3)
            self.assertEqual(commands, ["start"] * 3)
            self.assertEqual(device.num_of_captured_values, 3)
            samples = device.csv_writer.write.call_args_list[1:]
            self.assertEqual(len(samples), 3)
            self.assertTrue(all(call.args[0].startswith("77840.0,") for call in samples))

    def test_static_timeout_stops_and_does_not_restart(self):
        with patch("src.LPM01A.SerialCommunication"), patch("src.LPM01A.CsvWriter"):
            device = LPM01A("unused", 3864000)
            device.mode = "ascii"
            elapsed = [0]

            def receive():
                elapsed[0] += 1
                return ""

            device.serial_comm.receive_data.side_effect = receive
            with patch.object(device, "send_command_wait_for_response", return_value=True) as command, patch(
                "src.LPM01A.monotonic", side_effect=lambda: elapsed[0]
            ), patch("src.LPM01A.sleep"):
                device.capture_static_for(duration_s=3)
            self.assertEqual([call.args[0] for call in command.call_args_list], ["start", "stop"])
            self.assertEqual(device.num_of_captured_values, 0)
            self.assertEqual(elapsed[0], 3)


if __name__ == "__main__":
    unittest.main()
