import socket
import sys
import unittest
from unittest.mock import Mock
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_translated_speech import assert_ports_available, check_services


class RunnerPorts(unittest.TestCase):
    def test_optional_service_exit_does_not_stop_captions(self):
        speech, captions = Mock(), Mock()
        speech.poll.return_value = 3
        captions.poll.return_value = None
        degraded = set()
        with patch('run_translated_speech.print') as output:
            for _ in range(2):
                check_services([('speech', speech, None), ('captions', captions, None)],
                               800*1024, {'speech', 'translation'}, degraded)
        self.assertEqual(degraded, {'speech'})
        output.assert_called_once()

    def test_critical_failure_and_memory_guard_remain_active_in_degraded_mode(self):
        captions = Mock()
        captions.poll.return_value = 2
        with self.assertRaisesRegex(RuntimeError, 'captions exited'):
            check_services([('captions', captions, None)], 800*1024, {'speech', 'translation'})
        with self.assertRaisesRegex(RuntimeError, '768 MiB'):
            check_services([], 767*1024, {'speech', 'translation'})

    def test_retained_services_are_supervised_after_capture_ends(self):
        speech = Mock(); speech.poll.return_value = None
        translation = Mock(); translation.poll.return_value = None
        check_services([('speech', speech, None), ('translation', translation, None)], 800*1024)
        translation.poll.return_value = 3; translation.returncode = 3
        with self.assertRaisesRegex(RuntimeError, 'translation exited'):
            check_services([('speech', speech, None), ('translation', translation, None)], 800*1024)

    def test_retained_services_keep_existing_memory_guard(self):
        speech = Mock(); speech.poll.return_value = None
        with self.assertRaisesRegex(RuntimeError, '768 MiB'):
            check_services([('speech', speech, None)], 767*1024)
        check_services([('speech', speech, None)], 768*1024)

    def test_live_tcp_listener_is_rejected(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); listener.listen(1)
            with self.assertRaises(OSError):
                assert_ports_available([(listener.getsockname()[1], socket.SOCK_STREAM)])

    def test_recently_closed_tcp_allows_restart(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0)); listener.listen(1)
            port = listener.getsockname()[1]
            client.connect(('127.0.0.1', port))
            accepted, _ = listener.accept()
            accepted.close()
            self.assertEqual(client.recv(1), b'')
        # The server closed first, so its accepted connection is in TIME_WAIT.
        with socket.socket() as old_check:
            with self.assertRaises(OSError):
                old_check.bind(('127.0.0.1', port))
        assert_ports_available([(port, socket.SOCK_STREAM)])

    def test_live_udp_receiver_is_rejected(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(('127.0.0.1', 0))
            with self.assertRaises(OSError):
                assert_ports_available([(receiver.getsockname()[1], socket.SOCK_DGRAM)])


if __name__ == '__main__':
    unittest.main()
