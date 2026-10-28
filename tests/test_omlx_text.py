#!/usr/bin/env python3
"""oMLX model transitions must confirm asynchronous unload completion."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import omlx_client
import omlx_text

CONFIG = {'base_url': 'http://127.0.0.1:8000', 'api_key': 'test-key'}
MODEL = 'Example-27B'


class TransitionTests(unittest.TestCase):
    def call(self, status_code, statuses, *, load=False, timeout=1200):
        response = mock.Mock(status_code=status_code, text='unloading')
        with mock.patch.object(omlx_text.requests, 'post', return_value=response) as post, \
             mock.patch.object(omlx_text, 'model_status', side_effect=statuses) as status, \
             mock.patch.object(omlx_text.time, 'sleep') as sleep:
            result = omlx_text.transition(CONFIG, MODEL, load, timeout=timeout)
        return result, post, status, sleep

    def test_202_unload_waits_for_status_to_confirm_unloaded(self):
        result, post, status, sleep = self.call(202, [[{'id': MODEL, 'loaded': True}],
                                                        [{'id': MODEL, 'loaded': False}]])
        self.assertEqual({'id': MODEL, 'loaded': False}, result)
        self.assertEqual(2, status.call_count)
        sleep.assert_called_once_with(omlx_text.UNLOAD_POLL_INTERVAL)
        self.assertTrue(post.call_args.args[0].endswith('/unload'))

    def test_202_unload_times_out_if_model_stays_loaded(self):
        response = mock.Mock(status_code=202, text='unloading')
        with mock.patch.object(omlx_text.requests, 'post', return_value=response), \
             mock.patch.object(omlx_text, 'model_status', return_value=[{'id': MODEL, 'loaded': True}]), \
             mock.patch.object(omlx_text.time, 'monotonic', side_effect=[0, 1, 3]), \
             mock.patch.object(omlx_text.time, 'sleep') as sleep:
            with self.assertRaisesRegex(omlx_client.OMLXError, 'timed out'):
                omlx_text.transition(CONFIG, MODEL, False, timeout=2)
        sleep.assert_called_once()

    def test_202_unload_rejects_missing_model_or_loaded_flag(self):
        for rows, error in (([], 'omitted'), ([{'id': MODEL}], 'missing loaded flag')):
            with self.subTest(error=error), \
                 mock.patch.object(omlx_text.requests, 'post', return_value=mock.Mock(status_code=202)), \
                 mock.patch.object(omlx_text, 'model_status', return_value=rows):
                with self.assertRaisesRegex(omlx_client.OMLXError, error):
                    omlx_text.transition(CONFIG, MODEL, False)

    def test_202_load_is_not_accepted(self):
        with mock.patch.object(omlx_text.requests, 'post', return_value=mock.Mock(status_code=202, text='loading')), \
             mock.patch.object(omlx_text, 'model_status') as status:
            with self.assertRaisesRegex(omlx_client.OMLXError, r'load failed \(202\)'):
                omlx_text.transition(CONFIG, MODEL, True)
        status.assert_not_called()

    def test_synchronous_transitions_still_use_one_readback(self):
        for code, load in ((200, True), (204, False)):
            with self.subTest(code=code, load=load):
                result, _, status, sleep = self.call(code, [[{'id': MODEL, 'loaded': load}]], load=load)
                self.assertEqual(load, result['loaded'])
                status.assert_called_once()
                sleep.assert_not_called()

    def test_synchronous_unload_does_not_accept_still_loaded(self):
        with self.assertRaisesRegex(omlx_client.OMLXError, 'read-back failed'):
            self.call(200, [[{'id': MODEL, 'loaded': True}]])


if __name__ == '__main__':
    unittest.main()
