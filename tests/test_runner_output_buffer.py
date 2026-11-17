import threading
import unittest

from src.agent.runner_output_buffer import BoundedRunnerOutput, RunnerOutputError


class BoundedRunnerOutputTests(unittest.TestCase):
    def test_mixed_unicode_exact_cap(self):
        e, euro, smile = chr(0xE9), chr(0x20AC), chr(0x1F600)
        out = BoundedRunnerOutput(10)
        self.assertEqual(out.write_stdout('a' + e), 2)
        self.assertEqual(out.write_stderr(euro + smile), 2)
        self.assertEqual(out.finish(), ('a' + e, euro + smile))
        out = BoundedRunnerOutput(10)
        out.write_stdout('a' + e + euro + smile)
        self.assertEqual(out.write_stderr(''), 0)
        with self.assertRaises(RunnerOutputError):
            out.write_stderr('x')

    def test_over_limit_poisons_atomically_without_replay(self):
        e = chr(0xE9)
        out = BoundedRunnerOutput(5)
        out.write_stdout('abc')
        with self.assertRaises(RunnerOutputError):
            out.write_stderr('d' + e + 'f')
        self.assertEqual(out._chunks, ([], []))
        with self.assertRaises(RunnerOutputError):
            out.write_stdout('a')
        with self.assertRaises(RunnerOutputError):
            out.finish()
        with self.assertRaises(RunnerOutputError):
            out.finish()
        small = BoundedRunnerOutput(4)
        with self.assertRaises(RunnerOutputError):
            small.write_stdout('x' * 5)

    def test_invalid_config_types_and_surrogates(self):
        for bad in (True, 0, -1, 524289, '5', 5.0, None):
            with self.assertRaises(RunnerOutputError):
                BoundedRunnerOutput(bad)
        BoundedRunnerOutput(1)
        BoundedRunnerOutput(524288)

        class S(str):
            pass

        for bad in (b'x', None, 1, S('x'), chr(0xD800)):
            out = BoundedRunnerOutput(16)
            with self.assertRaises(RunnerOutputError):
                out.write_stdout(bad)
            with self.assertRaises(RunnerOutputError):
                out.write_stderr('ok')
            with self.assertRaises(RunnerOutputError):
                out.finish()
        out = BoundedRunnerOutput(16)
        with self.assertRaises(RunnerOutputError):
            out.stderr.write(chr(0xDC00))
        with self.assertRaises(RunnerOutputError):
            out.stdout.write('x')

    def test_empty_writes_do_not_grow_memory(self):
        out = BoundedRunnerOutput(8)
        for _ in range(50000):
            out.stdout.write('')
            out.write_stderr('')
        self.assertEqual(out._chunks, ([], []))
        self.assertEqual(out._used, 0)
        self.assertEqual(out.stdout.write('12345678'), 8)
        self.assertEqual(out.finish(), ('12345678', ''))

    def test_finish_and_sink_lifecycle(self):
        out = BoundedRunnerOutput(32)
        self.assertIs(out.stdout, out.stdout)
        self.assertTrue(out.stdout.writable())
        self.assertEqual(out.stdout.write('hi'), 2)
        out.stdout.flush()
        out.stderr.flush()
        result = out.finish()
        self.assertIsInstance(result, tuple)
        self.assertEqual(result, ('hi', ''))
        self.assertFalse(out.stderr.writable())
        calls = (
            lambda: out.stdout.write('x'),
            lambda: out.stderr.write(''),
            out.stdout.flush,
            lambda: out.write_stdout('x'),
            lambda: out.write_stderr('x'),
            out.finish,
        )
        for call in calls:
            with self.assertRaises(RunnerOutputError):
                call()
        self.assertEqual(result, ('hi', ''))
        poisoned = BoundedRunnerOutput(1)
        with self.assertRaises(RunnerOutputError):
            poisoned.stdout.write('ab')
        self.assertFalse(poisoned.stdout.writable())
        with self.assertRaises(RunnerOutputError):
            poisoned.stderr.flush()

    def test_shared_combined_accounting_and_threads(self):
        out = BoundedRunnerOutput(6)
        out.stdout.write('abc')
        out.stderr.write('def')
        with self.assertRaises(RunnerOutputError):
            out.stdout.write('g')
        out = BoundedRunnerOutput(4000)

        def worker(sink):
            for _ in range(1000):
                sink.write('x')

        threads = [
            threading.Thread(target=worker, args=(sink,))
            for sink in (out.stdout, out.stderr, out.stdout, out.stderr)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(out.finish(), ('x' * 2000, 'x' * 2000))


if __name__ == '__main__':
    unittest.main()
